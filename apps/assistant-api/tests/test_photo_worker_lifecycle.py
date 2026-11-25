import asyncio
import io
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from assistant.capture_jobs import CaptureJobs, draft_result
from assistant.clients import ServiceError
from assistant.config import Config
from assistant.main import PREFIX, create_app
from assistant.schemas import Vision
from assistant.state import State
from test_api import FakeClients, G1, U1, confirm, propose


def answer():
    return Vision.model_validate({"items": [{"name": "Synthetic box", "confidence": .9}]})


@pytest.fixture
def worker_state(tmp_path):
    cfg = Config.load()
    cfg.state, cfg.uploads = tmp_path / "lifecycle.db", tmp_path / "uploads"
    cfg.uploads.mkdir()
    state, clients = State(cfg.state, cfg.instance_id), FakeClients()
    session = {"id": "synthetic-lifecycle-session", "actor": U1, "collection": G1, "credential": "Bearer alice"}
    state.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                  (session["id"], cfg.cipher.encrypt(b"Bearer alice").decode(),
                   U1, G1, "synthetic-csrf", time.time() + 3600))

    async def analyze(*args):
        return answer()

    return CaptureJobs(cfg, state, clients, SimpleNamespace(analyze=analyze)), session


def stall_guard_cleanup(loop, entered, release, finished):
    previous = loop.get_task_factory()

    def factory(loop, coro, **kwargs):
        if coro.cr_code.co_qualname == "CaptureJobs.process.<locals>.watch_context":
            async def controlled_guard():
                try:
                    return await coro
                finally:
                    entered.set()
                    try:
                        await release.wait()
                    finally:
                        finished.set()

            return asyncio.Task(controlled_guard(), loop=loop, **kwargs)
        if previous:
            return previous(loop, coro, **kwargs)
        return asyncio.Task(coro, loop=loop, **kwargs)

    loop.set_task_factory(factory)
    return previous


def retained_photo(manager, row):
    capture = manager.state.one("SELECT filename FROM captures WHERE id=?", (row["capture_id"],))
    assert capture is not None
    path = manager.cfg.uploads / capture["filename"]
    assert path.exists() and path.read_bytes()


def test_real_guard_cancel_boundary_preserves_outer_worker_cancellation(worker_state, monkeypatch):
    manager, session = worker_state
    row = manager.submit(session, "synthetic-real-guard-boundary", b"synthetic-normalized-image", "", None)
    calls = []

    async def analyze(data, hint):
        calls.append(data)
        return answer()

    manager.vision.analyze = analyze

    async def scenario():
        original_create_task = asyncio.create_task
        injected, guards = asyncio.Event(), []

        class ShutdownGuard(asyncio.Task):
            def cancel(self, *args, **kwargs):
                parent = asyncio.current_task()
                result = super().cancel(*args, **kwargs)
                if result and parent is not None and parent is not self and not injected.is_set():
                    asyncio.get_running_loop().call_soon(parent.cancel)
                    injected.set()
                return result

        def intercept(coro, *args, **kwargs):
            if coro.cr_code.co_qualname == "CaptureJobs.process.<locals>.watch_context":
                guard = ShutdownGuard(coro, *args, **kwargs)
                guards.append(guard)
                return guard
            return original_create_task(coro, *args, **kwargs)

        monkeypatch.setattr(asyncio, "create_task", intercept)
        worker = original_create_task(manager.run())
        try:
            await asyncio.wait_for(injected.wait(), 1)
            done, _ = await asyncio.wait((worker,), timeout=.3)
            assert worker in done, "Real guard cleanup swallowed the scheduled worker cancellation"
            assert worker.cancelled()
            assert guards and all(guard.done() for guard in guards)
            with manager.try_model_lock() as acquired:
                assert acquired
        finally:
            monkeypatch.setattr(asyncio, "create_task", original_create_task)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())
    assert manager.load(session, row["id"])["status"] == "succeeded"
    assert calls == [b"synthetic-normalized-image"]
    retained_photo(manager, row)
    assert manager.clients.calls["create_item"] == manager.clients.calls["upload"] == 0


