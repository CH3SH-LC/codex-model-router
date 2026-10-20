"""Codex-owned stdio MCP App: native settings, sidebar and conversation panel."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import secrets
import sys

from . import __version__
from .catalog import build_catalog
from .integration import install, restore
from .manager import lease, probe_model, start_service, status
from .store import alias, protect, validate_provider

URI = "ui://codex-router/settings.html"
EMPTY = {"type": "object", "additionalProperties": False}
APP_ONLY = {"ui": {"visibility": ["app"]}}
SETTINGS_SCHEMA = {
    "type": "object", "properties": {
        "enabled": {"type": "boolean", "title": "启用订阅与自定义模型路由",
                    "description": "保留原生登录和默认模型；停用后新启动的客户端恢复原生直连。"}
    }
}


def tool(name, title, properties=None, required=None, readonly=False, public=False):
    return {"name": name, "title": title, "description": title,
            "inputSchema": {"type": "object", "properties": properties or {},
                            "required": required or [], "additionalProperties": False},
            "annotations": {"readOnlyHint": readonly, "destructiveHint": False,
                            "openWorldHint": name == "probe_provider"},
            "_meta": {} if public else copy.deepcopy(APP_ONLY)}


def tools():
    result = [
        tool("open_router", "模型路由", readonly=True, public=True),
        tool("read_state", "读取路由配置", readonly=True),
        tool("save_provider", "保存提供商", {
            "session": {"type": "string"}, "revision": {"type": "integer"},
            "provider": {"type": "object"}, "api_key": {"type": "string"},
            "clear_key": {"type": "boolean"}}, ["session", "revision", "provider"]),
        tool("delete_provider", "删除提供商", {
            "session": {"type": "string"}, "revision": {"type": "integer"},
            "id": {"type": "string"}}, ["session", "revision", "id"]),
        tool("probe_provider", "测试模型连接", {
            "session": {"type": "string"}, "model": {"type": "string"}}, ["session", "model"]),
        tool("set_routing", "切换模型路由", {
            "session": {"type": "string"}, "enabled": {"type": "boolean"}}, ["session", "enabled"]),
        tool("settings_read", "读取插件设置", readonly=True),
        tool("settings_update", "更新插件设置", {
            "set": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                    "minProperties": 1, "additionalProperties": False}}, ["set"]),
    ]
    result[0]["description"] = "在 Codex 应用内打开模型路由设置。用户在面板中填写自己的 Base URL、API Key 和模型；不要要求用户把密钥发到聊天中。"
    result[0]["_meta"] = {
        "ui": {"resourceUri": URI},
        "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
    }
    result[-2]["outputSchema"] = {
        "type": "object", "properties": {
            "schema": {"type": "object"}, "values": {"type": "object"},
            "layout": {"type": "array", "items": {"type": "object"}}},
        "required": ["schema", "values"],
    }
    result[-1]["outputSchema"] = {
        "type": "object", "properties": {"values": {"type": "object"}}, "required": ["values"]}
    return result


class App:
    def __init__(self, store, codex_home):
        self.store, self.home = store, Path(codex_home)
        self.session = secrets.token_urlsafe(32)

    def enabled(self):
        return (self.store.root / "integration.json").exists()

    def state(self):
        data = self.store.read()
        providers = []
        for original in data["providers"]:
            item = {k: copy.deepcopy(v) for k, v in original.items() if k != "key_dpapi"}
            item["has_key"] = bool(original.get("key_dpapi"))
            providers.append(item)
        running = status(self.store)
        return {"revision": data["revision"], "providers": providers,
                "enabled": self.enabled(), "running": running is not None,
                "requests": running.get("recent", []) if running else [],
                "version": __version__, "windows_autostart": False}

    def private_result(self, text="已读取路由配置。", extra=None):
        return {"content": [{"type": "text", "text": text}],
                "_meta": {"router/state": self.state(), "router/session": self.session,
                          **({"router/result": extra} if extra else {})}}

    def require_session(self, args):
        if not secrets.compare_digest(str(args.get("session", "")), self.session):
            raise ValueError("面板连接已失效，请重新打开应用内的模型路由。")

    def set_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("路由开关必须为布尔值。")
        if enabled:
            start_service(self.store)
            install(self.store, self.home)
        else:
            restore(self.store)

    async def call(self, name, args):
        if name in ("open_router", "read_state"):
            return await asyncio.to_thread(self.private_result)
        if name == "settings_read":
            return {"content": [], "structuredContent": {
                "schema": SETTINGS_SCHEMA, "values": {"enabled": self.enabled()},
                "layout": [{"kind": "group", "title": "模型路由", "items": [
                    {"kind": "property", "property": "enabled"},
                    {"kind": "tool", "tool": "open_router",
                     "description": "在应用内配置 Base URL、密钥和多个模型，并查看连接状态。"}]}]}}
        if name == "settings_update":
            values = args.get("set", {})
            if set(values) != {"enabled"}:
                raise ValueError("仅支持更新路由开关。")
            await asyncio.to_thread(self.set_enabled, values["enabled"])
            return {"content": [], "structuredContent": {"values": {"enabled": self.enabled()}}}
        self.require_session(args)
        if name == "save_provider":
            data = self.store.read()
            source = args["provider"]
            if not isinstance(source, dict):
                raise ValueError("提供商配置无效。")
            # Never accept DPAPI blobs or listener settings from the UI.
            allowed = ("id", "name", "base_url", "protocol", "models", "enabled")
            provider = {k: copy.deepcopy(source[k]) for k in allowed if k in source}
            validate_provider(provider)
            old = next((p for p in data["providers"] if p["id"] == provider["id"]), None)
            key = args.get("api_key", "")
            if not isinstance(key, str) or len(key) > 16384:
                raise ValueError("API Key 格式无效。")
            provider["key_dpapi"] = protect(key) if key else (
                "" if args.get("clear_key") else (old or {}).get("key_dpapi", ""))
            data["providers"] = [p for p in data["providers"] if p["id"] != provider["id"]] + [provider]
            self.store.save(data, args["revision"])
            build_catalog(self.store, self.home)
            return await asyncio.to_thread(self.private_result, "已保存。模型目录变更需要重开客户端。")
        if name == "delete_provider":
            data = self.store.read()
            if not any(p["id"] == args["id"] for p in data["providers"]):
                raise ValueError("提供商已不存在，请刷新。")
            data["providers"] = [p for p in data["providers"] if p["id"] != args["id"]]
            self.store.save(data, args["revision"])
            build_catalog(self.store, self.home)
            return await asyncio.to_thread(self.private_result, "已删除提供商。")
        if name == "probe_provider":
            if args["model"] not in self.store.routes():
                raise ValueError("仅可测试已启用的自定义模型。")
            result = await asyncio.to_thread(probe_model, self.store, args["model"])
            return await asyncio.to_thread(self.private_result, "模型连接测试完成。", result)
        if name == "set_routing":
            await asyncio.to_thread(self.set_enabled, args["enabled"])
            return await asyncio.to_thread(self.private_result, "已更新路由，重开客户端后生效。")
        raise ValueError("未知操作。")

    async def dispatch(self, method, params):
        capabilities = {
            "tools": {}, "resources": {},
            "experimental": {"openai/settings": {"readTool": "settings_read", "updateTool": "settings_update"}},
        }
        if method == "initialize":
            return {"protocolVersion": "2025-11-25",
                    "serverInfo": {"name": "codex-router", "title": "模型路由", "version": __version__},
                    "capabilities": capabilities,
                    "instructions": "管理应用内模型路由。密钥由用户在插件界面填写，不得索取或回显密钥。"}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tools()}
        if method in ("resources/list", "resources/templates/list"):
            return {"resources": [{"uri": URI, "name": "模型路由", "mimeType": "text/html;profile=mcp-app"}]} if method == "resources/list" else {"resourceTemplates": []}
        if method == "resources/read":
            if params.get("uri") != URI:
                raise ValueError("未知界面资源。")
            return {"contents": [{"uri": URI, "mimeType": "text/html;profile=mcp-app",
                "text": (Path(__file__).parent / "ui.html").read_text(encoding="utf-8"),
                "_meta": {"ui": {"prefersBorder": False, "csp": {"connectDomains": [], "resourceDomains": []}},
                          "openai/ui": {"availableDisplayModes": ["inline", "fullscreen"],
                                        "preferredDisplayMode": "fullscreen"}}}]}
        if method == "tools/call":
            try:
                return await self.call(params["name"], params.get("arguments", {}))
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
                return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        raise ValueError("不支持此 MCP 方法。")


async def run(store, codex_home):
    app = App(store, codex_home)
    identity = secrets.token_hex(16)
    # Plugin initialization completes only after routing is ready for inference.
    await asyncio.to_thread(start_service, store)
    await asyncio.to_thread(lease, store, identity)
    async def heartbeat():
        while True:
            await asyncio.sleep(12)
            try:
                await asyncio.to_thread(lease, store, identity)
            except OSError:
                await asyncio.to_thread(start_service, store)
                await asyncio.to_thread(lease, store, identity)
    pulse = asyncio.create_task(heartbeat())
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not line:
                break
            message = None
            try:
                message = json.loads(line)
                if "id" not in message:
                    continue
                result = await app.dispatch(message["method"], message.get("params", {}))
                reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
            except Exception:
                reply = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                         "error": {"code": -32600, "message": "MCP 请求无效。"}}
            # MCP stdio is UTF-8 regardless of the Windows console code page.
            sys.stdout.buffer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()
    finally:
        pulse.cancel()
        await asyncio.gather(pulse, return_exceptions=True)
        try:
            await asyncio.to_thread(lease, store, identity, True)
        except OSError:
            pass
