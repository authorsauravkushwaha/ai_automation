"""Trusted subprocess bootstrap, invoked by absolute path under Python -I -S.

Do not import HINATA or project code here. Apply POSIX limits before exec; on
Windows wait for the parent to assign its configured Job Object before releasing
any project/native command. A missing gate times out closed.
"""
from __future__ import annotations

import os
import sys
import time


def main() -> None:
    memory_bytes, cpu_seconds = int(sys.argv[1]), int(sys.argv[2])
    gate = sys.argv[3]
    command = sys.argv[4:]
    if not command:
        raise SystemExit("Missing supervised command")
    if os.name == "nt":
        deadline = time.monotonic() + 15
        while not os.path.isfile(gate):
            if time.monotonic() >= deadline:
                raise SystemExit("Windows resource job was not assigned; refusing execution")
            time.sleep(0.01)
        # Windows exec spawns a replacement and exits this process: the supervisor
        # would observe a dead bootstrap and close its kill-on-close Job Object
        # while the command is still running. Keep this assigned process alive
        # until its child exits; children inherit the job and output handles.
        import subprocess
        raise SystemExit(subprocess.call(command))
    else:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    os.execvpe(command[0], command, os.environ)


if __name__ == "__main__":
    main()
