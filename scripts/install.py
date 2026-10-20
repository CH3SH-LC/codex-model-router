"""Prepare the runtime and register the in-app plugin; never enable routing."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import venv

ROOT = Path(__file__).resolve().parents[1]
VERSION = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]


def discover_codex(explicit=None, local_app_data=None):
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ValueError("--codex must point to the installed Codex engine executable.")
        return path
    base = Path(local_app_data or os.environ["LOCALAPPDATA"]) / "OpenAI/Codex/bin"
    candidates = list(base.glob("*/codex.exe")) if base.exists() else []
    if not candidates:
        raise ValueError("Codex desktop engine not found. Open Codex once, or pass --codex PATH.")
    return max(candidates, key=lambda p: p.stat().st_mtime_ns)


def runtime_path(local_app_data=None):
    return Path(local_app_data or os.environ["LOCALAPPDATA"]) / "CodexRouter/runtimes" / VERSION


def prepare_runtime(destination, wheelhouse=None):
    destination = Path(destination).resolve()
    marker = destination / ".codex-router-runtime"
    lock = ROOT / "requirements.lock.txt"
    digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    if destination.exists() and any(destination.iterdir()) and not marker.exists():
        raise ValueError("Refusing to modify an existing unowned runtime directory.")
    destination.mkdir(parents=True, exist_ok=True)
    marker.write_text("pending\n", encoding="utf-8")
    python = destination / "Scripts/python.exe"
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(destination)
    command = [str(python), "-m", "pip", "install", "--disable-pip-version-check",
               "--only-binary=:all:", "-r", str(lock)]
    if wheelhouse is not None:
        command += ["--no-index", "--find-links", str(Path(wheelhouse).resolve())]
    subprocess.run(command, check=True)
    subprocess.run([str(python), "-c", "import aiohttp, tomlkit, zstandard"], check=True)
    marker.write_text(digest + "\n", encoding="utf-8")
    return python


class Engine:
    """Bounded JSON-RPC client. Keep stderr separate from the protocol stream."""
    def __init__(self, command, codex_home, cwd=ROOT):
        self.messages = queue.Queue()
        self.sequence = 0
        env = dict(os.environ, CODEX_HOME=str(Path(codex_home).resolve()))
        self.proc = subprocess.Popen(
            [str(p) for p in command], cwd=cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        def collect():
            for line in self.proc.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
            self.messages.put({"eof": True})
        self.reader = threading.Thread(target=collect, daemon=True)
        self.reader.start()

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def call(self, method, params, timeout=60):
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params}
        self.proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                message = self.messages.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError(f"Codex timed out during {method}; installation was not confirmed.") from None
            if message.get("eof"):
                raise RuntimeError(f"Codex exited during {method}; installation was not confirmed.")
            if message.get("id") != self.sequence:
                continue
            if "error" in message:
                raise RuntimeError(f"Codex rejected {method}: {message['error'].get('message', 'RPC error')}")
            return message["result"]

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        self.reader.join(timeout=1)
        self.proc.stdout.close()


def register_plugin(executable, codex_home):
    client = Engine([executable, "app-server", "--listen", "stdio://"], codex_home)
    try:
        client.call("initialize", {
            "clientInfo": {"name": "codex_model_router_setup", "version": VERSION},
            "capabilities": {"experimentalApi": True}})
        client.notify("initialized", {})
        client.call("marketplace/add", {"source": str(ROOT)})
        descriptor = {"marketplacePath": str(ROOT / ".agents/plugins/marketplace.json"),
                      "pluginName": "codex-router"}
        client.call("plugin/install", descriptor)
        result = client.call("plugin/read", descriptor)
        detail = result.get("plugin", result)
        plugin = detail.get("summary", detail)
        if not plugin.get("installed") or not plugin.get("enabled"):
            raise RuntimeError("Codex did not confirm an installed, enabled plugin.")
        return {"plugin": "codex-router@codex-model-router", "version": VERSION,
                "installed": True, "enabled": True}
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, help="Explicit desktop engine executable")
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--runtime-dir", type=Path, help="Testing/managed deployment override")
    parser.add_argument("--wheelhouse", type=Path, help="Offline directory of pinned wheels")
    parser.add_argument("--runtime-only", action="store_true", help="Prepare Python; install through Codex UI separately")
    parser.add_argument("--check", action="store_true", help="Show prerequisites without writing files or installing")
    args = parser.parse_args()
    if os.name != "nt" or sys.version_info[:2] != (3, 13):
        parser.error("This release requires Windows and CPython 3.13.")
    executable = discover_codex(args.codex) if not args.runtime_only else None
    destination = args.runtime_dir or runtime_path()
    if args.check:
        print(json.dumps({"version": VERSION, "engine": str(executable) if executable else None,
                          "runtime": str(destination), "codex_home": str(args.codex_home)}, indent=2))
        return 0
    prepare_runtime(destination, args.wheelhouse)
    if args.runtime_only:
        print("Runtime prepared. Install the plugin from its local marketplace in Codex.")
    else:
        result = register_plugin(executable, args.codex_home)
        print(json.dumps(result, indent=2))
        print("Reopen Codex, open Model Router settings, and enable routing there.")
    if args.runtime_dir:
        print("Custom runtime: set CODEX_ROUTER_RUNTIME to this directory before starting Codex.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
