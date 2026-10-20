from __future__ import annotations

import copy
from pathlib import Path

from .store import Store, alias, load_json, write_json


def build_catalog(store: Store, codex_home: Path):
    cache = codex_home / "models_cache.json"
    previous = store.root / "native-models.json"
    if cache.exists():
        data = load_json(cache)
        models = [m for m in data.get("models", []) if not m["slug"].startswith("custom/")]
        if models:
            write_json(previous, {"models": models})
    elif previous.exists():
        models = load_json(previous)["models"]
    else:
        raise ValueError("尚无原生模型目录。请先在 Codex 登录订阅并加载一次模型菜单。")
    if not models:
        raise ValueError("原生模型目录为空；未覆盖桌面配置。")
    catalog = copy.deepcopy(models)
    for p in store.read()["providers"]:
        if not p.get("enabled", True):
            continue
        for m in p["models"]:
            efforts = m.get("reasoning_efforts") or ["low", "medium", "high"]
            catalog.append({
                "slug": alias(p["id"], m["id"]),
                "display_name": f"{p.get('name') or p['id']} · {m.get('name') or m['id']}",
                "description": f"用户 API · {p['protocol']} · {m['id']}",
                "base_instructions": (
                    "You are Codex, a coding assistant collaborating with the user. "
                    "Follow the system and developer instructions, applicable AGENTS.md files, "
                    "and the user's requested scope. Inspect relevant files before editing. "
                    "Use the provided tools to carry authorized work through implementation "
                    "and appropriate verification. Respect sandbox and approval decisions. "
                    "Do not invent tool results, completed tests, or external actions. "
                    "Preserve unrelated user changes. Report the result and remaining limitations clearly."
                ),
                "default_reasoning_level": "medium" if "medium" in efforts else efforts[0],
                "supported_reasoning_levels": [
                    {"effort": e, "description": e} for e in efforts
                ],
                "shell_type": "unified_exec", "visibility": "list",
                "supported_in_api": True, "priority": 100 + len(catalog),
                "additional_speed_tiers": [], "service_tiers": [],
                "availability_nux": None, "upgrade": None,
                "include_skills_usage_instructions": True,
                "include_plugin_usage_instructions": True,
                "include_apps_usage_instructions": True,
                "supports_reasoning_summary_parameter": False,
                "default_reasoning_summary": "none",
                "support_verbosity": False, "default_verbosity": None,
                "apply_patch_tool_type": None,
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "context_window": int(m.get("context_window", 128000)),
                "max_context_window": int(m.get("context_window", 128000)),
                "effective_context_window_percent": 90,
                "experimental_supported_tools": [],
                "input_modalities": ["text", "image"] if m.get("vision") else ["text"],
                "supports_search_tool": False, "supports_experimental_context": False,
                "use_responses_lite": False, "tool_mode": "direct",
                "supports_reasoning_effort_updates": False,
                "node_repl_auto_review_required": True, "node_repl_disabled": False,
            })
    path = store.root / "model-catalog.json"
    write_json(path, {"models": catalog})
    return path, len(models), len(catalog) - len(models)
