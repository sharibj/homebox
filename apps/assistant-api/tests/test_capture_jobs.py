import asyncio
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from assistant.capture_jobs import CaptureJobs, MODEL_LOCK, draft_result
from assistant.clients import ServiceError
from assistant.config import Config
from assistant.images import VisionClient
from assistant.main import PREFIX, create_app
from assistant.schemas import Vision
from assistant.state import State
from test_api import FakeClients, G1, G2, U1, confirm, propose, setup


def photo(color="blue"):
    output = io.BytesIO()
    Image.new("RGB", (48, 32), color).save(output, "JPEG")
    return output.getvalue()


def answer():
    return Vision.model_validate({"items": [{"name": "Blue box", "confidence": .8}]})


def submit(client, key="photo-submission-one", **kwargs):
    return client.post(PREFIX + "/capture-jobs", files={"image": ("photo.jpg", photo(), "image/jpeg")},
                       headers={"Idempotency-Key": key}, **kwargs)


def terminal(client, job, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(PREFIX + "/capture-jobs/" + job["id"])
        assert response.status_code == 200, response.text
        value = response.json()
        if value["status"] not in {"queued", "running"}:
            return value
        time.sleep(.01)
    raise AssertionError("Photo job did not reach a terminal state")


@pytest.fixture
def standalone(tmp_path):
    cfg = Config.load()
    cfg.state, cfg.uploads = tmp_path / "jobs.db", tmp_path / "uploads"
    cfg.uploads.mkdir()
    state, clients = State(cfg.state, cfg.instance_id), FakeClients()
    session = {"id": "test-browser-session", "actor": U1, "collection": G1, "credential": "Bearer alice"}
    state.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                  (session["id"], cfg.cipher.encrypt(b"Bearer alice").decode(), U1, G1, "csrf", time.time() + 3600))
    calls = []

    async def analyze(*args):
        calls.append(args)
        return answer()

    manager = CaptureJobs(cfg, state, clients, SimpleNamespace(analyze=analyze))
    return manager, session, calls


