"""Media storage is re-scanned while the plugin runs, so storage pressure never goes stale (issue #15)."""

from __future__ import annotations

import time
import uuid

from tests.conftest import VALID_PNG_DATA_URL, FakeClock
from tests.mock_mcp_server import make_valid_mp4_bytes

# One 64-byte file fits the budget, two files (128 bytes) exceed it.
MEDIA_BUDGET_BYTES = 80
SCAN_INTERVAL_S = 0.05


def _create_completed_video(h, idx: int) -> tuple[str, str]:
    op_id = str(uuid.uuid4())
    store = h.runtime.store
    store.reserve_operation(operation_id=op_id, media_kind="video", fingerprint=f"fp-{idx}", snapshot={})
    store.claim_submit_gate(op_id)
    store.record_upstream_task(op_id, f"up-{idx}")
    mp4 = h.data_dir / "output" / f"{op_id}.mp4"
    mp4.write_bytes(make_valid_mp4_bytes() + b"\x00" * 20)
    rec = store.mark_succeeded_video(op_id, media_path=mp4, upstream_media_id=f"rec-media-{idx}")
    return op_id, rec["media_name"]


def _wait_for(what: str, condition, timeout_s: float = 10.0) -> None:
    """Block until the background scan has made condition() true."""
    deadline = time.monotonic() + timeout_s
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert condition(), f"{what} did not happen within {timeout_s}s"


def test_media_storage_is_rescanned_daily_and_refreshes_storage_pressure(harness_factory):
    clock = FakeClock(initial=1_700_000_000.0)
    h = harness_factory(clock=clock, media_budget_bytes=MEDIA_BUDGET_BYTES, media_cleanup_interval_s=SCAN_INTERVAL_S)

    # The startup scan saw an empty disk; two fresh files then push the instance over budget.
    op1, name1 = _create_completed_video(h, 1)
    clock.advance(100)
    _, name2 = _create_completed_video(h, 2)

    # Files are too fresh to purge (7-day minimum), so the periodic scan only raises storage pressure.
    _wait_for("storage pressure to be raised", lambda: h.runtime.storage_pressure)
    degraded = h.client.get("/healthz", headers=h.auth_headers())
    assert degraded.status_code == 503
    assert degraded.json()["checks"]["storage"]["status"] == "degraded"

    # Without a restart, new jobs are now refused while existing media stays downloadable.
    r_new = h.client.post(
        "/v1/videos",
        headers=h.auth_headers(str(uuid.uuid4())),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_new.status_code == 503
    assert r_new.json()["error"]["code"] == "STORAGE_BUDGET_EXCEEDED"
    assert h.client.get(f"/v1/media/{name2}", headers=h.auth_headers()).status_code == 200

    # Past the 7-day minimum, the next periodic scan evicts the oldest file and clears the pressure.
    clock.advance(8 * 86400)
    _wait_for("storage pressure to clear", lambda: not h.runtime.storage_pressure)
    recovered = h.client.get("/healthz", headers=h.auth_headers())
    assert recovered.status_code == 200
    assert "storage" not in recovered.json()["checks"]

    r410 = h.client.get(f"/v1/media/{name1}", headers=h.auth_headers())
    assert r410.status_code == 410
    assert r410.json()["error"]["code"] == "MEDIA_EXPIRED"
    assert r410.json()["error"]["kind"] == "not_found"
    assert r410.json()["error"]["recoverable_media_id"] == "rec-media-1"
    assert h.client.get(f"/v1/media/{name2}", headers=h.auth_headers()).status_code == 200

    op1_rec = h.client.get(f"/vf-plugin/v1/operations/{op1}", headers=h.auth_headers()).json()
    assert op1_rec["status"] == "reconcile_needed"


def test_media_cleanup_loop_survives_a_failed_scan(harness_factory, monkeypatch):
    clock = FakeClock(initial=1_700_000_000.0)
    h = harness_factory(clock=clock, media_budget_bytes=MEDIA_BUDGET_BYTES, media_cleanup_interval_s=SCAN_INTERVAL_S)
    store = h.runtime.store
    run_cleanup = store.run_media_cleanup
    scans: list[int] = []

    def _cleanup_failing_first_scan() -> dict:
        scans.append(1)
        if len(scans) == 1:
            raise RuntimeError("simulated storage failure")
        return run_cleanup()

    monkeypatch.setattr(store, "run_media_cleanup", _cleanup_failing_first_scan)
    _create_completed_video(h, 1)
    clock.advance(100)
    _create_completed_video(h, 2)

    _wait_for("storage pressure to be raised", lambda: h.runtime.storage_pressure)
    assert len(scans) >= 2  # the failed pass did not stop the loop
    degraded = h.client.get("/healthz", headers=h.auth_headers())
    assert degraded.status_code == 503
    assert degraded.json()["checks"]["storage"]["status"] == "degraded"
