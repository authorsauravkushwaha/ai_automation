"""Bounded pipe draining with a deadline and process-tree termination callback."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any


class OutputLimitError(RuntimeError):
    pass


def kill_process_group(process: subprocess.Popen[Any]) -> None:
    """Kill the session group even when its leader has already exited."""
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()  # Callers with Windows jobs must also terminate the job.
    except (OSError, ProcessLookupError):
        pass


def capture_bounded(process: Any, *, timeout: float, kill: Callable[[], None],
                    max_bytes: int = 1_000_000) -> tuple[str, str]:
    """Drain both pipes without unbounded communicate buffers or post-kill waits.

    Retained output is capped across both streams; exceeding the quota kills the
    tree and fails the operation, rather than reporting a truncated success.
    Reader threads are daemonized so an escaped descendant holding a pipe cannot
    trap shutdown. Such readers own/close their pipe and retain no additional data.
    """
    if max_bytes < 1 or timeout <= 0:
        raise ValueError("Output quota and timeout must be positive")
    lock = threading.Lock()
    overflow = threading.Event()
    stop = threading.Event()
    output = [bytearray(), bytearray()]
    errors: list[Exception] = []

    def drain(stream: Any, index: int) -> None:
        try:
            raw_stream = getattr(stream, "buffer", stream)
            while not stop.is_set():
                chunk = raw_stream.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                with lock:
                    room = max_bytes - sum(len(buffer) for buffer in output)
                    output[index].extend(chunk[:room])
                    if len(chunk) > room:
                        overflow.set()
                        break
        except Exception as exc:
            errors.append(exc)
        finally:
            stream.close()

    readers = [threading.Thread(target=drain, args=(stream, i), daemon=True, name="hinata-output")
               for i, stream in enumerate((process.stdout, process.stderr)) if stream is not None]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    failure: Exception | None = None
    try:
        while True:
            if overflow.is_set():
                failure = OutputLimitError(f"Process exceeded combined output quota ({max_bytes} bytes)")
                break
            if process.poll() is not None and not any(reader.is_alive() for reader in readers):
                break
            if time.monotonic() >= deadline:
                failure = subprocess.TimeoutExpired(getattr(process, "args", "process"), timeout)
                break
            overflow.wait(min(0.02, max(0, deadline - time.monotonic())))
    except BaseException:
        stop.set()
        kill()
        raise
    finally:
        if failure or errors:
            stop.set()
            kill()
        for reader in readers:
            reader.join(timeout=0.2)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            kill()
    if failure:
        raise failure
    if errors:
        raise OutputLimitError("Could not capture subprocess output safely") from errors[0]
    return tuple(bytes(buffer).decode("utf-8", errors="replace") for buffer in output)  # type: ignore[return-value]


def checked_output(argv: list[str], *, timeout: float = 10, max_bytes: int = 1_000_000,
                   **kwargs: Any) -> str:
    """Bounded replacement for developer Git check_output (always decoded text)."""
    kwargs.pop("text", None)
    kwargs.pop("stderr", None)
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, start_new_session=os.name != "nt", **kwargs)
    stdout, stderr = capture_bounded(proc, timeout=timeout, max_bytes=max_bytes,
                                    kill=lambda: kill_process_group(proc))
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, argv, output=stdout, stderr=stderr)
    return stdout