def test_submission_is_prompt_durable_idempotent_and_recoverable(setup, monkeypatch):
    client, app, fake, _ = setup
    calls = []

    async def slow(*args):
        calls.append(1)
        await asyncio.sleep(3600)

    monkeypatch.setattr(app.state.vision, "analyze", slow)
    started = time.monotonic()
    response = submit(client, data={"hint": "private photo hint"})
    assert response.status_code == 202, response.text
    assert time.monotonic() - started < 2
    job = response.json()
    assert job["status"] == "queued" and job["result"] is None
    recovered = client.get(PREFIX + "/capture-jobs", params={"submissionKey": job["submissionKey"]}).json()["jobs"]
    assert recovered[0]["id"] == job["id"]
    for _ in range(5):
        replay = submit(client, data={"hint": "private photo hint"})
        assert replay.status_code == 202 and replay.json()["id"] == job["id"]
    assert len(app.state.store.all("SELECT * FROM captures")) == 1
    assert len(app.state.store.all("SELECT * FROM capture_jobs")) == 1
    assert "private photo hint" not in app.state.store.one("SELECT hint FROM capture_jobs")["hint"]
    path = next(app.state.config.uploads.iterdir())
    assert path.stat().st_mode & 0o777 == 0o600
    assert Image.open(path).format == "JPEG"
    assert fake.calls["create_item"] == 0 and len(calls) <= 1
    conflict = submit(client, data={"hint": "different hint"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"


def test_finished_job_still_requires_review_and_confirmation(setup, monkeypatch):
    client, app, fake, _ = setup

    async def analyzed(*args):
        return answer()

    monkeypatch.setattr(app.state.vision, "analyze", analyzed)
    job = terminal(client, submit(client).json())
    assert job["status"] == "succeeded" and job["result"]["draft"]["name"] == "Blue box"
    assert fake.calls["create_item"] == 0
    recovered = client.get(PREFIX + "/capture-jobs").json()["jobs"]
    assert recovered[0] == job
    action = propose(client, "item.create", {"name": "Reviewed blue box"}, captureId=job["captureId"])
    assert fake.calls["create_item"] == 0
    saved = confirm(client, action).json()
    assert saved["status"] == "succeeded"
    assert confirm(client, action).json() == saved
    assert fake.calls["create_item"] == fake.calls["upload"] == 1


def test_jobs_fail_closed_across_session_actor_and_collection(setup, monkeypatch):
    client, app, fake, _ = setup

    async def slow(*args):
        await asyncio.sleep(3600)

    monkeypatch.setattr(app.state.vision, "analyze", slow)
    job = submit(client).json()
    switched = client.post(PREFIX + "/session/collection", json={"collectionId": G2})
    assert switched.status_code == 200
    assert client.get(PREFIX + "/capture-jobs/" + job["id"]).status_code == 409
    client.headers["X-Collection-ID"] = G2
    assert client.get(PREFIX + "/capture-jobs/" + job["id"]).status_code == 404
    assert client.get(PREFIX + "/capture-jobs").json()["jobs"] == []
    assert submit(client).status_code == 409
    for username in ("alice", "bob"):
        login = client.post(PREFIX + "/auth/login", json={"username": username, "password": "fixture"}).json()
        client.headers["X-Collection-ID"] = login["collectionId"]
        assert client.get(PREFIX + "/capture-jobs/" + job["id"]).status_code == 404
        assert client.get(PREFIX + "/capture-jobs").json()["jobs"] == []
    assert fake.calls["create_item"] == 0


def test_submission_origin_csrf_collection_and_validation(setup):
    client, app, fake, _ = setup
    assert client.post(PREFIX + "/capture-jobs", files={"image": ("x.jpg", photo())}).status_code == 400
    assert submit(client, data={"hint": "x" * 501}).status_code == 422
    client.headers.pop("X-CSRF-Token")
    assert submit(client).status_code == 403
    assert client.get(PREFIX + "/capture-jobs", headers={"X-Collection-ID": G2}).status_code == 409


def test_bounded_queue_and_queue_deadline_retain_manual_photos(standalone):
    manager, session, calls = standalone
    manager.cfg.analysis_queue_limit = 1
    first = manager.submit(session, "queue-first", photo(), "", None)
    full = manager.submit(session, "queue-second", photo("red"), "", None)
    assert full["status"] == "manual"
    assert manager.public(full)["warningCodes"] == ["analysis_queue_full"]
    manager.state.execute("UPDATE capture_jobs SET created=? WHERE id=?",
                          (time.time() - manager.cfg.analysis_queue_timeout - 1, first["id"]))
    manager.refresh()
    expired = manager.public(manager.load(session, first["id"]))
    assert expired["status"] == "manual" and expired["warningCodes"] == ["analysis_queue_timed_out"]
    assert expired["result"]["draft"]["name"] == ""
    assert len(list(manager.cfg.uploads.iterdir())) == 2 and calls == []


def test_concurrent_lost_response_replays_store_one_photo_and_job(standalone):
    manager, session, calls = standalone
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda _: manager.submit(session, "same-submission", photo(), "", None), range(6)))
    assert len({row["id"] for row in rows}) == 1
    assert len(manager.state.all("SELECT * FROM captures")) == 1
    assert len(list(manager.cfg.uploads.iterdir())) == 1 and calls == []


def test_queued_job_resumes_but_interrupted_running_job_never_reinfers(standalone):
    manager, session, calls = standalone
    interrupted = manager.submit(session, "interrupted-key", photo(), "", None)
    manager.state.execute("UPDATE capture_jobs SET status='running',started=? WHERE id=?",
                          (time.time(), interrupted["id"]))
    queued = manager.submit(session, "resumable-key", photo(), "", None)
    asyncio.run(manager.step())
    assert manager.public(manager.load(session, interrupted["id"]))["warningCodes"] == ["analysis_interrupted"]
    assert manager.load(session, queued["id"])["status"] == "succeeded"
    assert len(calls) == 1


