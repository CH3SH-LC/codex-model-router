"""Check the explicit source allowlist before commit or packaging."""
from pathlib import Path
import json
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def check():
    names = (ROOT / "public-files.txt").read_text(encoding="utf-8").splitlines()
    if len(names) != len(set(names)):
        raise ValueError("Duplicate public-file entry.")
    forbidden_parts = {".venv", "__pycache__", "evidence", "work", "state", ".state", "backups"}
    forbidden_files = {"auth.json", "settings.json", "integration.json", "models_cache.json",
                       "model-catalog.json", "native-models.json"}
    for name in names:
        path = Path(name)
        if (not name or path.is_absolute() or ".." in path.parts
                or forbidden_parts.intersection(path.parts) or path.name in forbidden_files
                or name.endswith((".pyc", ".log", ".jsonl"))):
            raise ValueError(f"Non-public file in allowlist: {name}")
        target = ROOT / path
        if target.is_symlink() or not target.resolve().is_relative_to(ROOT):
            raise ValueError(f"External or symbolic path: {name}")
        if not target.is_file():
            raise ValueError(f"Missing public file: {name}")
        if target.suffix == ".png":
            continue
        text = target.read_text(encoding="utf-8")
        patterns = [
            r"(?:ghp|gho|github_pat)_[A-Za-z0-9_]{30,}",
            r"sk-[A-Za-z0-9_-]{24,}",
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            r"(?i)[a-z]:[\\/]+Users[\\/]+[^\\/\s\"']+[\\/]",
        ]
        if any(re.search(pattern, text) for pattern in patterns):
            raise ValueError(f"Potential secret or personal path: {name}")
    if (ROOT / ".git").exists():
        tracked = set(subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")) - {""}
        if tracked != set(names):
            raise ValueError(f"Index/allowlist mismatch. Extra: {sorted(tracked-set(names))}; missing: {sorted(set(names)-tracked)}")
    plugin = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    version = plugin["version"]
    for name in ("router/__init__.py", "router/ui.html", "scripts/launch.cmd", "README.md", "docs/INSTALL.md"):
        if version not in (ROOT / name).read_text(encoding="utf-8"):
            raise ValueError(f"Version missing in {name}")
    if not plugin.get("license") == "MIT":
        raise ValueError("Plugin license mismatch.")
    return names


if __name__ == "__main__":
    print(f"Public source check passed: {len(check())} files.")
