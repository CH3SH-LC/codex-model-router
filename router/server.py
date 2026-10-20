from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
import contextlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import zlib

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
import zstandard

from . import __version__
from .protocol import ChatStream, ProtocolError, encode_event, sse_events, to_chat, to_responses, uid
from .store import Store, load_json, unprotect, write_json


OFFICIAL_BASE = "https://chatgpt.com/backend-api/codex"
HOP = {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
       "te", "trailer", "transfer-encoding", "upgrade", "content-length",
       "accept-encoding"}
ALLOWED_NATIVE = {"/models", "/responses", "/responses/compact", "/responses/lite",
                  "/responses/count_tokens"}
MAX_BODY = 32 * 1024 * 1024


def error_response(message, status=400, code="router_error"):
    return web.json_response({"error": {"message": message, "type": "router_error", "code": code}}, status=status)


def native_headers(headers, websocket=False):
    excluded = set(HOP)
    excluded.update(v.strip().lower() for v in headers.get("Connection", "").split(","))
    if websocket:
        excluded.update(k.lower() for k in headers if k.lower().startswith("sec-websocket-"))
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


def custom_headers(provider):
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json",
               "User-Agent": f"CodexLocalRouter/{__version__}"}
    key = unprotect(provider.get("key_dpapi", ""))
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


