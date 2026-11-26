"""MCP compatibility tests: tool aliases, unknown required params, versions, and Streamable HTTP /mcp."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcp_client import GflowMcpClient, UpstreamCompatibilityError
from tests.conftest import write_mock_state


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_tool_rename_with_alias_and_new_model(harness_factory):
    mock_state = {
        "tool_renames": {"gflow_generate_video": "gflow_video_v2"},
        "catalog": {
            "video": {
                "modes": ["i2v"],
                "aspects": ["16:9"],
                "models": [
                    {
                        "name": "veo_next_gen",
                        "label": "Veo Next Gen",
                        "aliases": ["veo-next"],
                        "durations": [6, 12],
                        "aspects": ["16:9"],
                    }
                ],
            }
        },
    }
    h = harness_factory(
        mock_state=mock_state,
        extra_aliases={"generate_video": ["gflow_video_v2"]},
    )
    cap = h.client.get("/vf-plugin/v1/capabilities", headers=h.auth_headers())
    assert cap.status_code == 200
    models = cap.json()["video"]["models"]
    assert models[0]["id"] == "veo-next"
    assert models[0]["durations"] == [6, 12]
    assert "image" not in cap.json()["kinds"] or len(cap.json()["image"]["models"]) == 0


def test_unknown_required_param_and_old_version_rejected(tmp_path: Path):
    state_file = tmp_path / "state.json"
    mcp_cmd = [
        sys.executable,
        str(Path(__file__).parent / "mock_mcp_server.py"),
        "--transport",
        "stdio",
        "--state-file",
        str(state_file),
    ]

    # 1. Unknown required parameter
    write_mock_state(state_file, {"extra_required_params": {"gflow_generate_video": ["magic_token_v9"]}})
    client = GflowMcpClient(gflow_home=tmp_path / "home", output_dir=tmp_path / "out", stdio_command=mcp_cmd)
    with pytest.raises(UpstreamCompatibilityError, match="magic_token_v9"):
        import asyncio

        asyncio.run(client.probe_compatibility())

    # 2. Version lower than 0.107.0
    write_mock_state(state_file, {"version": "0.106.9"})
    with pytest.raises(UpstreamCompatibilityError, match="0.106.9"):
        import asyncio

        asyncio.run(client.probe_compatibility())

    # 3. Missing optional tool (generate_image) only disables image capability
    write_mock_state(state_file, {"version": "0.108.0", "hidden_tools": ["gflow_generate_image"]})
    import asyncio

    report = asyncio.run(client.probe_compatibility())
    assert "generate_image" in report.missing_optional_tools
    assert "generate_video" in report.resolved_tools


@pytest.mark.asyncio
async def test_streamable_http_transport_with_daemon_token(tmp_path: Path):
    state_file = tmp_path / "http_state.json"
    write_mock_state(state_file, {})
    port = _free_port()
    token = "daemon-secret-http-token"

    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parent / "mock_mcp_server.py"),
            "--transport",
            "http",
            "--port",
            str(port),
            "--token",
            token,
            "--state-file",
            str(state_file),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Wait for HTTP server port to bind
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)

        client = GflowMcpClient(
            gflow_home=tmp_path / "home",
            output_dir=tmp_path / "out",
            daemon_url=f"http://127.0.0.1:{port}/mcp",
            daemon_token=token,
        )
        report = await client.probe_compatibility()
        assert report.tasks_supported is True
        models = await client.list_models()
        assert "video" in models
    finally:
        proc.terminate()
        proc.wait(timeout=5)
