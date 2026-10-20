from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import secrets

import tomlkit

from .catalog import build_catalog
from .store import Store, atomic_write, digest, file_lock, load_json, write_json


KEYS = ("openai_base_url", "model_catalog_json")


def prepare(store: Store, codex_home: Path):
    path = codex_home / "config.toml"
    doc = tomlkit.parse(path.read_bytes().decode("utf-8-sig") if path.exists() else "")
    if doc.get("model_provider", "openai") != "openai":
        raise ValueError("当前已有自定义 provider。为避免覆盖，请先恢复原生 provider 再接入。")
    receipt = store.root / "integration.json"
    if not receipt.exists() and any(k in doc for k in KEYS):
        raise ValueError("已有模型地址或目录配置，拒绝覆盖。请先审阅并移除既有路由配置。")
    catalog, native, custom = build_catalog(store, codex_home)
    expected = {"openai_base_url": store.base_url(), "model_catalog_json": str(catalog.resolve())}
    return path, doc, expected, {"native_models": native, "custom_models": custom}


def install(store: Store, codex_home: Path):
    with file_lock(store.root / "integration.lock"):
        return _install(store, codex_home)


def _install(store: Store, codex_home: Path):
    path, doc, expected, counts = prepare(store, codex_home)
    receipt_path = store.root / "integration.json"
    if receipt_path.exists():
        receipt = load_json(receipt_path)
        if receipt["config_path"] != str(path.resolve()):
            raise ValueError("此路由实例已接入另一个 Codex 配置目录。")
        if receipt.get("state") == "prepared" and all(doc.get(k) == receipt["original"][k] for k in KEYS):
            # A crash after recording the backup but before writing the config.
            receipt_path.unlink()
            return _install(store, codex_home)
        if any(doc.get(k) != receipt["written"][k] for k in KEYS):
            raise ValueError("接入字段已被其他程序修改，请先人工检查，未覆盖。")
        if receipt.get("state") == "prepared":
            receipt["state"] = "installed"
            receipt["after_sha256"] = digest(path)
            write_json(receipt_path, receipt)
        return {**counts, "status": "already_installed"}
    before_hash = digest(path)
    previous = {k: doc[k].unwrap() if k in doc else None for k in KEYS}
    for k, v in expected.items():
        doc[k] = v
    after = tomlkit.dumps(doc)
    tomlkit.parse(after)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    backup = store.root / "backups" / f"config-{stamp}.toml"
    original = path.read_bytes() if path.exists() else b""
    atomic_write(backup, original)
    receipt = {
        "config_path": str(path.resolve()), "original": previous, "written": expected,
        "backup": str(backup), "before_sha256": before_hash, "state": "prepared",
    }
    write_json(receipt_path, receipt)
    if digest(path) != before_hash:
        receipt_path.unlink()
        raise ValueError("Codex 配置刚被更改，接入已取消；请重试。")
    atomic_write(path, after)
    check = tomlkit.parse(path.read_text(encoding="utf-8"))
    if any(check.get(k) != v for k, v in expected.items()):
        raise RuntimeError("接入写入回读失败，备份已保留。")
    receipt["state"] = "installed"
    receipt["after_sha256"] = digest(path)
    write_json(receipt_path, receipt)
    return {**counts, "status": "installed", "backup": str(backup)}


def restore(store: Store):
    with file_lock(store.root / "integration.lock"):
        return _restore(store)


def _restore(store: Store):
    receipt_path = store.root / "integration.json"
    if not receipt_path.exists():
        return {"status": "not_installed"}
    receipt = load_json(receipt_path)
    path = Path(receipt["config_path"])
    before_hash = digest(path)
    doc = tomlkit.parse(path.read_bytes().decode("utf-8-sig"))
    if receipt.get("state") == "prepared" and all(doc.get(k) == receipt["original"][k] for k in KEYS):
        receipt_path.unlink()
        return {"status": "not_installed", "recovered": "prepared_receipt"}
    for k in KEYS:
        if doc.get(k) != receipt["written"][k]:
            raise ValueError(f"{k} 已被其他程序修改，拒绝覆盖。请对照备份手工恢复。")
    for k in KEYS:
        value = receipt["original"][k]
        if value is None:
            del doc[k]
        else:
            doc[k] = value
    if digest(path) != before_hash:
        raise ValueError("配置发生并发修改，恢复已取消。")
    atomic_write(path, tomlkit.dumps(doc))
    readback = tomlkit.parse(path.read_text(encoding="utf-8"))
    if any(readback.get(k) != receipt["original"][k] for k in KEYS):
        raise RuntimeError("恢复回读失败，备份与接入凭据已保留。")
    receipt["state"] = "restored"
    write_json(store.root / "last-restoration.json", receipt)
    receipt_path.unlink()
    return {"status": "restored"}
