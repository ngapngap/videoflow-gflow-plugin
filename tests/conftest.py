"""Shared pytest fixtures, FakeClock, and MockHost helpers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_client import GflowMcpClient
from plugin import PluginRuntime, build_runtime, create_app

VALID_PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg=="
)
VALID_PNG_DATA_URL = f"data:image/png;base64,{VALID_PNG_1X1_B64}"
OTHER_PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
OTHER_PNG_DATA_URL = f"data:image/png;base64,{OTHER_PNG_1X1_B64}"


class FakeClock:
    """Deterministic controllable clock for retention & timeout tests."""

    def __init__(self, initial: float = 1_700_000_000.0) -> None:
        self._now = float(initial)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        self._now += float(seconds)
        return self._now


def write_mock_state(state_file: Path, data: dict[str, Any]) -> None:
    state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def read_mock_state(state_file: Path) -> dict[str, Any]:
    return json.loads(state_file.read_text(encoding="utf-8"))


class PluginHarness:
    """In-process plugin host harness backed by real stdio mock MCP server."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        token: str = "test-bearer-token-123",
        secrets: dict[str, str] | None = None,
        config: dict[str, Any] | None = None,
        mock_state: dict[str, Any] | None = None,
        extra_aliases: dict[str, list[str]] | None = None,
        clock: FakeClock | None = None,
        media_budget_bytes: int | None = None,
        abandoned_timeout_s: int | None = None,
        poll_interval_s: float = 0.05,
        image_sync_wait_s: float = 1.5,
    ) -> None:
        self.root = tmp_path
        self.root.mkdir(parents=True, exist_ok=True)
        self.data_dir = tmp_path / "data"
        self.config_file = tmp_path / "config.json"
        self.state_file = tmp_path / "mock_mcp_state.json"
        self.token = token
        self.clock = clock or FakeClock()

        self.config_file.write_text(
            json.dumps(config or {"video_model": "veo-fast", "image_model": "nano2", "profile": "p1"}),
            encoding="utf-8",
        )
        write_mock_state(self.state_file, mock_state or {})

        os.environ["VF_PLUGIN_TOKEN"] = token
        os.environ["VF_PLUGIN_SECRETS_JSON"] = json.dumps(secrets or {})
        os.environ["VF_MOCK_MCP_STATE"] = str(self.state_file)

        self.extra_aliases = extra_aliases
        self.media_budget_bytes = media_budget_bytes
        self.abandoned_timeout_s = abandoned_timeout_s
        self.poll_interval_s = poll_interval_s
        self.image_sync_wait_s = image_sync_wait_s
        self._client_cm: TestClient | None = None
        self.client: TestClient | None = None
        self.runtime: PluginRuntime | None = None

    def start(self) -> TestClient:
        gflow_home = self.data_dir / "gflow_home"
        output_dir = self.data_dir / "output"
        mcp_cmd = [sys.executable, str(Path(__file__).parent / "mock_mcp_server.py"), "--transport", "stdio", "--state-file", str(self.state_file)]
        mcp = GflowMcpClient(
            gflow_home=gflow_home,
            output_dir=output_dir,
            extra_aliases=self.extra_aliases,
            stdio_command=mcp_cmd,
        )
        self.runtime = build_runtime(
            data_dir=self.data_dir,
            config_file=self.config_file,
            clock=self.clock,
            mcp_client=mcp,
            media_budget_bytes=self.media_budget_bytes,
            abandoned_timeout_s=self.abandoned_timeout_s,
            poll_interval_s=self.poll_interval_s,
            image_sync_wait_s=self.image_sync_wait_s,
        )
        app = create_app(self.runtime)
        self._client_cm = TestClient(app)
        self.client = self._client_cm.__enter__()
        return self.client

    def stop(self) -> None:
        if self._client_cm is not None:
            self._client_cm.__exit__(None, None, None)
            self._client_cm = None
            self.client = None

    def restart(self, *, new_config: dict[str, Any] | None = None) -> TestClient:
        self.stop()
        if new_config is not None:
            self.config_file.write_text(json.dumps(new_config), encoding="utf-8")
        return self.start()

    def auth_headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.token}"}
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h


@pytest.fixture
def harness_factory(tmp_path: Path):
    created: list[PluginHarness] = []

    def _make(subdir: str = "inst1", **kwargs: Any) -> PluginHarness:
        h = PluginHarness(tmp_path / subdir, **kwargs)
        h.start()
        created.append(h)
        return h

    yield _make

    for h in created:
        h.stop()
