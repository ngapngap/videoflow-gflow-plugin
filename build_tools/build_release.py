"""Build PyInstaller onedir package and copy validated manifest.json."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from build_tools.validate_manifest import validate_manifest_data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build videoflow-gflow Windows onedir package")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dev", action="store_true", help="Build dev package (allows min_core_version: null)")
    mode.add_argument("--release", action="store_true", help="Build release package with strict core version gate")
    parser.add_argument("--min-core-version", type=str, default=None, help="Set min_core_version in packaged manifest")
    parser.add_argument("--released-cores", type=str, default="", help="Comma-separated released core versions")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    manifest_src = repo_root / "manifest.json"
    manifest_data = json.loads(manifest_src.read_text(encoding="utf-8"))

    if args.min_core_version is not None:
        manifest_data["min_core_version"] = args.min_core_version

    cores = {c.strip() for c in args.released_cores.split(",") if c.strip()} or None
    validate_manifest_data(manifest_data, release=args.release, released_cores=cores)

    spec_file = repo_root / "build_tools" / "videoflow-gflow.spec"
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(spec_file)],
        cwd=str(repo_root),
        check=True,
    )

    out_dir = repo_root / "dist" / "videoflow-gflow"
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Packaged successfully at: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
