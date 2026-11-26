"""Manifest and CI release gate validation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from build_tools.validate_manifest import ManifestValidationError, validate_manifest_data


def test_dev_manifest_is_valid():
    data = json.loads(Path("manifest.json").read_text(encoding="utf-8"))
    validate_manifest_data(data, release=False)


def test_release_gate_rejects_null_and_unreleased_core():
    data = json.loads(Path("manifest.json").read_text(encoding="utf-8"))

    # Release mode must reject min_core_version: null
    with pytest.raises(ManifestValidationError, match="min_core_version"):
        validate_manifest_data(data, release=True, released_cores={"1.5.5"})

    # Release mode must reject unreleased core version
    data["min_core_version"] = "9.9.9"
    with pytest.raises(ManifestValidationError, match="chưa phát hành"):
        validate_manifest_data(data, release=True, released_cores={"1.5.5", "1.6.0"})

    # Release mode accepts valid released SemVer
    data["min_core_version"] = "1.5.5"
    validate_manifest_data(data, release=True, released_cores={"1.5.5", "1.6.0"})
