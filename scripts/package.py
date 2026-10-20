"""Build and verify a deterministic, allowlisted source release."""
import hashlib
import json
from pathlib import Path
import zipfile

from check_public import ROOT, check


def main():
    names = check()
    version = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))["version"]
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    archive_path = output / f"codex-model-router-v{version}.zip"
    payloads = {}
    for name in sorted(names):
        raw = (ROOT / name).read_bytes()
        if not name.endswith(".png"):
            raw = raw.replace(b"\r\n", b"\n")
            if name.endswith(".cmd"):
                raw = raw.replace(b"\n", b"\r\n")
        payloads[name] = raw
    manifest = "".join(hashlib.sha256(raw).hexdigest() + "  " + name + "\n"
                       for name, raw in payloads.items()).encode("utf-8")
    payloads["MANIFEST.sha256"] = manifest
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, raw in payloads.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw, compresslevel=9)
    with zipfile.ZipFile(archive_path) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity failure.")
        for line in archive.read("MANIFEST.sha256").decode().splitlines():
            expected, name = line.split("  ", 1)
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError(f"Manifest mismatch: {name}")
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    (output / "MANIFEST.sha256").write_bytes(manifest)
    print(json.dumps({"version": version, "files": len(names), "package": archive_path.name,
                      "bytes": archive_path.stat().st_size, "sha256": digest}, indent=2))


if __name__ == "__main__":
    main()