def test_recovery_rereads_after_lock_without_clobbering_finished_job(standalone, monkeypatch):
    manager, session, calls = standalone
    row = manager.submit(session, "interleaved-key", photo(), "", None)
    manager.state.execute("UPDATE capture_jobs SET status='running' WHERE id=?", (row["id"],))
    original = manager.try_model_lock

    @contextmanager
    def interleaved():
        manager.finish(row, "succeeded", draft_result(row["capture_id"], answer()))
        with original() as acquired:
            yield acquired

    monkeypatch.setattr(manager, "try_model_lock", interleaved)
    asyncio.run(manager.step())
    assert manager.load(session, row["id"])["status"] == "succeeded" and calls == []


def _hold_model_lock(path, instance, acquired, release):
    import fcntl
    from pathlib import Path
    state = State(Path(path), instance)
    with (state.locks / MODEL_LOCK).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        acquired.set()
        release.wait(10)


def test_live_worker_lock_and_crash_release_across_processes(standalone):
    import multiprocessing
    manager, session, calls = standalone
    row = manager.submit(session, "worker-crash-key", photo(), "", None)
    manager.state.execute("UPDATE capture_jobs SET status='running' WHERE id=?", (row["id"],))
    context = multiprocessing.get_context("spawn")
    acquired, release = context.Event(), context.Event()
    process = context.Process(target=_hold_model_lock,
                              args=(str(manager.cfg.state), manager.cfg.instance_id, acquired, release))
    try:
        process.start()
        assert acquired.wait(4)
        assert asyncio.run(manager.step()) is False
        assert manager.load(session, row["id"])["status"] == "running"
        process.terminate()
        process.join(4)
        asyncio.run(manager.step())
        recovered = manager.public(manager.load(session, row["id"]))
        assert recovered["status"] == "manual" and recovered["warningCodes"] == ["analysis_interrupted"]
        assert calls == []
    finally:
        if process.is_alive():
            process.terminate()
        process.join(4)


def test_total_running_deadline_cancels_and_retains_manual_photo(standalone):
    manager, session, calls = standalone
    manager.cfg.analysis_timeout = .05
    cancelled = []

    async def slow(*args):
        try:
            await asyncio.sleep(1)
        finally:
            cancelled.append(True)

    manager.vision.analyze = slow
    row = manager.submit(session, "deadline-key", photo(), "", None)
    asyncio.run(manager.step())
    result = manager.public(manager.load(session, row["id"]))
    assert result["status"] == "manual" and result["warningCodes"] == ["analysis_timed_out"]
    assert cancelled and next(manager.cfg.uploads.iterdir()).exists()


def test_schema_retries_and_semaphore_share_one_total_budget(standalone):
    manager, _, _ = standalone
    manager.cfg.analysis_timeout = .05
    client = VisionClient(manager.cfg)
    calls = []

    async def invalid(*args, **kwargs):
        calls.append(kwargs["timeout_seconds"])
        await asyncio.sleep(.03)
        return "{}"

    client.provider.generate = invalid

    async def run():
        with pytest.raises(ServiceError) as exc:
            await client.analyze(photo(), "")
        assert exc.value.code == "analysis_timed_out"
        assert len(calls) == 2 and calls[1] < calls[0]
        calls.clear()
        async with client.lock:
            with pytest.raises(ServiceError) as exc:
                await client.analyze(photo(), "")
        assert exc.value.code == "analysis_timed_out" and calls == []
        await client.provider.aclose()

    asyncio.run(run())


