from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from router.store import Store, default_state_dir


def main():
    parser = argparse.ArgumentParser(description="Codex desktop subscription + custom provider router")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
    parser.add_argument("command", choices=["mcp", "serve", "status", "restore", "catalog"], nargs="?", default="mcp")
    args = parser.parse_args()
    store = Store(args.state_dir.resolve())
    store.initialize()
    if args.command == "serve":
        from router.server import serve
        serve(store)
    elif args.command == "mcp":
        import asyncio
        from router.mcp_app import run
        asyncio.run(run(store, args.codex_home))
    elif args.command == "catalog":
        from router.catalog import build_catalog
        path, native, custom = build_catalog(store, args.codex_home)
        print(json.dumps({"catalog": str(path), "native": native, "custom": custom}, ensure_ascii=False))
    else:
        from router.manager import deactivate, status
        if args.command == "restore":
            result = deactivate(store)
        else:
            result = status(store) or {"status": "stopped"}
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
