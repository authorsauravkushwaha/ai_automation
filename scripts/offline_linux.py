#!/usr/bin/env python3
"""Run HINATA and optionally its Ollama server in a private Linux network namespace.

This is an opt-in launcher for Linux, not a sandbox for filesystem or IPC access.
No privileged host firewall rules are modified.
"""
from __future__ import annotations

import argparse
import errno
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hinata.config import ConfigError, load_config  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Launch HINATA in a Linux network namespace with loopback only")
    result.add_argument("--config", help="HINATA config file")
    result.add_argument("--with-ollama", action="store_true", help="Start a separate Ollama server inside the namespace")
    result.add_argument("--check", action="store_true", help="Verify loopback works and external routing is blocked, then exit")
    result.add_argument("--inside", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("hinata_args", nargs=argparse.REMAINDER, help="HINATA arguments after -- (default: interactive CLI)")
    return result


def probe() -> None:
    """Fail closed if loopback is unavailable or external connections are routable."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=2):
            accepted, _ = listener.accept()
            accepted.close()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        result = sock.connect_ex(("198.51.100.1", 443))
        if result not in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
            raise RuntimeError(f"External IPv4 connection did not fail as unroutable (errno {result})")
    if socket.has_ipv6:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            result = sock.connect_ex(("2001:db8::1", 443))
            if result not in {errno.ENETUNREACH, errno.EHOSTUNREACH}:
                raise RuntimeError(f"External IPv6 connection did not fail as unroutable (errno {result})")


def run_inside(args: argparse.Namespace) -> int:
    if not sys.platform.startswith("linux") or not Path("/proc/self/ns/net").exists():
        raise RuntimeError("Linux network namespaces are required")
    # Refuse direct --inside use, which otherwise could give a false isolation claim.
    parent_ns = os.environ.get("HINATA_PARENT_NETNS", "")
    this_ns = os.readlink("/proc/self/ns/net")
    if not parent_ns or parent_ns == this_ns:
        raise RuntimeError("A new network namespace was not established")
    ip = shutil.which("ip")
    if not ip:
        raise RuntimeError("The ip command is required to bring up isolated loopback")
    subprocess.run([ip, "link", "set", "lo", "up"], check=True, timeout=10)
    probe()
    print("Offline namespace: loopback available; external IPv4/IPv6 probe unroutable.", flush=True)
    if args.check:
        return 0
    rest = args.hinata_args
    if rest and rest[0] == "--":
        rest = rest[1:]
    if any(arg == "--config" or arg.startswith("--config=") for arg in rest):
        raise RuntimeError("Pass --config to the offline launcher before --, not to HINATA")
    config = load_config(args.config)
    if config.ollama.auto_pull:
        raise RuntimeError("Disable ollama.auto_pull for an offline launch")
    server = None
    try:
        if args.with_ollama:
            executable = shutil.which("ollama")
            if not executable:
                raise RuntimeError("Ollama executable not found; install it and the models during connected setup")
            parsed = urlsplit(config.ollama.host)
            if parsed.path not in {"", "/"} or parsed.port is None:
                raise RuntimeError("Isolated Ollama requires a loopback host with an explicit port and no URL path")
            address = parsed.netloc
            environment = os.environ.copy()
            environment["OLLAMA_HOST"] = address
            server = subprocess.Popen([executable, "serve"], env=environment, stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError("Isolated Ollama exited during startup; check the installed runtime and configuration")
                try:
                    with socket.create_connection((parsed.hostname, parsed.port or 11434), timeout=0.5):
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                raise RuntimeError("Isolated Ollama did not start within 20 seconds")
        command = [sys.executable, "-m", "hinata"]
        if args.config:
            command.extend(["--config", args.config])
        return subprocess.run([*command, *rest], cwd=ROOT, check=False).returncode
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.inside:
            return run_inside(args)
        if not sys.platform.startswith("linux"):
            raise RuntimeError("This launcher requires Linux; Windows is not isolated by this tool")
        unshare = shutil.which("unshare")
        if not unshare:
            raise RuntimeError("unshare is not installed")
        environment = os.environ.copy()
        environment["HINATA_PARENT_NETNS"] = os.readlink("/proc/self/ns/net")
        # All options precede the HINATA argument separator.
        command = [unshare, "--user", "--map-root-user", "--net", "--fork", sys.executable, str(Path(__file__).resolve()), "--inside"]
        if args.config:
            command.extend(["--config", args.config])
        if args.with_ollama:
            command.append("--with-ollama")
        if args.check:
            command.append("--check")
        command.extend(args.hinata_args)
        return subprocess.run(command, env=environment, check=False).returncode
    except (ConfigError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Offline launcher: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