def test_cancel_during_guard_cleanup_stops_worker_and_preserves_committed_draft(worker_state):
    manager, session = worker_state
    row = manager.submit(session, "synthetic-cleanup-cancellation", b"synthetic-normalized-image", "", None)

    async def scenario():
        loop = asyncio.get_running_loop()
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        previous = stall_guard_cleanup(loop, entered, release, finished)
        worker = asyncio.create_task(manager.run())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert manager.load(session, row["id"])["status"] == "succeeded"
            worker.cancel()
            done, _ = await asyncio.wait((worker,), timeout=.3)
            assert worker in done, "Worker swallowed shutdown cancellation during guard cleanup"
            assert worker.cancelled()
            assert finished.is_set()
            with manager.try_model_lock() as acquired:
                assert acquired
        finally:
            release.set()
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            loop.set_task_factory(previous)

    asyncio.run(scenario())
    assert manager.load(session, row["id"])["status"] == "succeeded"
    retained_photo(manager, row)

    async def restart():
        assert await manager.step() is False

    asyncio.run(restart())
    assert manager.load(session, row["id"])["status"] == "succeeded"
    assert manager.clients.calls["create_item"] == manager.clients.calls["upload"] == 0


def test_cancelling_inflight_processing_releases_lock_and_resumes_only_queued_job(worker_state):
    manager, session = worker_state
    interrupted = manager.submit(session, "synthetic-inflight", b"synthetic-first-image", "", None)
    queued = manager.submit(session, "synthetic-queued", b"synthetic-second-image", "", None)
    calls = []

    async def scenario():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def analyze(data, hint):
            calls.append(data)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        manager.vision.analyze = analyze
        processing = asyncio.create_task(manager.step())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            processing.cancel()
            done, _ = await asyncio.wait((processing,), timeout=.3)
            assert processing in done and processing.cancelled()
            assert cancelled.is_set()
            assert manager.load(session, interrupted["id"])["status"] == "running"
            assert manager.load(session, queued["id"])["status"] == "queued"
            with manager.try_model_lock() as acquired:
                assert acquired
        finally:
            processing.cancel()
            await asyncio.gather(processing, return_exceptions=True)

        async def resumed(data, hint):
            calls.append(data)
            return answer()

        restarted = CaptureJobs(manager.cfg, manager.state, manager.clients, SimpleNamespace(analyze=resumed))
        assert await restarted.step() is True
        assert await restarted.step() is False

    asyncio.run(scenario())
    first, second = manager.load(session, interrupted["id"]), manager.load(session, queued["id"])
    assert first["status"] == "manual"
    assert manager.public(first)["warningCodes"] == ["analysis_interrupted"]
    assert second["status"] == "succeeded"
    assert calls == [b"synthetic-first-image", b"synthetic-second-image"]
    retained_photo(manager, first)
    retained_photo(manager, second)
    assert manager.clients.calls["create_item"] == manager.clients.calls["upload"] == 0


def test_testclient_lifespan_shutdown_does_not_need_a_second_worker_cancel(tmp_path, monkeypatch):
    cfg = Config.load()
    cfg.state, cfg.uploads = tmp_path / "testclient.db", tmp_path / "uploads"
    fake = FakeClients()
    app = create_app(cfg, fake)
    entered, finished, rescued = threading.Event(), threading.Event(), threading.Event()
    closed = []
    original_close = app.state.vision.aclose

    async def analyze(*args):
        return answer()

    async def close():
        closed.append(True)
        await original_close()

    monkeypatch.setattr(app.state.vision, "analyze", analyze)
    monkeypatch.setattr(app.state.vision, "aclose", close)
    photo = io.BytesIO()
    Image.new("RGB", (32, 24), "blue").save(photo, "JPEG")

    with TestClient(app, base_url="https://inventory.test", headers={"Origin": cfg.origin}) as client:
        async def install():
            loop = asyncio.get_running_loop()
            stall_guard_cleanup(loop, entered, asyncio.Event(), finished)
            return next(task for task in asyncio.all_tasks()
                        if task.get_coro().cr_code.co_qualname == "CaptureJobs.run")

        worker = client.portal.call(install)
        login = client.post(PREFIX + "/auth/login", json={"username": "alice", "password": "synthetic"}).json()
        client.headers.update({"X-CSRF-Token": login["csrfToken"], "X-Collection-ID": login["collectionId"]})
        response = client.post(PREFIX + "/capture-jobs", files={"image": ("synthetic.jpg", photo.getvalue(), "image/jpeg")},
                               headers={"Idempotency-Key": "synthetic-lifespan-shutdown"})
        assert response.status_code == 202
        row = response.json()
        assert entered.wait(2), "Worker did not enter its controlled guard cleanup"

        async def watchdog():
            # Rescue a broken implementation so the regression fails in bounded
            # time instead of leaving TestClient.__exit__ and pytest hung.
            done, _ = await asyncio.wait((worker,), timeout=.3)
            if not done:
                rescued.set()
                worker.cancel()

        client.portal.start_task_soon(watchdog)

    assert not rescued.is_set(), "Lifespan needed a second cancellation to stop its worker"
    assert worker.cancelled() and finished.is_set()
    assert closed == [True]
    assert app.state.store.one("SELECT status FROM capture_jobs WHERE id=?", (row["id"],))["status"] == "succeeded"
    assert len(list(cfg.uploads.iterdir())) == 1
    assert fake.calls["create_item"] == fake.calls["upload"] == 0
    with app.state.capture_jobs.try_model_lock() as acquired:
        assert acquired


