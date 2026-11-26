"""Multi-profile isolation, 2 real processes + 10 jobs, profile mismatch, and port recovery."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

from tests.conftest import VALID_PNG_DATA_URL, write_mock_state


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(url: str, token: str, log_path: Path | None = None, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/healthz", headers={"Authorization": f"Bearer {token}"}, timeout=3.0)
            if r.status_code in (200, 503):
                return
        except Exception:
            pass
        time.sleep(0.1)
    log_txt = log_path.read_text(encoding="utf-8", errors="replace") if log_path and log_path.is_file() else ""
    raise AssertionError(f"Plugin at {url} did not become ready in {timeout_s}s.\nLOG:\n{log_txt}")


def _spawn_plugin(
    root: Path,
    *,
    port: int,
    token: str,
    profile: str,
    state_file: Path,
) -> subprocess.Popen[str]:
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg = root / "config.json"
    cfg.write_text(json.dumps({"video_model": "veo-fast", "profile": profile}), encoding="utf-8")

    env = dict(os.environ)
    env["VF_PLUGIN_TOKEN"] = token
    env["VF_PLUGIN_SECRETS_JSON"] = "{}"
    env["VF_MOCK_MCP_STATE"] = str(state_file)
    env["VF_GFLOW_MCP_CMD"] = (
        f'"{sys.executable}" "{Path(__file__).parent / "mock_mcp_server.py"}" '
        f'--transport stdio --state-file "{state_file}"'
    )
    log_file = (root / "plugin.log").open("w", encoding="utf-8")
    return subprocess.Popen(
        [
            sys.executable,
            "plugin.py",
            "--port",
            str(port),
            "--data-dir",
            str(data_dir),
            "--config-file",
            str(cfg),
        ],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        stdout=log_file,
        stderr=log_file,
        text=True,
    )


def test_two_profiles_ten_jobs_and_port_recovery(tmp_path: Path):
    state_a = tmp_path / "state_a.json"
    state_b = tmp_path / "state_b.json"
    write_mock_state(state_a, {"polls_before_done": 1})
    write_mock_state(state_b, {"polls_before_done": 1})

    port_a = _free_port()
    port_b = _free_port()
    tok_a = "token-profile-a"
    tok_b = "token-profile-b"

    proc_a = _spawn_plugin(tmp_path / "instA", port=port_a, token=tok_a, profile="profA", state_file=state_a)
    proc_b = _spawn_plugin(tmp_path / "instB", port=port_b, token=tok_b, profile="profB", state_file=state_b)
    try:
        url_a = f"http://127.0.0.1:{port_a}"
        url_b = f"http://127.0.0.1:{port_b}"
        _wait_ready(url_a, tok_a, tmp_path / "instA" / "plugin.log")
        _wait_ready(url_b, tok_b, tmp_path / "instB" / "plugin.log")

        jobs_a: list[tuple[str, str]] = []
        jobs_b: list[tuple[str, str]] = []
        for i in range(5):
            op_a = str(uuid.uuid4())
            ra = httpx.post(
                f"{url_a}/v1/videos",
                headers={"Authorization": f"Bearer {tok_a}", "Idempotency-Key": op_a},
                json={"prompt": f"A-{i}", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
                timeout=5.0,
            )
            assert ra.status_code == 202
            jobs_a.append((op_a, ra.json()["task_id"]))

            op_b = str(uuid.uuid4())
            rb = httpx.post(
                f"{url_b}/v1/videos",
                headers={"Authorization": f"Bearer {tok_b}", "Idempotency-Key": op_b},
                json={"prompt": f"B-{i}", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
                timeout=5.0,
            )
            assert rb.status_code == 202
            jobs_b.append((op_b, rb.json()["task_id"]))

        # Verify isolation: B does NOT know A's jobs
        r_cross = httpx.get(
            f"{url_b}/vf-plugin/v1/operations/{jobs_a[0][0]}",
            headers={"Authorization": f"Bearer {tok_b}"},
            timeout=5.0,
        )
        assert r_cross.status_code == 404

        # Wait for all 10 jobs to succeed
        for url, tok, jobs in [(url_a, tok_a, jobs_a), (url_b, tok_b, jobs_b)]:
            for _, tid in jobs:
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline:
                    st = httpx.get(f"{url}/v1/videos/{tid}", headers={"Authorization": f"Bearer {tok}"}, timeout=5.0).json()
                    if st["status"] == "succeeded":
                        break
                    time.sleep(0.1)
                assert st["status"] == "succeeded"

        # Kill instance A and recover on a new port
        proc_a.terminate()
        proc_a.wait(timeout=5)
        new_port_a = _free_port()
        proc_a = _spawn_plugin(tmp_path / "instA", port=new_port_a, token=tok_a, profile="profA", state_file=state_a)
        new_url_a = f"http://127.0.0.1:{new_port_a}"
        _wait_ready(new_url_a, tok_a)

        r_rec = httpx.get(
            f"{new_url_a}/vf-plugin/v1/operations/{jobs_a[0][0]}",
            headers={"Authorization": f"Bearer {tok_a}"},
            timeout=5.0,
        )
        assert r_rec.status_code == 200
        assert r_rec.json()["status"] == "succeeded"
    finally:
        for p in (proc_a, proc_b):
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=5)


def test_profile_mismatch_degrades_instance_but_allows_local_lookup(harness_factory):
    h = harness_factory(config={"video_model": "veo-fast", "profile": "profA"})
    op_id = str(uuid.uuid4())
    r = h.client.post(
        "/v1/videos",
        headers=h.auth_headers(op_id),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r.status_code == 202

    # Restart with different profile in same data_dir -> degraded
    c2 = h.restart(new_config={"video_model": "veo-fast", "profile": "profB"})
    hz = c2.get("/healthz", headers=h.auth_headers())
    assert hz.status_code == 503
    assert hz.json()["checks"]["identity"]["status"] == "degraded"

    # New job rejected with 424
    r_new = c2.post(
        "/v1/videos",
        headers=h.auth_headers(str(uuid.uuid4())),
        json={"prompt": "dog", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_new.status_code == 424

    # Existing UUID lookup still works!
    r_old = c2.get(f"/vf-plugin/v1/operations/{op_id}", headers=h.auth_headers())
    assert r_old.status_code == 200
