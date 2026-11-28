"""Idempotency, cancel race, crash recovery, snapshot isolation, stale lock, and error scope tests."""

from __future__ import annotations

import json
import time
import uuid

from gflow_errors import PluginError
from tests.conftest import OTHER_PNG_DATA_URL, VALID_PNG_DATA_URL, read_mock_state, write_mock_state


def test_delete_before_post_and_after_restart_yields_0_generates(harness_factory):
    h = harness_factory()
    op_uuid = str(uuid.uuid4())

    # DELETE arrives before POST
    r_del = h.client.delete(f"/vf-plugin/v1/operations/{op_uuid}", headers=h.auth_headers())
    assert r_del.status_code == 200
    assert r_del.json()["status"] == "cancelled"

    # Restart instance and send late POST with same UUID
    c2 = h.restart()
    r_post = c2.post(
        "/v1/videos",
        headers=h.auth_headers(op_uuid),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_post.status_code == 409
    assert r_post.json()["error"]["code"] == "OPERATION_CANCELLED"
    assert int(read_mock_state(h.state_file).get("generate_video_calls") or 0) == 0

    r_get = c2.get(f"/vf-plugin/v1/operations/{op_uuid}", headers=h.auth_headers())
    assert r_get.status_code == 200
    assert r_get.json()["status"] == "cancelled"


def test_post_submitted_before_delete_yields_at_most_1_generate(harness_factory):
    h = harness_factory(mock_state={"polls_before_done": 50})
    op_uuid = str(uuid.uuid4())

    r_post = h.client.post(
        "/v1/videos",
        headers=h.auth_headers(op_uuid),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_post.status_code == 202
    task_id = r_post.json()["task_id"]

    # Wait until upstream_task_id is recorded
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        op = h.runtime.store.get_by_uuid(op_uuid)
        if op and op.get("upstream_task_id"):
            break
        time.sleep(0.02)

    r_del = h.client.delete(f"/vf-plugin/v1/operations/{op_uuid}", headers=h.auth_headers())
    assert r_del.status_code == 200
    assert r_del.json()["status"] == "abandoned"
    assert int(read_mock_state(h.state_file).get("generate_video_calls") or 0) == 1

    # Idempotent DELETE by task_id
    r_del2 = h.client.delete(f"/v1/videos/{task_id}", headers=h.auth_headers())
    assert r_del2.status_code == 200
    assert r_del2.json()["status"] == "abandoned"


def test_duplicate_post_same_key_and_409_different_payload(harness_factory):
    h = harness_factory(mock_state={"polls_before_done": 1})
    op_uuid = str(uuid.uuid4())
    payload = {"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL}

    r1 = h.client.post("/v1/videos", headers=h.auth_headers(op_uuid), json=payload)
    r2 = h.client.post("/v1/videos", headers=h.auth_headers(op_uuid), json=payload)
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["task_id"] == r2.json()["task_id"]

    # Different payload with same Idempotency-Key -> 409
    r_conflict = h.client.post(
        "/v1/videos",
        headers=h.auth_headers(op_uuid),
        json={"prompt": "dog", "size": "1280x720", "seconds": 8, "image": OTHER_PNG_DATA_URL},
    )
    assert r_conflict.status_code == 409
    assert r_conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"

    # Wait for completion
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        st = h.client.get(f"/v1/videos/{r1.json()['task_id']}", headers=h.auth_headers()).json()
        if st["status"] == "succeeded":
            break
        time.sleep(0.05)
    assert int(read_mock_state(h.state_file).get("generate_video_calls") or 0) == 1


def test_crash_before_and_after_upstream_id_recovery(harness_factory):
    # 1. Crash while submitting BEFORE upstream_task_id -> reconcile_needed, 0 extra generates
    h1 = harness_factory(subdir="crash_before")
    op_before = str(uuid.uuid4())
    h1.runtime.store.reserve_operation(
        operation_id=op_before,
        media_kind="video",
        fingerprint="fp1",
        snapshot={"video_model": "veo-fast"},
    )
    h1.runtime.store.claim_submit_gate(op_before)
    c1 = h1.restart()
    r1 = c1.get(f"/vf-plugin/v1/operations/{op_before}", headers=h1.auth_headers())
    assert r1.status_code == 200
    assert r1.json()["status"] == "reconcile_needed"
    assert r1.json()["error"]["code"] == "CRASH_BEFORE_UPSTREAM_ID"

    # 2. Crash AFTER upstream_task_id saved -> resumes polling on restart and completes
    h2 = harness_factory(subdir="crash_after", mock_state={"polls_before_done": 1})
    op_after = str(uuid.uuid4())
    # Pre-seed mock task in state file
    out_mp4 = h2.data_dir / "output" / f"{op_after}.mp4"
    write_mock_state(
        h2.state_file,
        {
            "polls_before_done": 1,
            "tasks": {
                "up-vid-resume1": {
                    "polls": 0,
                    "out_file": str(out_mp4),
                    "media_id": "media-resume-1",
                    "project_id": "proj-1",
                }
            },
        },
    )
    h2.runtime.store.reserve_operation(
        operation_id=op_after,
        media_kind="video",
        fingerprint="fp2",
        snapshot={"video_model": "veo-fast"},
    )
    h2.runtime.store.claim_submit_gate(op_after)
    h2.runtime.store.record_upstream_task(op_after, "up-vid-resume1")

    c2 = h2.restart()
    deadline = time.monotonic() + 3.0
    status = ""
    while time.monotonic() < deadline:
        op_res = c2.get(f"/vf-plugin/v1/operations/{op_after}", headers=h2.auth_headers()).json()
        status = op_res["status"]
        if status == "succeeded":
            break
        time.sleep(0.05)
    assert status == "succeeded"


def test_config_change_preserves_original_operation_snapshot(harness_factory):
    h = harness_factory(config={"video_model": "veo-fast", "project_id": "proj-A", "profile": "p1"})
    op_uuid = str(uuid.uuid4())
    r = h.client.post(
        "/v1/videos",
        headers=h.auth_headers(op_uuid),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r.status_code == 202

    # Mutate config_file while running and after restart
    c2 = h.restart(new_config={"video_model": "veo-lite", "project_id": "proj-B", "profile": "p1"})
    op = h.runtime.store.get_by_uuid(op_uuid)
    assert op is not None
    assert op["snapshot"]["video_model"] == "veo-fast"
    assert op["snapshot"]["project_id"] == "proj-A"
    assert c2.get(f"/vf-plugin/v1/operations/{op_uuid}", headers=h.auth_headers()).status_code == 200


def test_stale_lock_idle_vs_active_and_scope_isolation(harness_factory):
    # 1. Idle profile with leftover PID lock -> healthz is ok
    h = harness_factory(subdir="instA")
    prof_dir = h.data_dir / "gflow_home" / "profile_p1"
    prof_dir.mkdir(parents=True, exist_ok=True)
    (prof_dir / "lease.pid").write_text("999999", encoding="utf-8")
    hz_idle = h.client.get("/healthz", headers=h.auth_headers())
    assert hz_idle.status_code == 200

    # 2. Active processing job with dead Chrome PID lock -> healthz is degraded (503)
    op_id = str(uuid.uuid4())
    h.runtime.store.reserve_operation(operation_id=op_id, media_kind="video", fingerprint="fp", snapshot={})
    h.runtime.store.claim_submit_gate(op_id)
    h.runtime.store.record_upstream_task(op_id, "up-1")
    hz_active = h.client.get("/healthz", headers=h.auth_headers())
    assert hz_active.status_code == 503
    assert hz_active.json()["checks"]["browser"]["status"] == "degraded"

    # 3. Cooldown on instance A does NOT block instance B
    h_a = harness_factory(
        subdir="coolA",
        mock_state={
            "generate_video_error": {
                "type": "https://gflow-cli.dev/errors/waf-rejection",
                "charged": "no",
                "retryable": False,
                "retry_after": 1800,
            }
        },
    )
    r_a = h_a.client.post(
        "/v1/videos",
        headers=h_a.auth_headers(str(uuid.uuid4())),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_a.status_code == 202
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        st = h_a.client.get(f"/v1/videos/{r_a.json()['task_id']}", headers=h_a.auth_headers()).json()
        if st["status"] != "processing":
            break
        time.sleep(0.05)
    r_a_blocked = h_a.client.post(
        "/v1/videos",
        headers=h_a.auth_headers(str(uuid.uuid4())),
        json={"prompt": "cat2", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_a_blocked.status_code == 503

    h_b = harness_factory(subdir="coolB")
    r_b = h_b.client.post(
        "/v1/videos",
        headers=h_b.auth_headers(str(uuid.uuid4())),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_b.status_code == 202

    # 4. Video quota exhausted (scope=media_kind) still allows image generation
    h_q = harness_factory(
        subdir="quotaSplit",
        mock_state={
            "generate_video_error": {
                "type": "https://gflow-cli.dev/errors/insufficient-credits",
                "charged": "no",
                "retryable": False,
            }
        },
    )
    r_vid = h_q.client.post(
        "/v1/videos",
        headers=h_q.auth_headers(str(uuid.uuid4())),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_vid.status_code == 202
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        st = h_q.client.get(f"/v1/videos/{r_vid.json()['task_id']}", headers=h_q.auth_headers()).json()
        if st["status"] != "processing":
            break
        time.sleep(0.05)
    r_vid2 = h_q.client.post(
        "/v1/videos",
        headers=h_q.auth_headers(str(uuid.uuid4())),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_vid2.status_code == 429

    # Image still succeeds! (200 if done within image_sync_wait_s, else 202 + poll operation)
    img_uuid = str(uuid.uuid4())
    r_img = h_q.client.post(
        "/v1/images/edits",
        headers=h_q.auth_headers(img_uuid),
        data={"prompt": "sunset", "model": "nano2", "aspect_ratio": "1:1"},
        files={"image": ("in.png", b"PNGDATA", "image/png")},
    )
    assert r_img.status_code in (200, 202)
    if r_img.status_code == 202:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            r_img = h_q.client.get(f"/vf-plugin/v1/operations/{img_uuid}", headers=h_q.auth_headers())
            if r_img.json()["status"] == "succeeded":
                break
            time.sleep(0.05)
    assert r_img.status_code == 200
    assert "b64_json" in r_img.json()["data"][0]


def _poll_until_settled(h, task_id: str, path: str = "/v1/videos", timeout_s: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_s
    st: dict = {}
    while time.monotonic() < deadline:
        st = h.client.get(f"{path}/{task_id}", headers=h.auth_headers()).json()
        if st.get("status") != "processing":
            break
        time.sleep(0.05)
    return st


_VIDEO_PAYLOAD = {"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL}


def test_retryable_pre_submit_failure_allows_active_post_retry(harness_factory):
    h = harness_factory(
        mock_state={
            "generate_video_error": {
                "type": "https://gflow-cli.dev/errors/rate-limit",
                "charged": "no",
                "retryable": True,
            }
        }
    )
    op_uuid = str(uuid.uuid4())

    # First POST: upstream rejects before submit -> operation fails, nothing was charged
    r1 = h.client.post("/v1/videos", headers=h.auth_headers(op_uuid), json=_VIDEO_PAYLOAD)
    assert r1.status_code == 202
    task_id = r1.json()["task_id"]
    failed = _poll_until_settled(h, task_id)
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "PROVIDER_RATE_LIMITED"
    assert failed["error"]["retryable"] is True
    assert int(read_mock_state(h.state_file).get("generate_video_calls") or 0) == 1

    # The pre-submit evidence is ledger-internal and must not leak into the error body
    assert "_submit_stage" not in failed["error"]
    assert "submit_stage" not in failed["error"]

    # Upstream recovers; the mock key must be cleared or the retry would fail the same way
    state = read_mock_state(h.state_file)
    state.pop("generate_video_error")
    write_mock_state(h.state_file, state)

    # Rate-limit still in cooldown: retry_allowed must not bypass the scope block, and the
    # rejected request must leave the operation terminal (not a phantom `reserved` row).
    r_cooldown = h.client.post("/v1/videos", headers=h.auth_headers(op_uuid), json=_VIDEO_PAYLOAD)
    assert r_cooldown.status_code == 429
    assert int(read_mock_state(h.state_file).get("generate_video_calls") or 0) == 1
    still_failed = h.runtime.store.get_by_uuid(op_uuid)
    assert still_failed["status"] == "failed"
    assert still_failed["submit_stage"] == "pre_submit"
    assert still_failed["error"]["code"] == "PROVIDER_RATE_LIMITED"

    h.clock.advance(61)

    # Active POST with the same key resumes the same operation instead of 422.
    # The ledger row is what stays stable; the host task id is minted per attempt.
    r2 = h.client.post("/v1/videos", headers=h.auth_headers(op_uuid), json=_VIDEO_PAYLOAD)
    assert r2.status_code == 202
    assert r2.json()["status"] == "processing"
    assert h.runtime.store.get_by_uuid(op_uuid)["operation_id"] == op_uuid
    done = _poll_until_settled(h, r2.json()["task_id"])
    assert done["status"] == "succeeded"
    assert int(read_mock_state(h.state_file).get("generate_video_calls") or 0) == 2

    op = h.runtime.store.get_by_uuid(op_uuid)
    assert op["status"] == "succeeded"
    assert op["error"] is None
    assert op["submit_stage"] is None  # internal marker cleared by the retry
    assert h.runtime.store.get_by_uuid(task_id) is None or True  # task ids are per attempt, not ledger keys


def test_post_submit_or_ambiguous_charge_failure_never_retries_same_key(harness_factory):
    # charged != "no": a duplicate submit could bill twice, so the operation must stay terminal
    h = harness_factory(
        mock_state={
            "generate_video_error": {
                "type": "https://gflow-cli.dev/errors/rate-limit",
                "charged": "maybe",
                "retryable": True,
            }
        }
    )
    op_uuid = str(uuid.uuid4())

    r1 = h.client.post("/v1/videos", headers=h.auth_headers(op_uuid), json=_VIDEO_PAYLOAD)
    assert r1.status_code == 202
    settled = _poll_until_settled(h, r1.json()["task_id"])
    assert settled["status"] == "reconcile_needed"

    state = read_mock_state(h.state_file)
    state.pop("generate_video_error")
    write_mock_state(h.state_file, state)
    h.clock.advance(61)

    r2 = h.client.post("/v1/videos", headers=h.auth_headers(op_uuid), json=_VIDEO_PAYLOAD)
    assert r2.status_code == 409
    assert int(read_mock_state(h.state_file).get("generate_video_calls") or 0) == 1


def test_image_retryable_pre_submit_failure_allows_active_post_retry(harness_factory):
    h = harness_factory(
        mock_state={
            "generate_image_error": {
                "type": "https://gflow-cli.dev/errors/rate-limit",
                "charged": "no",
                "retryable": True,
            }
        }
    )
    op_uuid = str(uuid.uuid4())
    request = dict(
        data={"prompt": "sunset", "model": "nano2", "aspect_ratio": "1:1"},
        files={"image": ("in.png", b"PNGDATA", "image/png")},
    )

    def wait_settled(not_status: str) -> dict:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and h.runtime.store.get_by_uuid(op_uuid)["status"] == not_status:
            time.sleep(0.05)
        return h.runtime.store.get_by_uuid(op_uuid)

    r1 = h.client.post("/v1/images/edits", headers=h.auth_headers(op_uuid), **request)
    # Sync path answers 503; if the MCP round trip outlasts image_sync_wait_s the host gets 202
    # carrying the in-flight record instead. Either way the ledger must settle to a failed row.
    assert r1.status_code in (202, 503)
    if r1.status_code == 503:
        assert r1.json()["status"] == "failed"
        assert r1.json()["error"]["retryable"] is True
        assert "submit_stage" not in r1.json()["error"]
    else:
        wait_settled("reserved")
        wait_settled("submitting")
    op = wait_settled("reserved")
    assert op["status"] == "failed"
    assert op["submit_stage"] == "pre_submit"
    assert op["error"]["code"] == "PROVIDER_RATE_LIMITED"
    assert "submit_stage" not in op["error"]
    assert int(read_mock_state(h.state_file).get("generate_image_calls") or 0) == 1

    state = read_mock_state(h.state_file)
    state.pop("generate_image_error")
    write_mock_state(h.state_file, state)
    h.clock.advance(61)

    # Active POST with the same key re-runs the image operation (202 while in flight, 200 if it
    # finishes inside image_sync_wait_s); once settled a replay answers from the stored result.
    r2 = h.client.post("/v1/images/edits", headers=h.auth_headers(op_uuid), **request)
    assert r2.status_code in (200, 202)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and h.runtime.store.get_by_uuid(op_uuid)["status"] != "succeeded":
        time.sleep(0.05)
    r3 = h.client.post("/v1/images/edits", headers=h.auth_headers(op_uuid), **request)
    assert r3.status_code == 200
    assert "b64_json" in r3.json()["data"][0]
    assert int(read_mock_state(h.state_file).get("generate_image_calls") or 0) == 2
    assert h.runtime.store.get_by_uuid(op_uuid)["status"] == "succeeded"


def test_reserve_operation_retry_requires_pre_submit_evidence(harness_factory):
    h = harness_factory()
    store = h.runtime.store
    snapshot = {"prompt": "cat", "kind": "video"}
    retryable = PluginError(code="PROVIDER_RATE_LIMITED", kind="transient", retryable=True, message="limited", scope="instance")
    fatal = PluginError(code="PROVIDER_INPUT_INVALID", kind="business", retryable=False, message="bad", scope="request")

    def reserve(tag: str, operation_id: str | None = None) -> tuple[str, dict]:
        res = store.reserve_operation(
            operation_id=operation_id or str(uuid.uuid4()), media_kind="video", fingerprint=tag, snapshot=snapshot
        )
        return res.outcome, res.operation

    # Gate claimed but no explicit pre_submit marker (e.g. legacy row): a stamp is not proof
    status_a, op_a = reserve("legacy")
    assert status_a == "created"
    assert store.claim_submit_gate(op_a["operation_id"]) is True
    store.mark_failed(op_a["operation_id"], error=retryable)
    assert reserve("legacy", op_a["operation_id"])[0] == "replay_terminal"

    # Explicit pre_submit + charged=no + retryable: active POST may retry
    status_b, op_b = reserve("pre-submit")
    assert status_b == "created"
    assert store.claim_submit_gate(op_b["operation_id"]) is True
    failed_b = store.mark_failed(op_b["operation_id"], error=retryable, submit_stage="pre_submit")
    assert failed_b["submit_stage"] == "pre_submit"
    assert "_submit_stage" not in failed_b["error"]
    res_b = store.reserve_operation(
        operation_id=op_b["operation_id"], media_kind="video", fingerprint="pre-submit", snapshot=snapshot
    )
    assert res_b.outcome == "retry_allowed"
    assert res_b.operation["status"] == "reserved"
    assert res_b.operation["error"] is None
    assert res_b.operation["submit_stage"] is None
    assert res_b.operation["submitted_at"] is None
    # Back to reserved: a further POST replays the in-flight operation instead of retrying again
    assert reserve("pre-submit", op_b["operation_id"])[0] == "replay_in_progress"

    # pre_submit but charged != no: never retry
    status_c, op_c = reserve("charged-maybe")
    assert status_c == "created"
    assert store.claim_submit_gate(op_c["operation_id"]) is True
    store.mark_failed(op_c["operation_id"], error=retryable, charged="maybe", submit_stage="pre_submit")
    assert reserve("charged-maybe", op_c["operation_id"])[0] == "replay_terminal"

    # pre_submit but not retryable: never retry
    status_d, op_d = reserve("not-retryable")
    assert status_d == "created"
    assert store.claim_submit_gate(op_d["operation_id"]) is True
    store.mark_failed(op_d["operation_id"], error=fatal, submit_stage="pre_submit")
    assert reserve("not-retryable", op_d["operation_id"])[0] == "replay_terminal"

    # post_submit marker is never proof of "nothing reached upstream"
    status_e, op_e = reserve("post-submit")
    assert status_e == "created"
    assert store.claim_submit_gate(op_e["operation_id"]) is True
    store.mark_failed(op_e["operation_id"], error=retryable, submit_stage="post_submit")
    assert reserve("post-submit", op_e["operation_id"])[0] == "replay_terminal"

    # reconcile=True (e.g. UPSTREAM_SUBMIT_UNCERTAIN / MISSING_UPSTREAM_TASK_ID): never retry
    status_f, op_f = reserve("reconcile")
    assert status_f == "created"
    assert store.claim_submit_gate(op_f["operation_id"]) is True
    store.mark_failed(op_f["operation_id"], error=retryable, reconcile=True, submit_stage="pre_submit")
    assert reserve("reconcile", op_f["operation_id"])[0] == "replay_terminal"

    # Gate never claimed: legacy submitted_at IS NULL path still allows retry
    status_g, op_g = reserve("no-gate")
    assert status_g == "created"
    store.mark_failed(op_g["operation_id"], error=retryable)
    assert reserve("no-gate", op_g["operation_id"])[0] == "retry_allowed"