@pytest.mark.parametrize("status", ["succeeded", "manual"])
@pytest.mark.parametrize("unavailable", ["missing_record", "expired_capture", "missing_file"])
def test_terminal_capture_availability_is_checked_without_mutating_job(worker_state, status, unavailable):
    manager, session = worker_state
    data, key = b"synthetic-availability-image", "synthetic-availability-key"
    row = manager.submit(session, key, data, "", None)
    manager.finish(row, status, draft_result(row["capture_id"], answer()), expected="queued")
    capture = manager.state.one("SELECT * FROM captures WHERE id=?", (row["capture_id"],))
    if unavailable == "missing_record":
        manager.state.execute("DELETE FROM captures WHERE id=?", (capture["id"],))
    elif unavailable == "expired_capture":
        manager.state.execute("UPDATE captures SET expires=? WHERE id=?", (time.time() - 1, capture["id"]))
    else:
        (manager.cfg.uploads / capture["filename"]).unlink()
    stored = manager.load(session, row["id"])
    public = manager.public(stored)
    assert public["status"] == "expired" and public["result"] is None
    assert public["error"] == {
        "code": "capture_expired", "message": "This photo is no longer available. Take a new photo.",
    }
    assert public["warningCodes"] == []
    assert manager.load(session, row["id"]) == stored
    replay = manager.submit(session, key, data, "", None)
    assert manager.public(replay) == public
    assert len(manager.state.all("SELECT * FROM capture_jobs")) == 1
    assert manager.clients.calls["create_item"] == manager.clients.calls["upload"] == 0


@pytest.mark.parametrize("field", ["session", "collection"])
def test_terminal_capture_requires_matching_photo_scope(worker_state, field):
    manager, session = worker_state
    row = manager.submit(session, "synthetic-photo-scope", b"synthetic-scoped-image", "", None)
    manager.finish(row, "succeeded", draft_result(row["capture_id"], answer()), expected="queued")
    manager.state.execute(f"UPDATE captures SET {field}=? WHERE id=?", ("synthetic-other-scope", row["capture_id"]))
    public = manager.public(manager.load(session, row["id"]))
    assert public["status"] == "expired" and public["result"] is None
    with pytest.raises(ServiceError) as denied:
        manager.load({**session, "id" if field == "session" else field: "synthetic-other-scope"}, row["id"])
    assert denied.value.status == 404
    assert manager.state.one("SELECT * FROM captures WHERE id=?", (row["capture_id"],)) is not None
    assert len(list(manager.cfg.uploads.iterdir())) == 1


@pytest.fixture
def completed_upload(tmp_path, monkeypatch):
    cfg = Config.load()
    cfg.state, cfg.uploads = tmp_path / "availability.db", tmp_path / "uploads"
    fake = FakeClients()
    app = create_app(cfg, fake)
    finished, calls = threading.Event(), []
    original_finish = app.state.capture_jobs.finish

    async def analyze(*args):
        calls.append(True)
        return answer()

    async def unexpected_intent(*args):
        pytest.fail("Availability tests must not call a model for chat intent")

    def finish(row, status, *args, **kwargs):
        original_finish(row, status, *args, **kwargs)
        if status == "succeeded":
            finished.set()

    monkeypatch.setattr(app.state.vision, "analyze", analyze)
    monkeypatch.setattr(app.state.vision, "intent", unexpected_intent)
    monkeypatch.setattr(app.state.capture_jobs, "finish", finish)
    image = io.BytesIO()
    Image.new("RGB", (32, 24), "blue").save(image, "JPEG")
    data, key = image.getvalue(), "synthetic-availability-replay"
    with TestClient(app, base_url="https://inventory.test", headers={"Origin": cfg.origin}) as client:
        login = client.post(PREFIX + "/auth/login", json={"username": "alice", "password": "synthetic"}).json()
        client.headers.update({"X-CSRF-Token": login["csrfToken"], "X-Collection-ID": login["collectionId"]})
        response = client.post(PREFIX + "/capture-jobs", files={"image": ("synthetic.jpg", data, "image/jpeg")},
                               headers={"Idempotency-Key": key})
        assert response.status_code == 202
        assert finished.wait(2)
        job = client.get(PREFIX + "/capture-jobs/" + response.json()["id"]).json()
        assert job["status"] == "succeeded"
        yield client, app, fake, job, data, key, calls


