import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import ClientSession, web
import tomlkit
import zstandard

from router.catalog import build_catalog
from router.integration import install, restore
from router.protocol import ChatStream, ProtocolError, encode_event, to_chat
from router.server import Router
from router.store import Store, alias, load_json, protect, unprotect, validate_provider, write_json


def native_model():
    return {
        "slug": "gpt-fixture", "display_name": "Fixture native",
        "description": "Test fixture", "default_reasoning_level": "medium",
        "supported_reasoning_levels": [{"effort": "medium", "description": "medium"}],
        "shell_type": "unified_exec", "visibility": "list", "supported_in_api": True,
        "priority": 1, "availability_nux": None, "upgrade": None,
        "support_verbosity": False, "default_verbosity": None, "apply_patch_tool_type": None,
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "experimental_supported_tools": [], "input_modalities": ["text"],
    }


class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.store.initialize()
        self.home = self.root / "codex"
        self.home.mkdir()
        self.config = self.home / "config.toml"
        self.original = '# keep comment\nmodel = "gpt-fixture"\n[desktop]\nlocale = "zh-CN"\n'
        self.config.write_text(self.original, encoding="utf-8")
        write_json(self.home / "models_cache.json", {"models": [native_model()]})

    def tearDown(self):
        self.tmp.cleanup()

    def test_dpapi_roundtrip(self):
        value = "test-key-中文-123"
        encrypted = protect(value)
        self.assertNotIn(value, encrypted)
        self.assertEqual(unprotect(encrypted), value)
        with self.assertRaises(Exception):
            unprotect(encrypted[:-8] + "AAAAAAAA")

    def test_safe_install_restore_preserves_unrelated_edits(self):
        install(self.store, self.home)
        doc = tomlkit.parse(self.config.read_text(encoding="utf-8"))
        self.assertEqual(doc["model"], "gpt-fixture")
        self.assertEqual(doc["openai_base_url"], self.store.base_url())
        doc["desktop"]["theme"] = "dark"
        self.config.write_text(tomlkit.dumps(doc), encoding="utf-8")
        restore(self.store)
        after = self.config.read_text(encoding="utf-8")
        self.assertIn("# keep comment", after)
        self.assertIn('theme = "dark"', after)
        self.assertNotIn("openai_base_url", after)
        self.assertFalse((self.store.root / "integration.json").exists())

    def test_conflicting_config_not_overwritten(self):
        install(self.store, self.home)
        doc = tomlkit.parse(self.config.read_text(encoding="utf-8"))
        doc["openai_base_url"] = "https://different.example/v1"
        self.config.write_text(tomlkit.dumps(doc), encoding="utf-8")
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            restore(self.store)
        self.assertEqual(before, self.config.read_bytes())

    def test_prepared_receipt_can_recover_after_interrupted_install(self):
        install(self.store, self.home)
        receipt_path = self.store.root / "integration.json"
        receipt = load_json(receipt_path)
        receipt["state"] = "prepared"
        write_json(receipt_path, receipt)
        self.config.write_text(self.original, encoding="utf-8")
        self.assertEqual(install(self.store, self.home)["status"], "installed")
        self.assertEqual(load_json(receipt_path)["state"], "installed")
        restore(self.store)

    def test_concurrent_saves_do_not_lose_updates(self):
        from concurrent.futures import ThreadPoolExecutor
        data = self.store.read()
        def save():
            try:
                self.store.save(data, data["revision"])
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: save(), range(2)))
        self.assertEqual(sum(results), 1)

    def test_store_revision_conflict(self):
        a, b = self.store.read(), self.store.read()
        self.store.save(a, a["revision"])
        with self.assertRaises(ValueError):
            self.store.save(b, b["revision"])

    def test_external_http_and_credentials_rejected(self):
        p = {"id": "demo", "base_url": "http://external.example/v1", "protocol": "responses", "models": [{"id": "m"}]}
        with self.assertRaises(ValueError):
            validate_provider(p)
        p["base_url"] = "https://key@example.com/v1"
        with self.assertRaises(ValueError):
            validate_provider(p)

    def test_catalog_keeps_native_exact_and_custom_direct_tools(self):
        data = self.store.read()
        data["providers"] = [{"id": "demo", "name": "演示", "base_url": "https://example.com/v1",
                              "protocol": "responses", "models": [{"id": "org/model"}]}]
        self.store.save(data, data["revision"])
        path, n, c = build_catalog(self.store, self.home)
        models = load_json(path)["models"]
        self.assertEqual((n, c), (1, 1))
        self.assertEqual(models[0], native_model())
        self.assertEqual(models[1]["slug"], "custom/demo/org/model")
        self.assertEqual(models[1]["tool_mode"], "direct")
        self.assertFalse(models[1]["use_responses_lite"])

    def test_tool_namespace_and_history_translation(self):
        body = {"instructions": "test", "tools": [
            {"type": "namespace", "name": "functions", "tools": [
                {"type": "function", "name": "run", "parameters": {"type": "object"}},
                {"type": "custom", "name": "patch"}]}],
            "input": [{"role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
                      {"type": "function_call", "namespace": "functions", "name": "run", "call_id": "c1", "arguments": "{}"},
                      {"type": "function_call_output", "call_id": "c1", "output": "done"}]}
        out, mapping = to_chat(body, {"id": "upstream"})
        self.assertEqual(out["messages"][1]["role"], "system")
        self.assertEqual(out["messages"][-1]["tool_call_id"], "c1")
        name = out["tools"][0]["function"]["name"]
        self.assertEqual(mapping[name]["namespace"], "functions")
        self.assertEqual(out["messages"][2]["tool_calls"][0]["function"]["name"], name)

    def test_unsupported_items_are_not_silently_dropped(self):
        with self.assertRaises(ProtocolError):
            to_chat({"input": [{"type": "compaction", "encrypted_content": "x"}]}, {"id": "m"})
        with self.assertRaises(ProtocolError):
            to_chat({"tools": [{"type": "computer"}]}, {"id": "m"})

    def test_hosted_search_not_advertised_as_a_client_tool(self):
        out, mapping = to_chat({"tools": [{"type": "web_search"}], "input": "hello"}, {"id": "m"})
        self.assertNotIn("tools", out)
        self.assertEqual(mapping, {})

    def test_parallel_tool_stream_and_unicode(self):
        stream = ChatStream("custom/demo/m", {
            "run": {"name": "run", "namespace": None, "custom": False},
            "patch": {"name": "patch", "namespace": "functions", "custom": True}})
        events = stream.start()
        events += stream.chunk({"choices": [{"delta": {"content": "中文", "tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "run", "arguments": '{"x":'}},
            {"index": 1, "id": "b", "function": {"name": "patch", "arguments": '{"input":"abc'}},
        ]}}]})
        events += stream.chunk({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "1}"}},
            {"index": 1, "function": {"arguments": '"}'}},
        ]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}})
        events += stream.finish()
        result = events[-1]["response"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output"][0]["content"][0]["text"], "中文")
        self.assertEqual(result["output"][1]["arguments"], '{"x":1}')
        self.assertEqual(result["output"][2]["input"], "abc")
        self.assertEqual(result["usage"]["total_tokens"], 12)
        self.assertEqual([e["sequence_number"] for e in events], list(range(len(events))))

    def test_truncated_stream_cannot_claim_completed(self):
        stream = ChatStream("m", {})
        stream.chunk({"choices": [{"delta": {"content": "partial"}}]})
        with self.assertRaises(ProtocolError):
            stream.finish()

    def test_length_stop_is_incomplete(self):
        stream = ChatStream("m", {})
        stream.chunk({"choices": [{"delta": {"content": "partial"}, "finish_reason": "length"}]})
        self.assertEqual(stream.finish()[-1]["type"], "response.incomplete")


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state")
        self.store.initialize()
        self.calls = []
        self.runners = []
        self.custom_count = 0
        self.native_ws_headers = None

        async def upstream(request):
            if request.headers.get("Upgrade", "").lower() == "websocket":
                self.native_ws_headers = dict(request.headers)
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for msg in ws:
                    body = json.loads(msg.data)
                    if body.get("fixture_complete"):
                        await ws.send_json({"type": "response.completed", "response": {"id": "native_done", "status": "completed"}})
                    else:
                        await ws.send_str(msg.data)
                return ws
            body = await request.json()
            self.calls.append((request.path, dict(request.headers), body))
            if body["model"] == "bad-key":
                return web.json_response({"error": "secret-upstream-body"}, status=401)
            if request.path.endswith("/chat/completions"):
                self.custom_count += 1
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                chunks = [
                    {"choices": [{"index": 0, "delta": {"content": "路由"}}]},
                    {"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}},
                ]
                if body["model"] == "truncated":
                    chunks = chunks[:1]
                for chunk in chunks:
                    wire = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\r\n\r\n").encode()
                    for pos in range(0, len(wire), 3):
                        await response.write(wire[pos:pos + 3])
                await response.write(b"data: [DONE]\r\n\r\n")
                return response
            done = {"type": "response.completed", "response": {
                "id": "resp_mock", "object": "response", "model": body["model"],
                "status": "completed", "output": [{"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "OK"}]}]}}
            return web.Response(body=encode_event(done), content_type="text/event-stream")

        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{tail:.*}", upstream)
        self.upstream_url = await self.listen(upstream_app)
        config = self.store.read()
        config["providers"] = [
            {"id": "chat", "base_url": self.upstream_url + "/v1", "protocol": "chat_completions",
             "key_dpapi": protect("CUSTOM-KEY"), "models": [{"id": m} for m in ["model", "bad-key", "truncated"]]},
            {"id": "resp", "base_url": self.upstream_url + "/v1", "protocol": "responses",
             "key_dpapi": protect("RESP-KEY"), "models": [{"id": "model"}]},
        ]
        self.store.save(config, config["revision"])
        self.router = Router(self.store, native_base=self.upstream_url + "/native")
        base = await self.listen(self.router.app)
        self.base = base + "/r/" + self.store.read()["gateway_token"]
        self.client = ClientSession()
        self.sub_headers = {"Authorization": "Bearer SUBSCRIPTION-SECRET", "ChatGPT-Account-ID": "private-account",
                            "Cookie": "private-cookie", "X-Codex-Private": "private-value"}

    async def listen(self, app):
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.runners.append(runner)
        return "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])

    async def asyncTearDown(self):
        await self.client.close()
        for runner in reversed(self.runners):
            await runner.cleanup()
        self.tmp.cleanup()

    async def test_native_body_headers_preserved(self):
        body = {"model": "gpt-fixture", "input": [{"role": "user", "content": "hello"}], "stream": True,
                "client_metadata": {"special": "preserve"}, "service_tier": "priority"}
        async with self.client.post(self.base + "/responses", json=body, headers=self.sub_headers) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("response.completed", await r.text())
        path, headers, received = self.calls[-1]
        self.assertEqual(path, "/native/responses")
        self.assertEqual(body, received)
        self.assertEqual(headers["Authorization"], "Bearer SUBSCRIPTION-SECRET")

    async def test_zstd_native_requests(self):
        data = zstandard.ZstdCompressor().compress(json.dumps({"model": "gpt-fixture", "input": "hi"}).encode())
        async with self.client.post(self.base + "/responses", data=data,
                                    headers={**self.sub_headers, "Content-Encoding": "zstd"}) as r:
            self.assertEqual(r.status, 200)
            await r.read()
        self.assertNotIn("Content-Encoding", self.calls[-1][1])
        self.assertEqual(self.calls[-1][2]["input"], "hi")

    async def test_custom_credentials_isolated_and_usage_translated(self):
        body = {"model": "custom/chat/model", "input": "hi", "stream": False, "service_tier": "priority",
                "client_metadata": {"account": "PRIVATE"}}
        async with self.client.post(self.base + "/responses", json=body, headers=self.sub_headers) as r:
            result = await r.json()
            self.assertEqual(r.status, 200, result)
        _, headers, received = self.calls[-1]
        self.assertEqual(headers["Authorization"], "Bearer CUSTOM-KEY")
        self.assertFalse(any(k.lower() in ("cookie", "chatgpt-account-id", "x-codex-private") for k in headers))
        self.assertNotIn("service_tier", received)
        self.assertNotIn("client_metadata", received)
        self.assertEqual(result["output"][0]["content"][0]["text"], "路由OK")
        self.assertEqual(result["usage"]["total_tokens"], 5)

    async def test_responses_mapping_uses_own_key(self):
        async with self.client.post(self.base + "/responses", json={"model": "custom/resp/model", "input": "x", "stream": False},
                                    headers=self.sub_headers) as r:
            result = await r.json()
        self.assertEqual(self.calls[-1][1]["Authorization"], "Bearer RESP-KEY")
        self.assertEqual(self.calls[-1][2]["model"], "model")
        self.assertEqual(result["model"], "custom/resp/model")

    async def test_unknown_custom_alias_does_not_fallback(self):
        async with self.client.post(self.base + "/responses", json={"model": "custom/missing/x", "input": "x"}) as r:
            self.assertEqual(r.status, 400)
        self.assertEqual(self.calls, [])

    async def test_401_retained_and_error_body_not_exposed(self):
        async with self.client.post(self.base + "/responses", json={"model": "custom/chat/bad-key", "input": "x"}) as r:
            self.assertEqual(r.status, 401)
            self.assertNotIn("secret-upstream-body", await r.text())

    async def test_incomplete_sse_has_no_completed(self):
        async with self.client.post(self.base + "/responses", json={"model": "custom/chat/truncated", "input": "x"}) as r:
            text = await r.text()
        self.assertIn("router_upstream_stream_error", text)
        self.assertNotIn("response.completed", text)

    async def test_origin_and_invalid_token_denied(self):
        async with self.client.post(self.base + "/responses", json={}, headers={"Origin": "https://example.com"}) as r:
            self.assertEqual(r.status, 403)
        bad = self.base.rsplit("/", 1)[0] + "/wrong/responses"
        async with self.client.post(bad, json={}) as r:
            self.assertEqual(r.status, 401)

    async def test_parallel_independent_routes(self):
        async def call(model):
            async with self.client.post(self.base + "/responses", json={"model": model, "input": "x", "stream": False}) as r:
                text = await r.text()
                self.assertEqual(r.status, 200, text)
        await asyncio.gather(*(call(m) for m in ["gpt-fixture", "custom/chat/model", "custom/resp/model"] * 5))
        self.assertEqual(len(self.calls), 15)
        for path, headers, body in self.calls:
            if path.startswith("/native"):
                self.assertEqual(body["model"], "gpt-fixture")
            else:
                self.assertEqual(body["model"], "model")

    async def test_hot_disable_fails_closed(self):
        data = self.store.read()
        data["providers"][0]["enabled"] = False
        self.store.save(data, data["revision"])
        async with self.client.post(self.base + "/responses", json={"model": "custom/chat/model", "input": "x"}) as r:
            self.assertEqual(r.status, 400)
        self.assertEqual(self.calls, [])

    async def test_native_websocket_transparent(self):
        async with self.client.ws_connect(self.base + "/responses", headers=self.sub_headers) as ws:
            first = {"type": "response.create", "model": "gpt-fixture", "input": []}
            await ws.send_json(first)
            self.assertEqual(await ws.receive_json(), first)
        self.assertEqual(self.native_ws_headers["Authorization"], "Bearer SUBSCRIPTION-SECRET")

    async def test_native_completed_websocket_records_each_turn_before_close(self):
        async with self.client.ws_connect(self.base + "/responses", headers=self.sub_headers) as ws:
            await ws.send_json({"type": "response.create", "model": "gpt-fixture", "input": [], "fixture_complete": True})
            self.assertEqual((await ws.receive_json())["type"], "response.completed")
            self.assertEqual(len(self.router.recent), 1)
            await ws.send_json({"type": "response.create", "model": "gpt-fixture", "input": [], "fixture_complete": True})
            self.assertEqual((await ws.receive_json())["type"], "response.completed")
            self.assertEqual(len(self.router.recent), 2)
        for _ in range(20):
            if self.router.recent:
                break
            await asyncio.sleep(.01)
        self.assertEqual(self.router.recent[0]["status"], 200)

    async def test_custom_websocket_prewarm_and_continuation(self):
        async with self.client.ws_connect(self.base + "/responses", headers=self.sub_headers) as ws:
            await ws.send_json({"type": "response.create", "model": "custom/chat/model", "generate": False, "input": []})
            prewarm = await ws.receive_json()
            self.assertEqual(prewarm["type"], "response.completed")
            self.assertEqual(self.custom_count, 0)
            self.assertEqual(len(self.router.recent), 0)
            await ws.send_json({"type": "response.create", "model": "custom/chat/model", "input": "first"})
            while True:
                event = await asyncio.wait_for(ws.receive_json(), 5)
                if event.get("type") == "response.completed":
                    first_id = event["response"]["id"]
                    break
            await ws.send_json({"type": "response.create", "model": "custom/chat/model",
                                "previous_response_id": first_id,
                                "input": [{"role": "user", "content": "second"}]})
            while True:
                event = await asyncio.wait_for(ws.receive_json(), 5)
                if event.get("type") == "response.completed":
                    break
            self.assertEqual(len(self.router.recent), 2)
        messages = self.calls[-1][2]["messages"]
        self.assertEqual(messages[0]["content"], "first")
        self.assertEqual(messages[1]["content"], "路由OK")
        self.assertEqual(messages[-1]["content"], "second")


if __name__ == "__main__":
    unittest.main(verbosity=2)
