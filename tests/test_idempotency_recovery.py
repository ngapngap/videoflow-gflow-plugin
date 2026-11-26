"""Idempotency, cancel race, crash recovery, snapshot isolation, stale lock, and error scope tests."""

from __future__ import annotations

import json
import time
import uuid

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

    # Image still succeeds!
    r_img = h_q.client.post(
        "/v1/images/edits",
        headers=h_q.auth_headers(str(uuid.uuid4())),
        data={"prompt": "sunset", "model": "nano2", "aspect_ratio": "1:1"},
        files={"image": ("in.png", b"PNGDATA", "image/png")},
    )
    assert r_img.status_code == 200
    assert "b64_json" in r_img.json()["data"][0]
