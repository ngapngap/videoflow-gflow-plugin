"""Fake clock tests for 1800s abandoned job timeout and 7/30-day + 10 GiB media retention."""

from __future__ import annotations

import uuid

from tests.conftest import FakeClock
from tests.mock_mcp_server import make_valid_mp4_bytes


def test_abandoned_job_timeout_1800s_persists_across_polls_and_restart(harness_factory):
    clock = FakeClock(initial=1_700_000_000.0)
    h = harness_factory(clock=clock, mock_state={"polls_before_done": 100})
    op_id = str(uuid.uuid4())

    h.runtime.store.reserve_operation(operation_id=op_id, media_kind="video", fingerprint="fp", snapshot={})
    h.runtime.store.claim_submit_gate(op_id)
    h.runtime.store.record_upstream_task(op_id, "up-stalled-1")

    # Repeated polls without progress do NOT reset clock
    clock.advance(900)
    r1 = h.client.get(f"/vf-plugin/v1/operations/{op_id}", headers=h.auth_headers())
    assert r1.status_code == 200
    assert r1.json()["status"] == "processing"

    # Restart does NOT reset clock
    c2 = h.restart()
    clock.advance(901)
    r2 = c2.get(f"/vf-plugin/v1/operations/{op_id}", headers=h.auth_headers())
    assert r2.status_code == 200
    assert r2.json()["status"] == "reconcile_needed"
    assert r2.json()["error"]["code"] == "OPERATION_TIMED_OUT"


def test_media_retention_7d_30d_budget_and_410_gone(harness_factory):
    clock = FakeClock(initial=1_700_000_000.0)
    # Use a budget of 80 bytes: 1 file (64 bytes) fits, 2 files (128 bytes) triggers LRU eviction of oldest
    h = harness_factory(clock=clock, media_budget_bytes=80)
    store = h.runtime.store

    def _create_completed(idx: int, extra_bytes: int = 0) -> tuple[str, str]:
        op_id = str(uuid.uuid4())
        store.reserve_operation(operation_id=op_id, media_kind="video", fingerprint=f"fp-{idx}", snapshot={})
        store.claim_submit_gate(op_id)
        store.record_upstream_task(op_id, f"up-{idx}")
        mp4 = h.data_dir / "output" / f"{op_id}.mp4"
        mp4.write_bytes(make_valid_mp4_bytes() + b"\x00" * extra_bytes)
        rec = store.mark_succeeded_video(op_id, media_path=mp4, upstream_media_id=f"rec-media-{idx}")
        return op_id, rec["media_name"]

    op1, name1 = _create_completed(1, extra_bytes=20)
    clock.advance(100)
    op2, name2 = _create_completed(2, extra_bytes=20)

    # At day 8: total > 60 bytes -> oldest (op1) is purged, op2 remains
    clock.advance(8 * 86400)
    res1 = store.run_media_cleanup()
    assert name1 in res1["deleted"]
    assert name2 not in res1["deleted"]

    # GET /v1/media/{name1} -> 410 with kind=not_found
    r410 = h.client.get(f"/v1/media/{name1}", headers=h.auth_headers())
    assert r410.status_code == 410
    assert r410.json()["error"]["kind"] == "not_found"
    assert r410.json()["error"]["recoverable_media_id"] == "rec-media-1"

    # Lookup op1 -> reconcile_needed, dedup preserved
    op1_rec = h.client.get(f"/vf-plugin/v1/operations/{op1}", headers=h.auth_headers()).json()
    assert op1_rec["status"] == "reconcile_needed"
    assert op1_rec["error"]["recoverable_media_id"] == "rec-media-1"

    # Active download protects op2 even past 30 days
    clock.advance(31 * 86400)
    store.begin_media_download(name2)
    res2 = store.run_media_cleanup()
    assert name2 not in res2["deleted"]
    store.end_media_download(name2, completed=True)

    # After download completes, last_downloaded_at was refreshed -> protected again for 7 days
    res3 = store.run_media_cleanup()
    assert name2 not in res3["deleted"]

    # Advance 30 days with no access -> op2 purged
    clock.advance(30 * 86400)
    res4 = store.run_media_cleanup()
    assert name2 in res4["deleted"]
