"""Lightweight spawn targets; semaphore signals remain safe after child death."""
import time


STARTUP_TIMEOUT = 60
HOLD_TIMEOUT = 30


def process_state(process):
    return (f"{process.name}(pid={process.pid}, alive={process.is_alive()}, "
            f"exitcode={process.exitcode})")


def wait_for_process_signal(process, signal, timeout, phase):
    deadline = time.monotonic() + timeout
    while True:
        if signal.acquire(timeout=min(.1, max(0, deadline - time.monotonic()))):
            assert process.is_alive(), f"{phase}: child exited after signaling; {process_state(process)}"
            return
        assert process.exitcode is None, f"{phase}: child exited before signaling; {process_state(process)}"
        assert time.monotonic() < deadline, f"{phase}: timed out after {timeout:.2f}s; {process_state(process)}"


def start_ready_processes(workers, timeout=STARTUP_TIMEOUT):
    deadline = time.monotonic() + timeout
    for process, _ in workers:
        process.start()
    for process, ready in workers:
        wait_for_process_signal(process, ready, max(0, deadline - time.monotonic()), "bootstrap")


def join_clean_processes(processes, timeout=15):
    deadline = time.monotonic() + timeout
    for process in processes:
        process.join(max(0, deadline - time.monotonic()))
    assert all(process.exitcode == 0 for process in processes), (
        "graceful process exit: " + "; ".join(map(process_state, processes))
    )


def stop_processes(processes, timeout=3):
    started = [process for process in processes if process.pid is not None]
    for process in started:
        if process.is_alive():
            process.terminate()
    deadline = time.monotonic() + timeout
    for process in started:
        process.join(max(0, deadline - time.monotonic()))
    for process in started:
        if process.is_alive():
            process.kill()
    deadline = time.monotonic() + timeout
    for process in started:
        process.join(max(0, deadline - time.monotonic()))
    assert not any(process.is_alive() for process in started), "; ".join(map(process_state, started))


def receive_process_messages(workers, kind, deadline, phase):
    from multiprocessing.connection import wait

    pending = dict((connection, process) for process, connection in workers)
    messages = {}
    phases = {}
    while pending:
        states = "; ".join(f"{process_state(process)} phase={phases.get(process.pid, 'not entered')}"
                           for process, _ in workers)
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"{phase}: shared deadline expired; {states}"
        watched = [process for process, connection in workers if kind != "result" or connection in pending]
        ready = wait([*pending, *(process.sentinel for process in watched)],
                     timeout=min(.1, remaining))
        for process, connection in workers:
            if connection not in pending and kind != "result" and process.sentinel in ready:
                process.join(timeout=0)
                raise AssertionError(f"{phase}: child exited after {kind}; {process_state(process)}")
        for connection, process in list(pending.items()):
            if connection in ready or connection.poll():
                try:
                    message = connection.recv()
                except EOFError:
                    process.join(timeout=.1)
                    raise AssertionError(f"{phase}: child closed its pipe without {kind}; {process_state(process)}")
                if message["kind"] == "progress":
                    phases[process.pid] = message["phase"]
                    continue
                assert message["kind"] == kind, (
                    f"{phase}: unexpected child message; {process_state(process)}; {message!r}"
                )
                messages[process.pid] = message
                del pending[connection]
            elif process.sentinel in ready:
                process.join(timeout=0)
                raise AssertionError(f"{phase}: child exited without {kind}; {process_state(process)}")
    return [messages[process.pid] for process, _ in workers]


