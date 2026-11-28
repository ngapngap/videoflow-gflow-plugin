"""Contract v1.2 fixtures, HTTP status mapping, strict validation, and security tests."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from tests.conftest import OTHER_PNG_DATA_URL, VALID_PNG_DATA_URL, read_mock_state, write_mock_state


def _poll_until_terminal(client, headers, task_id: str, timeout_s: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        r = client.get(f"/v1/videos/{task_id}", headers=headers)
        assert r.status_code == 200
        body = r.json()
        if body["status"] != "processing":
            return body
        time.sleep(0.05)
    raise AssertionError(f"Task {task_id} did not finish within {timeout_s}s")


def test_contract_v1_2_fixtures_shape(harness_factory):
    fixtures = json.loads(Path("tests/fixtures/contract_v1_2.json").read_text(encoding="utf-8"))
    assert fixtures["manifest"]["plugin_api_version"] == "1"
    assert fixtures["responses"]["capabilities"]["plugin_api_version"] == "1"
    assert fixtures["responses"]["cooldown"]["error"]["scope"] == "instance"

    h = harness_factory()
    c = h.client
    hdrs = h.auth_headers()

    cap = c.get("/vf-plugin/v1/capabilities", headers=hdrs)
    assert cap.status_code == 200
    cap_j = cap.json()
    assert cap_j["plugin_api_version"] == "1"
    assert "video" in cap_j["kinds"]
    assert cap_j["video"]["supports_cancel"] == "before_submit_only"

    q = c.get("/vf-plugin/v1/quota", headers=hdrs)
    assert q.status_code == 200
    assert "limits" in q.json()
    assert q.json()["limits"][0]["media_kind"] == "video"


def test_bearer_auth_401_on_all_routes_including_healthz(harness_factory):
    h = harness_factory(token="super-secret-token-xyz")
    c = h.client

    for method, path in [
        ("GET", "/healthz"),
        ("GET", "/vf-plugin/v1/capabilities"),
        ("GET", "/vf-plugin/v1/quota"),
        ("GET", f"/vf-plugin/v1/operations/{uuid.uuid4()}"),
        ("DELETE", f"/vf-plugin/v1/operations/{uuid.uuid4()}"),
    ]:
        r = c.request(method, path)
        assert r.status_code == 401
        body = r.json()
        assert body["status"] == "failed"
        assert body["error"]["kind"] == "auth"
        assert body["error"]["code"] == "PLUGIN_TOKEN_INVALID"
        assert "super-secret-token-xyz" not in r.text

        r_bad = c.request(method, path, headers={"Authorization": "Bearer wrong"})
        assert r_bad.status_code == 401


def test_strict_video_validation_returns_400_and_0_submits(harness_factory):
    h = harness_factory()
    c = h.client

    # "16:9" for size -> 400
    r1 = c.post(
        "/v1/videos",
        headers=h.auth_headers(str(uuid.uuid4())),
        json={"prompt": "cat", "size": "16:9", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r1.status_code == 400
    assert r1.json()["error"]["kind"] == "business"

    # "8" string for seconds -> 400
    r2 = c.post(
        "/v1/videos",
        headers=h.auth_headers(str(uuid.uuid4())),
        json={"prompt": "cat", "size": "1280x720", "seconds": "8", "image": VALID_PNG_DATA_URL},
    )
    assert r2.status_code == 400

    # bool for seconds -> 400
    r3 = c.post(
        "/v1/videos",
        headers=h.auth_headers(str(uuid.uuid4())),
        json={"prompt": "cat", "size": "1280x720", "seconds": True, "image": VALID_PNG_DATA_URL},
    )
    assert r3.status_code == 400

    # image != input_reference bytes -> 400 with 0 upstream calls
    r4 = c.post(
        "/v1/videos",
        headers=h.auth_headers(str(uuid.uuid4())),
        json={
            "prompt": "cat",
            "size": "1280x720",
            "seconds": 8,
            "image": VALID_PNG_DATA_URL,
            "input_reference": OTHER_PNG_DATA_URL,
        },
    )
    assert r4.status_code == 400
    assert int(read_mock_state(h.state_file).get("generate_video_calls") or 0) == 0


def test_video_422_out_of_range_and_delete_unknown_404_vs_op_200(harness_factory):
    h = harness_factory()
    c = h.client

    r422 = c.post(
        "/v1/videos",
        headers=h.auth_headers(str(uuid.uuid4())),
        json={"prompt": "cat", "model": "veo-fast", "size": "1280x720", "seconds": 99, "image": VALID_PNG_DATA_URL},
    )
    assert r422.status_code == 422
    assert r422.json()["error"]["kind"] == "business"

    # DELETE /v1/videos/{unknown} -> 404
    r_del_vid = c.delete("/v1/videos/unknown-task-id", headers=h.auth_headers())
    assert r_del_vid.status_code == 404
    assert r_del_vid.json()["error"]["kind"] == "not_found"

    # DELETE /vf-plugin/v1/operations/{unknown} -> 200 tombstone
    unknown_uuid = str(uuid.uuid4())
    r_del_op = c.delete(f"/vf-plugin/v1/operations/{unknown_uuid}", headers=h.auth_headers())
    assert r_del_op.status_code == 200
    assert r_del_op.json()["status"] == "cancelled"
    assert r_del_op.json()["operation_id"] == unknown_uuid


def test_upstream_424_auth_429_quota_vs_transient_and_redaction(harness_factory):
    h = harness_factory(secrets={"proxy": "http://user:secretpass@1.2.3.4:8080", "daemon_token": "tok-secret-999"})
    c = h.client

    # 424 auth error
    write_mock_state(
        h.state_file,
        {
            "generate_video_error": {
                "type": "https://gflow-cli.dev/errors/auth-expired",
                "charged": "no",
                "retryable": False,
                "next_action": "reauth",
                "detail": "Cookie expired at C:\\Users\\secret\\profile_p1 with http://user:secretpass@1.2.3.4:8080",
            }
        },
    )
    op1 = str(uuid.uuid4())
    r1 = c.post(
        "/v1/videos",
        headers=h.auth_headers(op1),
        json={"prompt": "secret prompt xyz", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r1.status_code == 202
    polled = _poll_until_terminal(c, h.auth_headers(), r1.json()["task_id"])
    assert polled["status"] == "failed"
    assert polled["error"]["kind"] == "auth"
    msg = polled["error"]["message"]
    assert "secretpass" not in msg
    assert "C:\\Users\\secret" not in msg
    assert "secret prompt xyz" not in msg

    # Subsequent POST when instance is auth-blocked returns 424 at HTTP
    r_blocked = c.post(
        "/v1/videos",
        headers=h.auth_headers(str(uuid.uuid4())),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_blocked.status_code == 424

    # Healthz probes auth_status (mock default = authenticated) and clears the auth block + flag
    hz = c.get("/healthz", headers=h.auth_headers())
    assert hz.status_code == 200
    assert hz.json()["checks"]["account"]["status"] == "ok"
    assert h.runtime.auth_degraded is False
    assert h.runtime.instance_block is None

    # Test 429 quota vs 429 transient via gflow_errors directly
    from gflow_errors import translate_upstream_error

    st_q, err_q, _ = translate_upstream_error(
        {"type": "https://gflow-cli.dev/errors/insufficient-credits", "charged": "no", "retryable": False},
        media_kind="video",
        stage="pre_submit",
    )
    assert st_q == 429 and err_q.kind == "quota" and err_q.scope == "media_kind"

    st_t, err_t, _ = translate_upstream_error(
        {"type": "https://gflow-cli.dev/errors/rate-limit", "charged": "no", "retryable": True, "retry_after": 30},
        media_kind="video",
        stage="pre_submit",
    )
    assert st_t == 429 and err_t.kind == "transient" and err_t.retryable is True


AUTH_EXPIRED_ERROR = {
    "type": "https://gflow-cli.dev/errors/auth-expired",
    "charged": "no",
    "retryable": False,
    "next_action": "reauth",
}


def _post_video(h, op_id: str | None = None):
    return h.client.post(
        "/v1/videos",
        headers=h.auth_headers(op_id or str(uuid.uuid4())),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )


def test_auth_degraded_recovers_via_healthz_probe(harness_factory):
    """Regression: auth_degraded / auth instance_block must clear once upstream is authenticated again."""
    h = harness_factory(mock_state={"generate_video_error": AUTH_EXPIRED_ERROR, "auth_response": {"status": "expired"}})
    c = h.client

    # 1. auth-expired failure -> operation failed, instance blocked
    r1 = _post_video(h)
    assert r1.status_code == 202
    polled = _poll_until_terminal(c, h.auth_headers(), r1.json()["task_id"])
    assert polled["status"] == "failed"
    assert polled["error"]["code"] == "UPSTREAM_AUTH_REQUIRED"
    assert h.runtime.auth_degraded is True
    assert h.runtime.instance_block is not None
    assert h.runtime.instance_block.expires_at is None

    # 2. Still expired -> POST blocked with 424, healthz probes and reports degraded
    assert _post_video(h).status_code == 424
    hz_blocked = c.get("/healthz", headers=h.auth_headers())
    assert hz_blocked.status_code == 503
    assert hz_blocked.json()["checks"]["account"]["status"] == "degraded"

    # 3. User re-authenticates: healthz must keep probing even though auth_degraded is already set
    state = read_mock_state(h.state_file)
    state["generate_video_error"] = None
    state["auth_response"] = {"status": "authenticated", "profile": "p1", "user_email": "test@example.com"}
    write_mock_state(h.state_file, state)
    hz_ok = c.get("/healthz", headers=h.auth_headers())
    assert hz_ok.status_code == 200
    assert hz_ok.json()["status"] == "ok"
    assert hz_ok.json()["checks"]["account"] == {"status": "ok", "message": "Đã xác thực"}
    assert h.runtime.auth_degraded is False
    assert h.runtime.instance_block is None

    # 4. New job is accepted again
    r_new = _post_video(h)
    assert r_new.status_code == 202
    assert _poll_until_terminal(c, h.auth_headers(), r_new.json()["task_id"])["status"] == "succeeded"


def test_browser_lost_flag_resets_on_healthz_probe_and_on_success(harness_factory):
    """Regression: browser_lost set by BROWSER_SESSION_CLOSED must not stay sticky."""
    browser_closed = {
        "type": "https://gflow-cli.dev/errors/browser-session-closed",
        "charged": "no",
        "retryable": False,
    }
    # Upstream still reports an expired session, so only the signals under test can clear the flag.
    h = harness_factory(mock_state={"task_poll_error": browser_closed, "auth_response": {"status": "expired"}})
    c = h.client

    # 1. Browser lost during poll (post_submit -> reconcile_needed) -> flag set, healthz browser degraded
    r1 = _post_video(h)
    assert r1.status_code == 202
    polled = _poll_until_terminal(c, h.auth_headers(), r1.json()["task_id"])
    assert polled["status"] == "reconcile_needed"
    assert polled["error"]["code"] == "BROWSER_SESSION_CLOSED"
    assert h.runtime.browser_lost is True
    assert h.runtime.instance_block is None  # operation-scope error never blocks the instance
    hz1 = c.get("/healthz", headers=h.auth_headers())
    assert hz1.status_code == 503
    assert hz1.json()["checks"]["browser"]["status"] == "degraded"
    assert h.runtime.browser_lost is True  # probe did not clear it: upstream is not healthy yet

    # 2. A successful video clears the flag on its own (no healthz probe in between)
    write_mock_state(h.state_file, {"polls_before_done": 1, "auth_response": {"status": "expired"}})
    r2 = _post_video(h)
    assert r2.status_code == 202
    assert _poll_until_terminal(c, h.auth_headers(), r2.json()["task_id"])["status"] == "succeeded"
    assert h.runtime.browser_lost is False
    hz2 = c.get("/healthz", headers=h.auth_headers())
    assert hz2.json()["checks"]["browser"]["status"] == "ok"

    # 3. Probe path alone also clears the flag once upstream is healthy again
    h.runtime.browser_lost = True
    write_mock_state(h.state_file, {"auth_response": {"status": "authenticated", "profile": "p1", "user_email": "test@example.com"}})
    hz3 = c.get("/healthz", headers=h.auth_headers())
    assert hz3.status_code == 200
    assert hz3.json()["checks"]["browser"]["status"] == "ok"
    assert h.runtime.browser_lost is False


def test_healthz_probe_failure_keeps_last_known_auth_verdict(harness_factory, monkeypatch):
    """When auth_status cannot be reached, healthz must not invent a verdict nor clear the auth block."""
    from mcp_client import McpTransportError

    h = harness_factory(mock_state={"generate_video_error": AUTH_EXPIRED_ERROR})
    c = h.client

    r1 = _post_video(h)
    assert r1.status_code == 202
    assert _poll_until_terminal(c, h.auth_headers(), r1.json()["task_id"])["status"] == "failed"
    assert h.runtime.auth_degraded is True

    async def _broken_auth_status(profile):
        raise McpTransportError("MCP server unreachable")

    monkeypatch.setattr(h.runtime.mcp, "auth_status", _broken_auth_status)
    hz = c.get("/healthz", headers=h.auth_headers())
    assert hz.status_code == 503
    assert hz.json()["checks"]["account"]["status"] == "degraded"
    assert h.runtime.auth_degraded is True
    assert h.runtime.instance_block is not None
    assert _post_video(h).status_code == 424

    # Probe healthy again (tool absent upstream -> "unknown") -> block cleared, verdict re-derived
    async def _unknown_auth_status(profile):
        return {"status": "unknown"}

    monkeypatch.setattr(h.runtime.mcp, "auth_status", _unknown_auth_status)
    hz2 = c.get("/healthz", headers=h.auth_headers())
    assert hz2.status_code == 200
    assert hz2.json()["checks"]["account"]["status"] == "ok"
    assert h.runtime.auth_degraded is False
    assert h.runtime.instance_block is None


def test_file_confinement_and_corrupt_mp4_rejected(harness_factory, tmp_path: Path):
    # 1. Path traversal outside output_dir
    outside_path = tmp_path / "outside" / "evil.mp4"
    h = harness_factory(subdir="confine", mock_state={"outside_output_path": str(outside_path)})
    op_id = str(uuid.uuid4())
    r = h.client.post(
        "/v1/videos",
        headers=h.auth_headers(op_id),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r.status_code == 202
    res = _poll_until_terminal(h.client, h.auth_headers(), r.json()["task_id"])
    assert res["status"] == "reconcile_needed"
    assert res["error"]["code"] == "MEDIA_PATH_OUT_OF_BOUNDS"

    # 2. Corrupt MP4 bytes
    h2 = harness_factory(subdir="corrupt", mock_state={"corrupt_mp4": True})
    op_id2 = str(uuid.uuid4())
    r2 = h2.client.post(
        "/v1/videos",
        headers=h2.auth_headers(op_id2),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r2.status_code == 202
    res2 = _poll_until_terminal(h2.client, h2.auth_headers(), r2.json()["task_id"])
    assert res2["status"] == "reconcile_needed"
    assert res2["error"]["code"] == "INVALID_MP4_FILE"
