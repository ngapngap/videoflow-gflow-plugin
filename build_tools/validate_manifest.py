"""Validate plugin manifest.json for dev and CI release gates."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_ID_RE = re.compile(r"^[a-z0-9-]+$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")


class ManifestValidationError(ValueError):
    pass


def validate_manifest_data(
    data: dict[str, Any],
    *,
    release: bool = False,
    released_cores: set[str] | None = None,
) -> None:
    required_keys = (
        "id",
        "name",
        "version",
        "plugin_api_version",
        "entry",
        "preferred_port",
        "health_path",
        "capabilities_path",
        "quota_path",
        "config_schema",
        "actions",
    )
    for key in required_keys:
        if key not in data:
            raise ManifestValidationError(f"Thiếu trường bắt buộc '{key}' trong manifest.json.")

    if not isinstance(data["id"], str) or not _ID_RE.match(data["id"]):
        raise ManifestValidationError("Trường 'id' phải khớp ^[a-z0-9-]+$.")

    if data["plugin_api_version"] != "1":
        raise ManifestValidationError("Trường 'plugin_api_version' phải là '1'.")

    entry = data["entry"]
    if not isinstance(entry, dict) or not entry.get("exe") or not isinstance(entry.get("args"), list):
        raise ManifestValidationError("Trường 'entry' phải có 'exe' và 'args'.")

    for placeholder in ("{port}", "{data_dir}", "{config_file}"):
        if placeholder not in entry["args"]:
            raise ManifestValidationError(f"'entry.args' thiếu placeholder '{placeholder}'.")

    min_core = data.get("min_core_version")
    if release:
        if not isinstance(min_core, str) or not _SEMVER_RE.match(min_core):
            raise ManifestValidationError(
                "Release gate từ chối manifest: 'min_core_version' phải là chuỗi SemVer hợp lệ (không được null)."
            )
        if released_cores is not None and min_core not in released_cores:
            raise ManifestValidationError(
                f"Release gate từ chối manifest: 'min_core_version' ({min_core}) chưa phát hành hoặc chưa qua suite."
            )
    elif min_core is not None and (not isinstance(min_core, str) or not _SEMVER_RE.match(min_core)):
        raise ManifestValidationError("'min_core_version' nếu có giá trị phải là chuỗi SemVer hợp lệ.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate VideoFlow plugin manifest.json")
    parser.add_argument("manifest", type=Path, help="Path to manifest.json")
    parser.add_argument("--release", action="store_true", help="Enforce strict release gate")
    parser.add_argument("--released-cores", type=str, default="", help="Comma-separated released core versions")
    args = parser.parse_args(argv)

    data = json.loads(args.manifest.read_text(encoding="utf-8"))
    cores = {c.strip() for c in args.released_cores.split(",") if c.strip()} or None
    validate_manifest_data(data, release=args.release, released_cores=cores)
    print(f"Manifest OK ({'release' if args.release else 'dev'}): {args.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
