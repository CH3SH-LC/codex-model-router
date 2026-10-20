import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from router.mcp_app import App, URI, tools
from router.store import Store, write_json, unprotect
from tests.test_router import native_model


class AppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")
        self.store.initialize()
        self.home = self.root / "codex"
        self.home.mkdir()
        (self.home / "config.toml").write_text('model = "gpt-fixture"\n', encoding="utf-8")
        write_json(self.home / "models_cache.json", {"models": [native_model()]})
        self.app = App(self.store, self.home)
        self.status = patch("router.mcp_app.status", return_value=None)
        self.status.start()
        self.provider = {"id": "fixture", "name": "Test", "protocol": "chat_completions",
                         "base_url": "https://example.test/v1", "models": [{"id": "fixture-model"}]}

    def tearDown(self):
        self.status.stop()
        self.tmp.cleanup()

    async def save(self, **extra):
        return await self.app.call("save_provider", {
            "session": self.app.session, "revision": self.store.read()["revision"],
            "provider": self.provider, **extra})

    async def test_native_settings_and_sidebar_entrypoints(self):
        initialized = await self.app.dispatch("initialize", {})
        self.assertEqual(initialized["capabilities"]["experimental"]["openai/settings"]["readTool"], "settings_read")
        inventory = tools()
        visible = [t for t in inventory if t["_meta"].get("ui", {}).get("visibility") != ["app"]]
        self.assertEqual([t["name"] for t in visible], ["open_router"])
        self.assertEqual(visible[0]["_meta"]["openai/ui"]["entrypoints"], [{"type": "global"}, {"type": "thread"}])
        settings = await self.app.call("settings_read", {})
        self.assertEqual(settings["structuredContent"]["values"], {"enabled": False})
        resource = await self.app.dispatch("resources/read", {"uri": URI})
        self.assertIn("ui/initialize", resource["contents"][0]["text"])
        self.assertNotIn("gateway_token", resource["contents"][0]["text"])

    async def test_key_encryption_redaction_and_preserve(self):
        result = await self.save(api_key="fixture-secret-KEY")
        serialized = json.dumps(result)
        self.assertNotIn("fixture-secret-KEY", serialized)
        self.assertNotIn("key_dpapi", serialized)
        self.assertNotIn(self.store.read()["gateway_token"], serialized)
        self.assertTrue(result["_meta"]["router/state"]["providers"][0]["has_key"])
        await self.save(api_key="")
        self.assertEqual(unprotect(self.store.read()["providers"][0]["key_dpapi"]), "fixture-secret-KEY")
        await self.save(clear_key=True)
        self.assertFalse(self.store.read()["providers"][0]["key_dpapi"])

    async def test_app_actions_require_private_session_and_revision(self):
        with self.assertRaises(ValueError):
            await self.app.call("save_provider", {"session": "wrong", "revision": 1, "provider": self.provider})
        await self.save()
        with self.assertRaises(ValueError):
            await self.app.call("save_provider", {"session": self.app.session, "revision": 1, "provider": self.provider})
        self.assertEqual(len(self.store.read()["providers"]), 1)

    async def test_native_settings_toggle_and_delete(self):
        with patch("router.mcp_app.start_service"):
            await self.app.call("settings_update", {"set": {"enabled": True}})
        self.assertTrue(self.app.enabled())
        await self.save()
        await self.app.call("delete_provider", {"session": self.app.session,
                           "revision": self.store.read()["revision"], "id": "fixture"})
        self.assertEqual(self.store.read()["providers"], [])
        await self.app.call("settings_update", {"set": {"enabled": False}})
        self.assertFalse(self.app.enabled())
        self.assertIn('model = "gpt-fixture"', (self.home / "config.toml").read_text())

    async def test_error_not_success_and_probe_only_configured_models(self):
        result = await self.app.dispatch("tools/call", {"name": "probe_provider",
            "arguments": {"session": self.app.session, "model": "gpt-fixture"}})
        self.assertTrue(result["isError"])
        with self.assertRaises(ValueError):
            await self.app.call("settings_update", {"set": {"enabled": "yes"}})