def test_legacy_queue_and_inference_return_manual_before_browser_budget(setup, monkeypatch):
    import fcntl
    client, app, fake, _ = setup
    app.state.config.capture_sync_timeout = .06
    calls = []

    async def slow(*args):
        calls.append(1)
        await asyncio.sleep(1)

    monkeypatch.setattr(app.state.vision, "analyze", slow)
    with (app.state.store.locks / MODEL_LOCK).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        response = client.post(PREFIX + "/captures", files={"image": ("old.jpg", photo())})
        assert response.status_code == 200 and "time limit" in response.json()["warnings"][0]
        assert calls == []
    response = client.post(PREFIX + "/captures", files={"image": ("old.jpg", photo())})
    assert response.status_code == 200 and response.json()["draft"]["name"] == ""
    assert calls == [1] and len(app.state.store.all("SELECT * FROM captures")) == 2
    assert fake.calls["create_item"] == 0


@pytest.mark.parametrize("change", ["logout", "collection", "membership"])
def test_background_analysis_fails_closed_on_access_change(standalone, change):
    manager, session, calls = standalone
    row = manager.submit(session, "context-key", photo(), "", None)
    if change == "logout":
        manager.state.execute("DELETE FROM sessions")
    elif change == "collection":
        manager.state.execute("UPDATE sessions SET collection=?", (G2,))
    else:
        manager.clients.memberships["Bearer alice"] = [G2]
    asyncio.run(manager.step())
    result = manager.public(manager.load(session, row["id"]))
    assert result["status"] == "failed" and result["result"] is None and result["error"]
    assert calls == []


def test_expired_job_does_not_reanalyze_or_revive_from_stale_completion(standalone):
    manager, session, calls = standalone
    row = manager.submit(session, "expired-key", photo(), "", None)
    manager.state.execute("UPDATE capture_jobs SET expires=? WHERE id=?", (time.time() - 1, row["id"]))
    manager.refresh()
    manager.finish(row, "succeeded", draft_result(row["capture_id"], answer()))
    current = manager.public(manager.load(session, row["id"]))
    assert current["status"] == "expired" and current["result"] is None
    assert manager.submit(session, "expired-key", photo(), "", None)["status"] == "expired"
    assert calls == []


def test_recovery_is_bounded_and_exact_key_recovers_older_job(standalone, monkeypatch):
    manager, session, _ = standalone
    for index in range(25):
        manager.submit(session, f"submission-{index:03}", photo(), "", None)
    app = create_app(manager.cfg, manager.clients)
    async def local_answer(*args):
        return answer()
    monkeypatch.setattr(app.state.vision, "analyze", local_answer)
    from hashlib import sha256
    raw = "disposable-cookie"
    manager.state.execute("UPDATE sessions SET id=?", (sha256(raw.encode()).hexdigest(),))
    manager.state.execute("UPDATE capture_jobs SET session=?", (sha256(raw.encode()).hexdigest(),))
    with TestClient(app, base_url="https://inventory.test", headers={"Origin": manager.cfg.origin, "X-Collection-ID": G1}) as client:
        client.cookies.set("hb_assistant", raw, path=PREFIX)
        assert len(client.get(PREFIX + "/capture-jobs").json()["jobs"]) == 20
        exact = client.get(PREFIX + "/capture-jobs", params={"submissionKey": "submission-000"}).json()["jobs"]
        assert len(exact) == 1


@respx.mock
def test_configured_openai_capture_job_still_strictly_validates_and_only_proposes(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("AI_BASE_URL", "https://inference.example.invalid/v1")
    monkeypatch.setenv("AI_MODEL", "vision-example")
    secret = tmp_path / "key"
    secret.write_text("disposable-provider-secret")
    monkeypatch.setenv("AI_API_KEY_FILE", str(secret))
    cfg = Config.load()
    cfg.state, cfg.uploads = tmp_path / "api.db", tmp_path / "uploads"
    fake = FakeClients()
    app = create_app(cfg, fake)
    route = respx.post("https://inference.example.invalid/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"items":[{"name":"Box","unapproved":true}]}'}}]}),
            httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": answer().model_dump_json()}}]}),
        ],
    )
    with TestClient(app, base_url="https://inventory.test", headers={"Origin": cfg.origin}) as client:
        login = client.post(PREFIX + "/auth/login", json={"username": "alice", "password": "fixture"}).json()
        assert login["ai"] == {"chat": {"provider": "openai-compatible", "model": "vision-example"},
                               "vision": {"provider": "openai-compatible", "model": "vision-example"}}
        assert "inference.example" not in json.dumps(login) and "secret" not in json.dumps(login)
        client.headers.update({"X-CSRF-Token": login["csrfToken"], "X-Collection-ID": login["collectionId"]})
        job = terminal(client, submit(client).json())
        assert job["status"] == "succeeded" and route.call_count == 2
        assert route.calls[0].request.headers["Authorization"] == "Bearer disposable-provider-secret"
        assert "Bearer alice" not in route.calls[0].request.content.decode()
        assert fake.calls["create_item"] == 0


