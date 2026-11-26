"""Image editing (POST /v1/images/edits) and paid media recovery (recover_media) tests."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import VALID_PNG_DATA_URL, read_mock_state, write_mock_state


def test_image_edits_validation_and_b2_concurrent_replay(harness_factory):
    # Slow image generation to force 202 on initial/concurrent POSTs, then 200 on lookup/replay
    h = harness_factory(
        mock_state={"generate_image_delay_s": 0.4},
        image_sync_wait_s=0.1,
    )
    c = h.client

    # Field size is rejected with 400 on image route
    r_bad = c.post(
        "/v1/images/edits",
        headers=h.auth_headers(str(uuid.uuid4())),
        data={"prompt": "paint", "model": "nano2", "size": "1024x1024"},
        files={"image": ("a.png", b"IMG1", "image/png")},
    )
    assert r_bad.status_code == 400

    op_uuid = str(uuid.uuid4())

    def _send():
        return c.post(
            "/v1/images/edits",
            headers=h.auth_headers(op_uuid),
            data={"prompt": "paint", "model": "nano2", "aspect_ratio": "9:16"},
            files={"image": ("a.png", b"IMG1", "image/png")},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_send)
        f2 = pool.submit(_send)
        r1 = f1.result()
        r2 = f2.result()

    assert r1.status_code in (200, 202)
    assert r2.status_code in (200, 202)

    # Poll operation UUID until succeeded
    deadline = time.monotonic() + 3.0
    op_body = {}
    while time.monotonic() < deadline:
        op_body = c.get(f"/vf-plugin/v1/operations/{op_uuid}", headers=h.auth_headers()).json()
        if op_body["status"] == "succeeded":
            break
        time.sleep(0.05)

    assert op_body["status"] == "succeeded"
    assert len(op_body["data"][0]["b64_json"]) > 10
    assert int(read_mock_state(h.state_file).get("generate_image_calls") or 0) == 1

    # Replay POST after completion returns 200 with stored b64_json without re-generating
    r_replay = _send()
    assert r_replay.status_code == 200
    assert r_replay.json()["data"][0]["b64_json"] == op_body["data"][0]["b64_json"]
    assert int(read_mock_state(h.state_file).get("generate_image_calls") or 0) == 1

    # Same key with different effective aspect_ratio -> 409
    r_diff = c.post(
        "/v1/images/edits",
        headers=h.auth_headers(op_uuid),
        data={"prompt": "paint", "model": "nano2", "aspect_ratio": "1:1"},
        files={"image": ("a.png", b"IMG1", "image/png")},
    )
    assert r_diff.status_code == 409


def test_recover_media_action_for_billed_failed_download(harness_factory):
    h = harness_factory(
        mock_state={
            "polls_before_done": 1,
            "task_poll_error": {
                "type": "https://gflow-cli.dev/errors/media-download",
                "charged": "yes",
                "retryable": False,
                "next_action": "reconcile",
                "operation_id": "billed-media-777",
            },
        }
    )
    c = h.client
    op_uuid = str(uuid.uuid4())
    r = c.post(
        "/v1/videos",
        headers=h.auth_headers(op_uuid),
        json={"prompt": "ocean", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r.status_code == 202
    task_id = r.json()["task_id"]

    deadline = time.monotonic() + 3.0
    polled = {}
    while time.monotonic() < deadline:
        polled = c.get(f"/v1/videos/{task_id}", headers=h.auth_headers()).json()
        if polled["status"] != "processing":
            break
        time.sleep(0.05)

    assert polled["status"] == "reconcile_needed"
    assert polled["error"]["recoverable_media_id"] == "billed-media-777"

    # Invoke recover_media action
    write_mock_state(h.state_file, {})
    r_rec = c.post(
        "/vf-plugin/v1/actions/recover_media",
        headers=h.auth_headers(),
        json={"recoverable_media_id": "billed-media-777"},
    )
    assert r_rec.status_code in (200, 202)
    assert r_rec.json()["task_id"] == task_id

    # Verify video is now succeeded and downloadable
    v_after = c.get(f"/v1/videos/{task_id}", headers=h.auth_headers()).json()
    assert v_after["status"] == "succeeded"
    dl = c.get(f"/v1/media/{v_after['media_name']}", headers=h.auth_headers())
    assert dl.status_code == 200
    assert dl.content.startswith(b"\x00\x00\x00\x14ftyp")
