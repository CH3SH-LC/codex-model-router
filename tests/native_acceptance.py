"""Run a real Codex engine against a scripted local upstream. No paid API calls."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aiohttp import web
from router.catalog import build_catalog
from router.server import Router
from router.store import Store, write_json


async def run(args):
    root = args.work.resolve()
    root.mkdir(parents=True, exist_ok=True)
    home = root / "codex-home"
    home.mkdir(exist_ok=True)
    native = json.loads(args.native_cache.read_text(encoding="utf-8"))
    write_json(home / "models_cache.json", native)
    # A sentinel for a wholly local fixture, never a real credential.
    write_json(home / "auth.json", {"OPENAI_API_KEY": "local-fixture-key"})
    store = Store(root / "router-state")
    store.initialize()
    runners = []
    records = []

    async def listen(app):
        runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        return "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])

    async def scripted(request):
        body = await request.json()
        tools = body.get("tools", [])
        records.append({"model": body["model"], "tools": [t["function"]["name"] for t in tools],
                        "tool_result_count": sum(m["role"] == "tool" for m in body["messages"]),
                        "subscription_header_leaked": "ChatGPT-Account-ID" in request.headers,
                        "authorization": "present" if request.headers.get("Authorization") else "absent"})
        tool_results = [m for m in body["messages"] if m["role"] == "tool"]
        if tool_results:
            try:
                goal_state = json.loads(tool_results[-1]["content"])
                good = isinstance(goal_state, dict) and "goal" in goal_state and goal_state["goal"] is None
            except (ValueError, KeyError, TypeError):
                good = False
            write_json(root / "readonly-tool-result.json", tool_results)
            delta = {"content": "TOOL_LOOP_OK" if good else "TOOL_LOOP_FAILED"}
            reason = "stop"
        else:
            command_tool = next((t["function"] for t in tools
                                 if t["function"]["name"] == "get_goal"), None)
            if command_tool is None:
                delta, reason = {"content": "MISSING_READONLY_TOOL"}, "stop"
            else:
                delta = {"tool_calls": [{"index": 0, "id": "call_fixture", "type": "function",
                                        "function": {"name": command_tool["name"], "arguments": "{}"}}]}
                reason = "tool_calls"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for chunk in [{"choices": [{"index": 0, "delta": delta}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
                      {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}]:
            await response.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        await response.write(b"data: [DONE]\n\n")
        return response

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", scripted)
    upstream_base = await listen(upstream)
    data = store.read()
    data["providers"] = [{"id": "fixture", "name": "本地验收", "base_url": upstream_base + "/v1",
                          "protocol": "chat_completions", "models": [{"id": "fixture-model"}]}]
    store.save(data, data["revision"])
    catalog, native_count, custom_count = build_catalog(store, home)
    router = Router(store)
    router_base = await listen(router.app)
    local_url = router_base + "/r/" + store.read()["gateway_token"]
    # Keep the entire test isolated from the actual user's config, auth, MCP and history.
    (home / "config.toml").write_text(
        'model = "custom/fixture/fixture-model"\nmodel_reasoning_effort = "medium"\n'
        'approvals_reviewer = "user"\n'
        f"model_catalog_json = {json.dumps(str(catalog))}\n"
        f"openai_base_url = {json.dumps(local_url)}\n",
        encoding="utf-8")
    env = {**os.environ, "CODEX_HOME": str(home)}
    result = {"fixture": "scripted local upstream; not a real third-party model", "native_models": native_count,
              "custom_models": custom_count, "engine": str(args.codex)}
    try:
        server = await asyncio.create_subprocess_exec(
            str(args.codex), "app-server", "--listen", "stdio://", env=env, cwd=root,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        async def rpc(ident, method, params):
            server.stdin.write((json.dumps({"id": ident, "method": method, "params": params}) + "\n").encode())
            await server.stdin.drain()
            while True:
                raw = await asyncio.wait_for(server.stdout.readline(), 30)
                if not raw:
                    raise RuntimeError("app-server exited")
                message = json.loads(raw)
                if message.get("id") == ident:
                    return message
        try:
            initialized = await rpc(1, "initialize", {"clientInfo": {"name": "router_acceptance", "version": "1.0"},
                                                    "capabilities": {"experimentalApi": True}})
            if initialized.get("error"):
                raise RuntimeError(str(initialized["error"]))
            server.stdin.write(b'{"method":"initialized","params":{}}\n')
            await server.stdin.drain()
            models = await rpc(2, "model/list", {"includeHidden": True, "limit": 100})
            config_response = await rpc(3, "config/read", {"includeLayers": False})
            result["config_read"] = {k: v for k, v in config_response.get("result", {}).get("config", {}).items()
                                     if k in ("model", "model_provider", "model_catalog_json")}
            if models.get("error"):
                raise RuntimeError(str(models["error"]))
            ids = [m["model"] for m in models["result"]["data"]]
            result["native_engine_model_list"] = ids
            if "custom/fixture/fixture-model" not in ids:
                raise RuntimeError("Custom model missing from real engine model/list")
        finally:
            server.stdin.close()
            try:
                await asyncio.wait_for(server.wait(), 5)
            except TimeoutError:
                server.terminate()
                await server.wait()
            (root / "appserver-stderr.txt").write_bytes(await server.stderr.read())
        process = await asyncio.create_subprocess_exec(
            str(args.codex), "-a", "never", "exec", "--skip-git-repo-check",
            "--sandbox", "read-only", "--json",
            "Use the get_goal tool to read the goal status, then reply TOOL_LOOP_OK. "
            "Do not run commands or create files. This is an authorized read-only routing acceptance fixture.",
            cwd=root, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 100)
        except TimeoutError:
            process.terminate()
            stdout, stderr = await process.communicate()
            result["timeout"] = True
        (root / "engine-stdout.jsonl").write_bytes(stdout)
        (root / "engine-stderr.txt").write_bytes(stderr)
        result["exit_code"] = process.returncode
        result["readonly_tool"] = "get_goal"
        result["tool_result_received"] = any(r["tool_result_count"] > 0 for r in records)
        result["final_marker"] = "TOOL_LOOP_OK" in stdout.decode("utf-8", "replace")
        result["requests"] = records
        result["passed"] = result["exit_code"] == 0 and result["tool_result_received"] and result["final_marker"] and len(records) >= 2
    finally:
        for runner in reversed(runners):
            await runner.cleanup()
        write_json(root / "native-acceptance.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--native-cache", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