@pytest.mark.parametrize("name,value", [
    ("ASSISTANT_ANALYSIS_QUEUE_LIMIT", "0"),
    ("ASSISTANT_ANALYSIS_QUEUE_TIMEOUT_SECONDS", "601"),
    ("ASSISTANT_ANALYSIS_TIMEOUT_SECONDS", "3601"),
    ("ASSISTANT_CAPTURE_SYNC_TIMEOUT_SECONDS", "121"),
    ("AI_PROVIDER", "unsupported"),
])
def test_unsafe_analysis_configuration_fails_closed(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises((RuntimeError, ValueError)):
        Config.load()


def test_openai_configuration_requires_explicit_values_and_hides_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai-compatible")
    with pytest.raises(ValueError, match="requires"):
        Config.load()
    monkeypatch.setenv("AI_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("AI_MODEL", "vision")
    secret = tmp_path / "key"
    secret.write_text("disposable-private-token")
    monkeypatch.setenv("AI_API_KEY_FILE", str(secret))
    cfg = Config.load()
    assert cfg.provider_settings.kind == "openai-compatible"
    assert "disposable-private-token" not in repr(cfg)
    assert "disposable-private-token" not in repr(cfg.provider_settings)


@respx.mock
def test_role_specific_models_route_photos_only_to_vision(standalone):
    from dataclasses import replace
    manager, _, _ = standalone
    manager.cfg.ai_roles = replace(manager.cfg.ai_roles, chat=replace(manager.cfg.ai_roles.chat, model="chat-only"),
                                   vision=replace(manager.cfg.ai_roles.vision, model="vision-only"))
    client = VisionClient(manager.cfg)
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        body = answer().model_dump_json() if payload["model"] == "vision-only" else '{"kind":"search","query":"box"}'
        return httpx.Response(200, json={"message": {"content": body}})

    respx.post(manager.cfg.ollama + "/api/chat").mock(side_effect=respond)

    async def run():
        assert (await client.analyze(photo(), "" )).items[0].name == "Blue box"
        assert (await client.intent("Locate my blue box")).kind == "search"
        await client.aclose()

    asyncio.run(run())
    assert requests[0]["model"] == "vision-only" and requests[0]["messages"][0]["images"]
    assert requests[0]["options"] == {"temperature": 0, "num_ctx": 4096, "num_predict": 1200}
    assert requests[1]["model"] == "chat-only" and all("images" not in message for message in requests[1]["messages"])
    assert requests[1]["options"] == {"temperature": 0, "num_ctx": 4096, "num_predict": 600}


def test_shared_model_reuses_and_closes_provider_once(standalone, monkeypatch):
    manager, _, _ = standalone
    client = VisionClient(manager.cfg)
    assert client.chat_provider is client.provider
    closed = []
    original = client.provider.aclose

    async def close():
        closed.append(True)
        await original()

    monkeypatch.setattr(client.provider, "aclose", close)
    asyncio.run(client.aclose())
    assert closed == [True]


def test_model_override_fallbacks_and_explicit_role_configuration(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "legacy-model")
    cfg = Config.load()
    assert cfg.ai_chat_model == cfg.ai_vision_model == "legacy-model"
    monkeypatch.setenv("AI_MODEL", "shared-model")
    monkeypatch.setenv("AI_CHAT_MODEL", "chat-model")
    cfg = Config.load()
    assert cfg.ai_chat_model == "chat-model" and cfg.ai_vision_model == "shared-model"
    monkeypatch.setenv("AI_PROVIDER", "openai-compatible")
    monkeypatch.setenv("AI_BASE_URL", "https://inference.invalid/v1")
    monkeypatch.setenv("AI_VISION_MODEL", "vision-model")
    monkeypatch.delenv("AI_MODEL")
    cfg = Config.load()
    assert cfg.chat_provider_settings.model == "chat-model"
    assert cfg.vision_provider_settings.model == "vision-model"


@pytest.mark.parametrize("change", ["provider", "base_url", "vision_model"])
def test_queued_jobs_never_reroute_photos_after_configuration_change(standalone, change):
    from dataclasses import replace
    manager, session, calls = standalone
    row = manager.submit(session, "old-configuration-key", photo(), "", None)
    snapshot = json.loads(row["configuration"])
    assert set(snapshot) == {"provider", "model", "baseUrlFingerprint"}
    assert manager.cfg.ollama not in row["configuration"]
    if change == "provider":
        changed = replace(manager.cfg.ai_roles.vision, kind="openai-compatible", base_url="https://external.invalid/v1")
    elif change == "base_url":
        changed = replace(manager.cfg.ai_roles.vision, base_url="https://different-ollama.invalid")
    else:
        changed = replace(manager.cfg.ai_roles.vision, model="new-vision-model")
    manager.cfg.ai_roles = replace(manager.cfg.ai_roles, vision=changed)
    asyncio.run(manager.step())
    result = manager.public(manager.load(session, row["id"]))
    assert result["status"] == "manual" and result["warningCodes"] == ["analysis_configuration_changed"]
    assert calls == [] and next(manager.cfg.uploads.iterdir()).exists()


def test_unknown_legacy_job_configuration_migrates_fail_closed(standalone):
    manager, session, calls = standalone
    row = manager.submit(session, "pre-snapshot-key", photo(), "", None)
    manager.state.execute("ALTER TABLE capture_jobs DROP COLUMN configuration")
    State(manager.cfg.state, manager.cfg.instance_id)
    asyncio.run(manager.step())
    result = manager.public(manager.load(session, row["id"]))
    assert result["warningCodes"] == ["analysis_configuration_changed"] and calls == []


def test_access_change_during_inference_cancels_work_and_retains_no_ready_draft(standalone):
    manager, session, calls = standalone
    started, cancelled = [], []

    async def slow(*args):
        started.append(True)
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    manager.vision.analyze = slow
    row = manager.submit(session, "logout-running-key", photo(), "", None)

    async def run():
        task = asyncio.create_task(manager.step())
        while not started:
            await asyncio.sleep(.005)
        manager.state.execute("DELETE FROM sessions")
        await task

    asyncio.run(run())
    result = manager.public(manager.load(session, row["id"]))
    assert cancelled and result["status"] == "failed"
    assert result["result"] is None and result["error"]["code"] == "unauthorized"


def test_legacy_outer_deadline_covers_auth_without_bypassing_it(setup, monkeypatch):
    import assistant.main as main_module
    client, app, _, _ = setup
    monkeypatch.setattr(main_module, "LEGACY_REQUEST_TIMEOUT", .04)
    original = app.state.clients.homebox

    async def slow_auth(*args, **kwargs):
        await asyncio.sleep(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.state.clients, "homebox", slow_auth)
    started = time.monotonic()
    response = client.post(PREFIX + "/captures", files={"image": ("old.jpg", photo())})
    assert time.monotonic() - started < .5
    assert response.status_code == 504 and response.json()["error"]["code"] == "capture_request_timed_out"
    assert app.state.store.all("SELECT * FROM captures") == []


@pytest.mark.parametrize("separate", [False, True])
def test_endpoint_scoped_inference_serialization_and_independence(standalone, separate):
    from dataclasses import replace
    manager, _, _ = standalone
    roles = manager.cfg.ai_roles
    chat = replace(roles.chat, model="text-only",
                   base_url="http://chat-only.invalid:11434" if separate else roles.vision.base_url)
    manager.cfg.ai_roles = replace(roles, chat=chat)
    client = VisionClient(manager.cfg)
    active, peak, inputs = 0, 0, []

    async def generate(messages, schema, **kwargs):
        nonlocal active, peak
        inputs.append(kwargs["image_jpeg"])
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.04)
            return answer().model_dump_json() if kwargs["image_jpeg"] is not None else '{"kind":"search","query":"box"}'
        finally:
            active -= 1

    client.provider.generate = generate
    client.chat_provider.generate = generate

    async def run():
        await asyncio.gather(client.analyze(photo(), ""), client.intent("Find my box"))
        await client.aclose()

    asyncio.run(run())
    assert peak == (2 if separate else 1)
    assert sum(image is None for image in inputs) == 1


def test_endpoint_lock_is_shared_between_client_instances(standalone):
    manager, _, _ = standalone
    first, second = VisionClient(manager.cfg), VisionClient(manager.cfg)
    active, peak = 0, 0

    async def generate(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.03)
            return answer().model_dump_json()
        finally:
            active -= 1

    first.provider.generate = second.provider.generate = generate

    async def run():
        await asyncio.gather(first.analyze(photo(), ""), second.analyze(photo(), ""))
        await first.aclose()
        await second.aclose()

    asyncio.run(run())
    assert peak == 1


def test_chat_configuration_change_does_not_invalidate_queued_photo(standalone):
    from dataclasses import replace
    manager, session, calls = standalone
    row = manager.submit(session, "chat-only-change", photo(), "", None)
    manager.cfg.ai_roles = replace(manager.cfg.ai_roles, chat=replace(
        manager.cfg.ai_roles.chat, kind="openai-compatible", base_url="https://chat.invalid/v1", model="different-chat",
    ))
    asyncio.run(manager.step())
    assert manager.load(session, row["id"])["status"] == "succeeded" and len(calls) == 1


def test_fully_independent_roles_integrate_without_reading_unused_shared_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "unused-invalid-provider")
    monkeypatch.setenv("AI_BASE_URL", "unused-invalid-url")
    monkeypatch.setenv("AI_API_KEY_FILE", "/nonexistent-unused-shared-secret")
    monkeypatch.setenv("AI_CHAT_PROVIDER", "ollama")
    monkeypatch.setenv("AI_CHAT_BASE_URL", "http://chat.invalid:11434")
    monkeypatch.setenv("AI_CHAT_MODEL", "chat-model")
    monkeypatch.setenv("AI_VISION_PROVIDER", "openai-compatible")
    monkeypatch.setenv("AI_VISION_BASE_URL", "https://vision.invalid/v1")
    monkeypatch.setenv("AI_VISION_MODEL", "vision-model")
    chat_key, vision_key = tmp_path / "chat-key", tmp_path / "vision-key"
    chat_key.write_text("disposable-chat-key")
    vision_key.write_text("disposable-vision-key")
    monkeypatch.setenv("AI_CHAT_API_KEY_FILE", str(chat_key))
    monkeypatch.setenv("AI_VISION_API_KEY_FILE", str(vision_key))
    cfg = Config.load()
    assert cfg.ai_roles.chat.api_key == "disposable-chat-key"
    assert cfg.ai_roles.vision.api_key == "disposable-vision-key"
    assert cfg.ai_roles.chat.base_url == "http://chat.invalid:11434"
    assert cfg.ai_roles.vision.base_url == "https://vision.invalid/v1"
    assert "disposable-chat-key" not in repr(cfg) and "disposable-vision-key" not in repr(cfg)
