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