def initial_conversation_turn(cfg, session, inventory, body, start, release, output, leader,
                              startup_timeout=STARTUP_TIMEOUT):
    phase = "bootstrap"
    original_flock = None
    try:
        output.send({"kind": "progress", "phase": "bootstrap imports"})
        import asyncio
        import fcntl
        import os
        from collections import Counter
        from copy import deepcopy
        from types import SimpleNamespace
        from assistant.actions import Actions
        from assistant.conversations import Conversations
        from assistant.schemas import Message
        from assistant.state import State

        output.send({"kind": "progress", "phase": "bootstrap state initialization"})
        store = State(cfg.state, cfg.instance_id)
        calls = Counter()

        async def homebox(method, path, current, **kwargs):
            assert method == "GET" and path == "/groups/all"
            assert current["credential"] == session["credential"]
            return deepcopy(inventory["groups"])

        async def tool(current, name, arguments=None):
            assert current["credential"] == session["credential"]
            assert current["collection"] == session["collection"]
            calls[name] += 1
            for expected_name, expected_arguments, response in inventory["tools"]:
                if (name, arguments) == (expected_name, expected_arguments):
                    return deepcopy(response)
            raise AssertionError(f"Unexpected inventory call: {name} {arguments!r}")

        # The parent obtains these exact responses from the existing FakeClients.
        # Do not import its HTTP app/test module in each spawned interpreter.
        fake = SimpleNamespace(homebox=homebox, tool=tool, calls=calls)
        service = Conversations(cfg, store, fake, Actions(cfg, store, fake), None)
        session_lock = store.locks / ("chat-session-" + session["id"])
        original_flock = fcntl.flock
        held, reported = False, False

        def observed_flock(fd, operation):
            nonlocal held, reported
            try:
                expected, actual = session_lock.stat(), os.fstat(fd)
                is_session = (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino)
            except FileNotFoundError:
                is_session = False
            try:
                value = original_flock(fd, operation)
            except BlockingIOError:
                if is_session and not leader and not reported:
                    output.send({"kind": "contended", "pid": os.getpid()})
                    reported = True
                raise
            if is_session:
                held = bool(operation & fcntl.LOCK_EX)
            return value

        original_homebox = fake.homebox

        async def paused_homebox(*args, **kwargs):
            nonlocal phase
            if args[1] == "/groups/all":
                assert held, "Initial turn reached scope without holding its real session flock"
                phase = "waiting for concurrent contenders"
                output.send({"kind": "entered", "pid": os.getpid()})
                while not release.acquire(block=False):
                    await asyncio.sleep(.02)
                phase = "request"
            return await original_homebox(*args, **kwargs)

        fcntl.flock = observed_flock
        if leader:
            fake.homebox = paused_homebox
        output.send({"kind": "ready", "pid": os.getpid()})
        phase = "waiting for start"
        if not start.acquire(timeout=startup_timeout + cfg.chat_timeout + 15):
            raise TimeoutError("Parent did not start the initial conversation turn")
        phase = "request"
        value = asyncio.run(service.handle(session, Message(**body)))
        phase = "result"
        output.send({"kind": "result", "pid": os.getpid(), "value": value, "calls": dict(fake.calls)})
    except BaseException as exc:
        import traceback
        try:
            output.send({"kind": "error", "phase": phase, "error": type(exc).__name__,
                         "message": str(exc), "traceback": traceback.format_exc()})
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise
    finally:
        if original_flock is not None:
            fcntl.flock = original_flock
        output.close()


def hold_collection_lock(path, instance, collection, ready, start, acquired, release, contended=None):
    import asyncio
    import fcntl
    from pathlib import Path
    from assistant.state import State

    store = State(Path(path), instance)
    original_flock = fcntl.flock

    def observed_flock(fd, operation):
        try:
            return original_flock(fd, operation)
        except BlockingIOError:
            if contended is not None:
                contended.release()
            raise

    async def run():
        async with store.collection_lock(collection):
            acquired.release()
            if not await asyncio.to_thread(release.acquire, True, HOLD_TIMEOUT):
                raise TimeoutError("Parent did not release the collection lock")

    fcntl.flock = observed_flock
    try:
        ready.release()
        if not start.acquire(timeout=STARTUP_TIMEOUT + HOLD_TIMEOUT):
            raise TimeoutError("Parent did not start the collection lock attempt")
        asyncio.run(run())
    finally:
        fcntl.flock = original_flock


def hold_model_lock(lock_directory, lock_name, ready, start, acquired, release):
    import fcntl
    from pathlib import Path

    with (Path(lock_directory) / lock_name).open("a") as lock:
        ready.release()
        if not start.acquire(timeout=STARTUP_TIMEOUT + HOLD_TIMEOUT):
            raise TimeoutError("Parent did not start the model lock attempt")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        acquired.release()
        if not release.acquire(timeout=HOLD_TIMEOUT):
            raise TimeoutError("Parent did not release the model lock")
