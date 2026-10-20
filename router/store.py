from __future__ import annotations

import base64
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from contextlib import contextmanager
from urllib.parse import urlsplit


def atomic_write(path: Path, data: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path: Path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


@contextmanager
def file_lock(path: Path):
    """Serialize local editors; no stale lock remains after process exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ValueError("另一窗口正在保存配置，请稍后重试。")
                time.sleep(.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def default_state_dir():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexRouter"


class Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_byte))]


def _dpapi(raw: bytes, decrypt=False) -> bytes:
    if os.name != "nt":
        raise RuntimeError("API Key 加密存储要求 Windows DPAPI；本版本不保存明文密钥。")
    buf = ctypes.create_string_buffer(raw)
    src = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    dest = Blob()
    api = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = api.CryptUnprotectData if decrypt else api.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(Blob)]
    fn.restype = ctypes.c_bool
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dest)):
        raise RuntimeError("Windows 凭据加密/解密失败；请在保存密钥的 Windows 账户下运行。")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        kernel.LocalFree(dest.data)


def protect(value: str):
    return base64.b64encode(_dpapi(value.encode())).decode() if value else ""


def unprotect(value: str):
    return _dpapi(base64.b64decode(value), True).decode() if value else ""


def validate_provider(p):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", p.get("id", "")):
        raise ValueError("提供商 ID 必须为小写字母开头的 1–40 位字母、数字、下划线或连字符。")
    url = urlsplit(p.get("base_url", ""))
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Base URL 需要完整地址，不能包含凭据、查询参数或片段。")
    if url.scheme != "https" and not (
        url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")
    ):
        raise ValueError("外部 Base URL 必须为 HTTPS；本机服务可以使用 HTTP。")
    if url.path.rstrip("/").endswith(("/responses", "/chat/completions", "/models")):
        raise ValueError("Base URL 应填写 API 根地址（例如 /v1），不要包含 /responses 或 /chat/completions。")
    if p.get("protocol") not in ("responses", "chat_completions"):
        raise ValueError("协议必须是 responses 或 chat_completions。")
    if not isinstance(p.get("models"), list) or not p["models"]:
        raise ValueError("至少添加一个模型。")
    seen = set()
    for m in p["models"]:
        name = m.get("id", "")
        if not name or len(name) > 200 or any(ord(c) < 33 for c in name):
            raise ValueError("模型 ID 不能为空、不能包含空白或控制字符。")
        if name in seen:
            raise ValueError("同一提供商不能重复添加相同模型 ID。")
        seen.add(name)
        if not 4096 <= int(m.get("context_window", 128000)) <= 4000000:
            raise ValueError("上下文窗口须为 4096–4000000。")
        if not 256 <= int(m.get("max_output_tokens", 8192)) <= 1000000:
            raise ValueError("最大输出 token 数须为 256–1000000。")
        if not set(m.get("reasoning_efforts", ["low", "medium", "high"])).issubset(
            {"low", "medium", "high", "xhigh"}
        ):
            raise ValueError("自定义模型推理档位支持 low / medium / high / xhigh。")


def alias(provider, model):
    # Keep API model IDs, including slashes, recoverable without ambiguous splitting.
    return f"custom/{provider}/{model}"


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "settings.json"

    def initialize(self):
        if not self.path.exists():
            write_json(self.path, {
                "version": 1, "revision": 1, "port": 18789,
                "gateway_token": secrets.token_urlsafe(32), "providers": [],
            })
        return self.read()

    def read(self):
        data = load_json(self.path)
        if data.get("version") != 1 or not 1024 <= int(data["port"]) <= 65535:
            raise ValueError("路由配置版本或端口无效。")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", data.get("gateway_token", "")):
            raise ValueError("路由凭据无效。")
        ids = set()
        for p in data["providers"]:
            validate_provider(p)
            if p["id"] in ids:
                raise ValueError("提供商 ID 重复。")
            ids.add(p["id"])
        return data

    def save(self, data, expected_revision):
        with file_lock(self.root / "settings.lock"):
            return self._save(data, expected_revision)

    def _save(self, data, expected_revision):
        current = self.read()
        if current["revision"] != expected_revision:
            raise ValueError("配置已被另一个窗口修改，请刷新后重试。")
        data = copy.deepcopy(data)
        for p in data["providers"]:
            validate_provider(p)
        data["revision"] = current["revision"] + 1
        # Listener identity is immutable through ordinary provider editing.
        data["port"] = current["port"]
        data["gateway_token"] = current["gateway_token"]
        write_json(self.path, data)
        return data

    def routes(self):
        result = {}
        for p in self.read()["providers"]:
            if not p.get("enabled", True):
                continue
            for m in p["models"]:
                result[alias(p["id"], m["id"])] = (p, m)
        return result

    def base_url(self):
        d = self.read()
        return f"http://127.0.0.1:{d['port']}/r/{d['gateway_token']}"