class Router:
    def __init__(self, store: Store, *, native_base=OFFICIAL_BASE, session=None):
        self.store = store
        self.identity = store.read()
        self.native_base = native_base.rstrip("/")
        self.session = session
        self.owns_session = session is None
        self.recent = deque(maxlen=60)
        self.active = 0
        self.leases = {}
        self.last_used = time.monotonic()
        self.log = logging.getLogger("codex-router")
        self.app = web.Application(client_max_size=MAX_BODY, handler_args={"auto_decompress": False})
        self.app.router.add_get("/health", self.health)
        self.app.router.add_route("*", "/r/{token}/{tail:.*}", self.handle)
        self.app.on_startup.append(self.start)
        self.app.on_cleanup.append(self.cleanup)

    async def start(self, app):
        if self.session is None:
            self.session = ClientSession(
                timeout=ClientTimeout(total=None, connect=30, sock_read=300),
                auto_decompress=True, trust_env=True, read_bufsize=1024 * 1024,
            )

    async def cleanup(self, app):
        if self.owns_session and self.session:
            await self.session.close()

    async def health(self, request):
        return web.json_response({"service": "codex-local-router", "version": __version__})

    def record(self, route, model, status, started, transport="http"):
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "route": route,
                 "model": str(model)[:240], "status": status,
                 "duration_ms": round((time.monotonic() - started) * 1000),
                 "transport": transport}
        self.recent.appendleft(entry)
        self.log.info(json.dumps(entry, ensure_ascii=False))

    def route(self, model):
        if not isinstance(model, str) or not model:
            raise ProtocolError("请求缺少 model。")
        routes = self.store.routes()
        if model in routes:
            return routes[model]
        if model.startswith("custom/"):
            raise ProtocolError("此自定义模型未配置或已停用，不会自动转发到订阅。")
        if model.startswith(("gpt-", "codex-")):
            return None
        # Only explicit native catalog entries may use other names.
        path = self.store.root / "native-models.json"
        if path.exists() and any(m["slug"] == model for m in load_json(path)["models"]):
            return None
        raise ProtocolError("未知模型；请先在配置窗口添加。")

    async def handle(self, request):
        if not hmac.compare_digest(request.match_info["token"], self.identity["gateway_token"]):
            return error_response("无效的本地路由凭据。", 401)
        # Prevent browser cross-origin requests. Desktop model clients have no Origin.
        if request.headers.get("Origin"):
            return error_response("此端点仅用于本机模型客户端。", 403)
        tail = "/" + request.match_info["tail"]
        if tail == "/status" and request.method == "GET":
            return web.json_response({"service": "codex-local-router", "pid": os.getpid(),
                                      "version": __version__, "managed_by": "codex-plugin",
                                      "active": self.active, "recent": list(self.recent),
                                      "revision": self.store.read()["revision"]})
        if tail == "/lease" and request.method == "POST":
            value = await request.json()
            identity = value.get("id", "")
            if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{32}", identity):
                return error_response("无效的插件连接。")
            if value.get("release"):
                self.leases.pop(identity, None)
            else:
                self.leases[identity] = time.monotonic()
            self.last_used = time.monotonic()
            return web.json_response({"connected": not value.get("release", False)})
        if tail not in ALLOWED_NATIVE:
            return error_response("未实现的模型端点。", 404)
        self.last_used = time.monotonic()
        if request.headers.get("Upgrade", "").lower() == "websocket":
            if tail != "/responses":
                return error_response("仅 /responses 支持 WebSocket。", 400)
            return await self.websocket(request)
        if tail == "/models":
            if request.method != "GET":
                return error_response("Method not allowed", 405)
            path = self.store.root / "model-catalog.json"
            if path.exists():
                return web.json_response(load_json(path))
            return await self.native_http(request, tail, b"")
        if request.method != "POST":
            return error_response("Method not allowed", 405)
        started = time.monotonic()
        model, route_id, status = "", "unresolved", 500
        self.active += 1
        try:
            raw = await request.read()
            encoding = request.headers.get("Content-Encoding", "").lower()
            if encoding == "zstd":
                raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=MAX_BODY)
            elif encoding in ("gzip", "deflate"):
                decoder = zlib.decompressobj(31 if encoding == "gzip" else 15)
                raw = decoder.decompress(raw, MAX_BODY + 1)
                if not decoder.eof:
                    raise ProtocolError("压缩请求无效或解压后超过大小限制。")
            elif encoding not in ("", "identity"):
                return error_response("不支持此 Content-Encoding。", 415)
            if len(raw) > MAX_BODY:
                raise ProtocolError("请求解压后超过大小限制。")
            body = json.loads(raw)
            model = body.get("model", "")
            route = self.route(model)
            route_id = route[0]["id"] if route else "subscription"
            if route is None:
                result = await self.native_http(request, tail, raw)
                status = result.status
                return result
            if tail != "/responses" and route[0]["protocol"] == "responses":
                provider, selected = route
                payload = to_responses(body, selected)
                payload.pop("stream", None)
                async with self.session.post(provider["base_url"].rstrip("/") + tail,
                                             json=payload, headers=custom_headers(provider),
                                             allow_redirects=False) as upstream:
                    result = await upstream.read()
                    status = upstream.status
                    return web.Response(body=result, status=status,
                                        headers={"Content-Type": upstream.headers.get("Content-Type", "application/json")})
            if tail != "/responses":
                # A custom compaction call must never be sent to an unrelated
                # provider, nor converted into a fake successful compaction.
                return error_response("该自定义模型仅支持 /responses。压缩历史后请新建会话。", 400)
            provider, selected = route
            result = await self.custom_http(request, body, provider, selected)
            status = result.get("router_status", result.status)
            return result
        except (ValueError, KeyError, TypeError, ProtocolError) as exc:
            status = 400
            return error_response(str(exc))
        except (asyncio.CancelledError, ConnectionResetError):
            status = 499
            raise
        except Exception:
            status = 502
            # Deliberately omit exception text: upstream URLs and headers may contain secrets.
            return error_response("上游连接失败或流中断；请检查地址、协议和服务状态。", 502)
        finally:
            self.active -= 1
            self.record(route_id, model, status, started)

    async def native_http(self, request, tail, raw):
        headers = native_headers(request.headers)
        # request.read() has already decompressed supported content encodings.
        headers.pop("Content-Encoding", None)
        headers.pop("content-encoding", None)
        async with self.session.request(request.method, self.native_base + tail,
                                        params=request.query, data=raw or None,
                                        headers=headers, allow_redirects=False) as upstream:
            out_headers = {k: v for k, v in upstream.headers.items()
                           if k.lower() not in HOP | {"content-encoding"}}
            response = web.StreamResponse(status=upstream.status, headers=out_headers)
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
            except Exception:
                # Once HTTP headers have been sent, terminate the stream. Do not
                # append a second HTTP error response or forge response.completed.
                if request.transport:
                    request.transport.close()
                raise ConnectionResetError()
            return response

    async def custom_events(self, body, provider, selected):
        if body.get("previous_response_id") and provider["protocol"] == "chat_completions":
            raise ProtocolError("Chat Completions 的 HTTP 请求不能引用 previous_response_id；需要完整历史。")
        if provider["protocol"] == "responses":
            payload = to_responses(body, selected)
            payload["stream"] = True
            async with self.session.post(provider["base_url"].rstrip("/") + "/responses",
                                         json=payload, headers=custom_headers(provider),
                                         allow_redirects=False) as upstream:
                if upstream.status != 200:
                    raise UpstreamError(upstream.status)
                terminal = False
                async for data in sse_events(upstream.content):
                    if data == "[DONE]":
                        break
                    event = json.loads(data)
                    if event.get("response"):
                        event["response"]["model"] = body["model"]
                    terminal |= event.get("type") in ("response.completed", "response.failed", "response.incomplete")
                    yield event
                if not terminal:
                    raise ProtocolError("上游 Responses 流提前结束。")
            return
        payload, mapping = to_chat(body, selected)
        stream = ChatStream(body["model"], mapping)
        async with self.session.post(provider["base_url"].rstrip("/") + "/chat/completions",
                                     json=payload, headers=custom_headers(provider),
                                     allow_redirects=False) as upstream:
            if upstream.status != 200:
                raise UpstreamError(upstream.status)
            for event in stream.start():
                yield event
            async for data in sse_events(upstream.content):
                if data == "[DONE]":
                    break
                for event in stream.chunk(json.loads(data)):
                    yield event
            for event in stream.finish():
                yield event

    async def custom_http(self, request, body, provider, selected):
        iterator = self.custom_events(body, provider, selected)
        # Pull before preparing headers so auth/model failures retain their HTTP status.
        try:
            first = await anext(iterator)
        except UpstreamError as exc:
            return error_response(f"提供商返回 HTTP {exc.status}；请检查 API Key、模型权限和协议。",
                                  exc.status if 400 <= exc.status <= 599 else 502, "upstream_error")
        if body.get("stream", True) is False:
            terminal = first.get("response") if first.get("type") in (
                "response.completed", "response.incomplete", "response.failed") else None
            async for event in iterator:
                if event.get("type") in ("response.completed", "response.incomplete", "response.failed"):
                    terminal = event["response"]
            if terminal is None:
                raise ProtocolError("没有终结响应。")
            return web.json_response(terminal)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                                "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        await response.prepare(request)
        try:
            await response.write(encode_event(first))
            async for event in iterator:
                await response.write(encode_event(event))
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception:
            response["router_status"] = 502
            await response.write(encode_event({
                "type": "error", "code": "router_upstream_stream_error",
                "message": "上游流中断或协议不兼容；未生成完成响应。",
            }))
        finally:
            await iterator.aclose()
        await response.write_eof()
        return response

    async def websocket(self, request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=MAX_BODY)
        await ws.prepare(request)
        started = time.monotonic()
        model, route_id, status = "", "unresolved", 500
        completed = False
        recorded = False
        should_record = True
        self.active += 1
        try:
            first = await asyncio.wait_for(ws.receive(), 30)
            if first.type != WSMsgType.TEXT:
                raise ProtocolError("WebSocket 首条消息必须是 response.create JSON。")
            body = json.loads(first.data)
            should_record = body.get("generate", True) is not False
            if body.get("type") != "response.create":
                raise ProtocolError("需要 response.create。")
            model = body.get("model", "")
            route = self.route(model)
            route_id = route[0]["id"] if route else "subscription"
            if route is None:
                url = self.native_base + "/responses"
                async with self.session.ws_connect(
                    url, headers=native_headers(request.headers, websocket=True),
                    heartbeat=30, max_msg_size=MAX_BODY,
                ) as upstream:
                    await upstream.send_str(first.data)

                    async def send_up():
                        nonlocal completed, recorded, started, model, should_record
                        async for msg in ws:
                            if msg.type == WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                if data.get("type") == "response.create":
                                    completed = False
                                    recorded = False
                                    started = time.monotonic()
                                    model = data.get("model", model)
                                    should_record = data.get("generate", True) is not False
                                if data.get("model") and self.route(data["model"]) is not None:
                                    raise ProtocolError("同一连接不可跨提供商切换，请重新打开会话。")
                                await upstream.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                await upstream.send_bytes(msg.data)

                    async def send_down():
                        nonlocal completed, recorded
                        async for msg in upstream:
                            if msg.type == WSMsgType.TEXT:
                                with contextlib.suppress(ValueError, TypeError):
                                    event_type = json.loads(msg.data).get("type")
                                    if event_type == "response.completed":
                                        completed = True
                                    if event_type in ("response.completed", "response.incomplete", "response.failed") and should_record:
                                        self.record(route_id, model, {"response.completed": 200, "response.incomplete": 206,
                                                    "response.failed": 502}[event_type], started, "websocket")
                                        recorded = True
                                await ws.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                await ws.send_bytes(msg.data)

                    tasks = {asyncio.create_task(send_up()), asyncio.create_task(send_down())}
                    try:
                        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
            else:
                history = OrderedDict()
                current = body
                while not ws.closed:
                    route = self.route(model)
                    completed = False
                    recorded = False
                    started = time.monotonic()
                    should_record = current.get("generate", True) is not False
                    if current.get("type") != "response.create":
                        raise ProtocolError("自定义连接仅支持 response.create；取消请关闭连接。")
                    if current.get("model", model) != model:
                        raise ProtocolError("切换模型需要建立新连接。")
                    full = dict(current, model=model, stream=True)
                    full.pop("type", None)
                    prior = full.pop("previous_response_id", None)
                    incoming = full.get("input", [])
                    if isinstance(incoming, str):
                        incoming = [{"role": "user", "content": incoming}]
                    full["input"] = incoming
                    if prior:
                        if prior not in history:
                            raise ProtocolError("previous_response_id 已失效；请发送完整历史或新建会话。")
                        full["input"] = history[prior] + incoming
                    if len(json.dumps(full, ensure_ascii=False).encode()) > MAX_BODY:
                        raise ProtocolError("会话历史超过路由限制，请新建会话。")
                    if full.pop("generate", True) is False:
                        # Codex prewarms its WS connection without asking the model
                        # to generate. Acknowledge locally; make no paid request.
                        ready = {"id": uid("resp"), "object": "response", "created_at": int(time.time()),
                                 "status": "completed", "model": model, "output": [],
                                 "error": None, "incomplete_details": None}
                        await ws.send_json({"type": "response.completed", "response": ready})
                        history[ready["id"]] = full.get("input", [])
                        msg = await ws.receive()
                        if msg.type != WSMsgType.TEXT:
                            break
                        current = json.loads(msg.data)
                        continue
                    finished = None

                    async def emit_custom():
                        nonlocal finished, completed, recorded
                        async for event in self.custom_events(full, *route):
                            if event.get("type") == "response.completed":
                                finished = event["response"]
                                completed = True
                            if event.get("type") in ("response.completed", "response.incomplete", "response.failed"):
                                self.record(route_id, model, {"response.completed": 200, "response.incomplete": 206,
                                            "response.failed": 502}[event["type"]], started, "websocket")
                                recorded = True
                            await ws.send_json(event)

                    generating = asyncio.create_task(emit_custom())
                    receiving = asyncio.create_task(ws.receive())
                    try:
                        done, _ = await asyncio.wait({generating, receiving}, return_when=asyncio.FIRST_COMPLETED)
                        if receiving in done and generating not in done and finished is None:
                            msg = receiving.result()
                            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                                break
                            raise ProtocolError("此连接正在生成；取消请关闭连接，不支持同时提交两轮。")
                        await generating
                        if finished:
                            history[finished["id"]] = full.get("input", []) + finished.get("output", [])
                            while len(history) > 8 or len(json.dumps(history).encode()) > MAX_BODY:
                                history.popitem(last=False)
                        msg = await receiving
                        if msg.type != WSMsgType.TEXT:
                            break
                        current = json.loads(msg.data)
                    finally:
                        generating.cancel()
                        receiving.cancel()
                        await asyncio.gather(generating, receiving, return_exceptions=True)
            status = 200
        except (ProtocolError, ValueError, UpstreamError) as exc:
            status = 400
            if not ws.closed:
                await ws.send_json({"type": "error", "error": {
                    "type": "invalid_request_error", "code": "router_error", "message": str(exc)}})
        except (asyncio.CancelledError, ConnectionResetError):
            status = 200 if completed else 499
        except Exception:
            status = 502
            if not ws.closed:
                with contextlib.suppress(Exception):
                    await ws.send_json({"type": "error", "error": {
                        "code": "router_upstream_error", "message": "上游 WebSocket 连接失败。"}})
        finally:
            self.active -= 1
            if not recorded and should_record:
                self.record(route_id, model, status, started, "websocket")
            await ws.close()
        return ws


class UpstreamError(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(f"提供商返回 HTTP {status}")


def serve(store: Store):
    store.initialize()
    store.root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("codex-router")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(store.root / "requests.jsonl", maxBytes=2 * 1024 * 1024,
                                  backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    router = Router(store)
    # Avoid framework access logs: capability paths must never appear in logs.
    async def started(app):
        write_json(store.root / "service.json", {"pid": os.getpid(), "port": store.read()["port"],
                                                "version": __version__, "started": time.time()})
    router.app.on_startup.append(started)
    async def run():
        from .desktop_lifetime import DesktopLifetime
        desktop = DesktopLifetime()
        runner = web.AppRunner(router.app, access_log=None, handler_cancellation=True)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", store.read()["port"]).start()
            while True:
                await asyncio.sleep(5)
                now = time.monotonic()
                router.leases = {key: seen for key, seen in router.leases.items() if now - seen < 45}
                if not desktop.alive() and not router.leases and not router.active and now - router.last_used > 90:
                    break
        finally:
            await runner.cleanup()
            desktop.close()
    asyncio.run(run())
