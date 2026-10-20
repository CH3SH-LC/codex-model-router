import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.install import Engine, discover_codex, prepare_runtime, register_plugin


class InstallerTests(unittest.TestCase):
    def test_discovers_desktop_engine_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            old = base / "OpenAI/Codex/bin/old/codex.exe"
            new = base / "OpenAI/Codex/bin/new/codex.exe"
            for path in (old, new):
                path.parent.mkdir(parents=True)
                path.write_bytes(b"fixture")
            os.utime(old, (1, 1))
            os.utime(new, (2, 2))
            self.assertEqual(discover_codex(local_app_data=base), new)
            self.assertEqual(discover_codex(old), old.resolve())
            with self.assertRaises(ValueError):
                discover_codex(base / "missing.exe")

    def test_does_not_modify_unowned_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / "important.txt"
            sentinel.write_text("keep")
            with self.assertRaisesRegex(ValueError, "unowned"):
                prepare_runtime(tmp)
            self.assertEqual(sentinel.read_text(), "keep")
            self.assertFalse((Path(tmp) / ".codex-router-runtime").exists())

    def test_registration_uses_actual_summary_and_no_route_writes(self):
        with patch("scripts.install.Engine") as factory:
            client = factory.return_value
            client.call.side_effect = [{}, {}, {}, {"plugin": {"summary": {"installed": True, "enabled": True}}}]
            result = register_plugin(Path("codex.exe"), Path("fixture-home"))
            self.assertTrue(result["installed"])
            self.assertEqual([c.args[0] for c in client.call.call_args_list],
                             ["initialize", "marketplace/add", "plugin/install", "plugin/read"])
            client.close.assert_called_once()

    def test_unconfirmed_install_is_failure(self):
        with patch("scripts.install.Engine") as factory:
            client = factory.return_value
            client.call.side_effect = [{}, {}, {}, {"plugin": {"summary": {"installed": False, "enabled": False}}}]
            with self.assertRaisesRegex(RuntimeError, "did not confirm"):
                register_plugin(Path("codex.exe"), Path("fixture-home"))
            client.close.assert_called_once()

    def test_rpc_error_and_early_exit_are_not_success(self):
        fixtures = [
            ("import json,sys; x=json.loads(sys.stdin.readline()); "
             "print(json.dumps({'id':x['id'],'error':{'message':'fixture rejection'}}),flush=True)",
             "fixture rejection"),
            ("pass", "exited"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for program, expected in fixtures:
                with self.subTest(expected=expected):
                    client = Engine([sys.executable, "-u", "-c", program], Path(tmp))
                    try:
                        with self.assertRaisesRegex(RuntimeError, expected):
                            client.call("fixture", {}, timeout=5)
                    finally:
                        client.close()
