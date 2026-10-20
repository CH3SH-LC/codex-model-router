from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .store import Store, file_lock
from .integration import restore


def status(store):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(store.base_url() + "/status", timeout=2) as response:
            data = json.load(response)
            return data if data.get("service") == "codex-local-router" else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def start_service(store):
    with file_lock(store.root / "service-start.lock"):
        return _start_service(store)


def _start_service(store):
    running = status(store)
    if running:
        return running
    executable = Path(sys.executable)
    if executable.name.lower() == "python.exe":
        hidden = executable.with_name("pythonw.exe")
        if hidden.exists():
            executable = hidden
    script = Path(__file__).resolve().parents[1] / "main.py"
    with (store.root / "service-boot.log").open("ab") as log:
        child = subprocess.Popen(
            [str(executable), str(script), "--state-dir", str(store.root), "serve"],
            cwd=script.parent, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            # Discovery MCP clients are transient. Their process-tree cleanup
            # must not terminate routing for other conversations. The daemon
            # separately observes the desktop lifetime and its client leases.
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_BREAKAWAY_FROM_JOB) if os.name == "nt" else 0,
        )
    for _ in range(40):
        running = status(store)
        if running:
            return running
        if child.poll() is not None:
            break
        time.sleep(.15)
    raise RuntimeError("路由服务未启动；检查端口占用和 service-boot.log。未修改 Codex 配置。")


def lease(store, identity, release=False):
    request = urllib.request.Request(
        store.base_url() + "/lease",
        data=json.dumps({"id": identity, "release": release}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        return json.load(response)


def deactivate(store):
    return restore(store)


def probe_model(store, model):
    start_service(store)
    request = urllib.request.Request(
        store.base_url() + "/responses",
        data=json.dumps({"model": model, "input": "Reply with exactly OK.", "stream": False,
                         "max_output_tokens": 64}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=180) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            message = json.load(exc).get("error", {}).get("message", "上游错误")
        except Exception:
            message = "上游错误"
        raise RuntimeError(f"HTTP {exc.code}: {message}") from None
    if result.get("status") != "completed":
        raise RuntimeError("请求未完成：" + str(result.get("status")))
    text = "".join(p.get("text", "") for item in result.get("output", [])
                   for p in item.get("content", []) if p.get("type") == "output_text")
    return {"status": result["status"], "model": result.get("model"), "text": text[:500],
            "usage": result.get("usage")}