def recover_all_paths(client, job, data, key):
    loaded = client.get(PREFIX + "/capture-jobs/" + job["id"])
    exact = client.get(PREFIX + "/capture-jobs", params={"submissionKey": key})
    listed = client.get(PREFIX + "/capture-jobs")
    replay = client.post(PREFIX + "/capture-jobs", files={"image": ("synthetic.jpg", data, "image/jpeg")},
                         headers={"Idempotency-Key": key})
    assert loaded.status_code == exact.status_code == listed.status_code == 200
    assert replay.status_code == 202
    return [loaded.json(), exact.json()["jobs"][0], listed.json()["jobs"][0], replay.json()]


def test_unconsumed_photo_survives_new_chat_and_idempotent_recovery(completed_upload):
    client, app, fake, job, data, key, calls = completed_upload
    assert client.post(PREFIX + "/conversations/reset").status_code == 200
    assert client.get(PREFIX + "/conversations/current").json()["conversation"] is None
    assert recover_all_paths(client, job, data, key) == [job] * 4
    assert calls == [True]
    assert len(app.state.store.all("SELECT * FROM capture_jobs")) == 1
    assert len(app.state.store.all("SELECT * FROM captures")) == 1
    assert len(list(app.state.config.uploads.iterdir())) == 1
    assert fake.calls["create_item"] == fake.calls["upload"] == 0


def test_consumed_photo_cannot_resurface_after_chat_edit_confirmation_and_reset(completed_upload):
    client, app, fake, job, data, key, calls = completed_upload
    fake.add("Entry", location=True)
    original = propose(client, "item.create", {"name": "Manual synthetic box", "quantity": 3},
                       captureId=job["captureId"])
    reply = client.post(PREFIX + "/chat", json={
        "message": "Add a different chat cable to Entry", "clientTurnId": str(uuid4()),
    }).json()
    assert reply["action"]["id"] == original["id"]
    edited = client.post(PREFIX + "/chat", json={
        "message": "make that two", "clientTurnId": str(uuid4()),
        "conversationId": reply["conversationId"], "expectedRevision": reply["revision"],
    }).json()
    assert edited["action"]["payload"]["quantity"] == 2
    assert edited["action"]["after"]["photoIncluded"] is True
    assert confirm(client, edited["action"]).json()["status"] == "succeeded"
    assert app.state.store.one("SELECT * FROM captures WHERE id=?", (job["captureId"],)) is None
    assert client.post(PREFIX + "/conversations/reset").status_code == 200
    assert client.get(PREFIX + "/conversations/current").json()["conversation"] is None
    for recovered in recover_all_paths(client, job, data, key):
        assert recovered["id"] == job["id"]
        assert recovered["status"] == "expired" and recovered["result"] is None
        assert recovered["error"]["code"] == "capture_expired"
    assert calls == [True]
    assert len(app.state.store.all("SELECT * FROM capture_jobs")) == 1
    assert app.state.store.all("SELECT * FROM captures") == []
    assert list(app.state.config.uploads.iterdir()) == []
    assert fake.calls["create_item"] == fake.calls["upload"] == 1


def test_retryable_photo_action_retains_recoverable_capture_until_success(completed_upload):
    client, app, fake, job, data, key, calls = completed_upload
    fake.upload_failures = 1
    action = propose(client, "item.create", {"name": "Retryable synthetic box"}, captureId=job["captureId"])
    failed = confirm(client, action).json()
    assert failed["status"] == "failed" and failed["retryable"] is True
    assert recover_all_paths(client, job, data, key) == [job] * 4
    assert len(app.state.store.all("SELECT * FROM captures")) == 1
    assert len(list(app.state.config.uploads.iterdir())) == 1
    assert confirm(client, action).json()["status"] == "succeeded"
    assert client.get(PREFIX + "/capture-jobs/" + job["id"]).json()["status"] == "expired"
    assert calls == [True]
    assert fake.calls["create_item"] == 1 and fake.calls["upload"] == 2
