"""Fake clock tests for 1800s abandoned job timeout and 7/30-day + 10 GiB media retention."""

from __future__ import annotations

import time
import uuid

from tests.conftest import VALID_PNG_DATA_URL, FakeClock, read_mock_state, write_mock_state
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


def test_abandoned_after_delete_times_out_to_reconcile_needed(harness_factory):
    """DELETE while upstream never reports cancelled/completed must not poll forever (issue #14).

    SPEC 247: once the 1800s mark passes without a known outcome the row becomes
    ``reconcile_needed`` (never ``cancelled``: upstream may still be running and billing), keeping
    the UUID, media ID and cancel tombstone so the host can reconcile.
    """
    clock = FakeClock(initial=1_700_000_000.0)
    h = harness_factory(clock=clock, mock_state={"polls_before_done": 1_000_000})
    op_uuid = str(uuid.uuid4())

    r_post = h.client.post(
        "/v1/videos",
        headers=h.auth_headers(op_uuid),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_post.status_code == 202

    # Wait until upstream_task_id is recorded
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        op = h.runtime.store.get_by_uuid(op_uuid)
        if op and op.get("upstream_task_id"):
            break
        time.sleep(0.02)
    assert op is not None and op.get("upstream_task_id")

    r_del = h.client.delete(f"/v1/videos/{r_post.json()['task_id']}", headers=h.auth_headers())
    assert r_del.status_code == 200
    assert r_del.json()["status"] == "abandoned"

    # Stage the SPEC 247 situation: a media ID is already known (possibly billed) but the outcome is not
    billed_media_id = "media-billed123"
    def _set_media(conn):
        conn.execute("UPDATE operations SET upstream_media_id = ? WHERE operation_id = ?", (billed_media_id, op_uuid))
    h.runtime.store._tx(_set_media)

    # At 1799s (one second below the >= 1800s threshold) the abandoned job is left alone
    clock.advance(1799)
    r_before = h.client.get(f"/vf-plugin/v1/operations/{op_uuid}", headers=h.auth_headers())
    assert r_before.json()["status"] == "abandoned"

    # Crossing the threshold moves it to reconcile_needed; poll loop exits on that terminal status
    clock.advance(2)
    r_after = h.client.get(f"/vf-plugin/v1/operations/{op_uuid}", headers=h.auth_headers())
    assert r_after.status_code == 200
    assert r_after.json()["status"] == "reconcile_needed"
    assert r_after.json()["error"]["code"] == "OPERATION_TIMED_OUT"
    assert int(read_mock_state(h.state_file).get("generate_video_calls") or 0) == 1

    # Reconciliation trail survives: UUID, upstream task/media IDs and the cancel tombstone are kept,
    # and the error carries the media ID so the host can reconcile a possibly-billed asset.
    row = h.runtime.store.get_by_uuid(op_uuid)
    assert row["cancel_requested"] == 1
    assert row["upstream_task_id"] == op["upstream_task_id"]
    assert row["upstream_media_id"] == billed_media_id
    assert r_after.json()["error"]["recoverable_media_id"] == billed_media_id

    # A late POST with the same UUID is still rejected (tombstone) and never re-generates
    r_replay = h.client.post(
        "/v1/videos",
        headers=h.auth_headers(op_uuid),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_replay.status_code == 409
    assert r_replay.json()["error"]["code"] == "OPERATION_CANCELLED"
    assert h.runtime.store.get_by_uuid(op_uuid)["status"] == "reconcile_needed"
    assert int(read_mock_state(h.state_file).get("generate_video_calls") or 0) == 1


def test_abandoned_poll_loop_exits_when_upstream_ignores_cancel(harness_factory):
    """Regression for issue #14: upstream keeps reporting ``working`` and ignores tasks/cancel.

    Before the fix the ``_poll_video_task`` loop kept polling (one new MCP session per call)
    until process restart because ``abandoned`` was never timed out.
    """
    clock = FakeClock(initial=1_700_000_000.0)
    h = harness_factory(clock=clock, mock_state={"polls_before_done": 1_000_000})
    op_uuid = str(uuid.uuid4())

    async def _noop_cancel(_task_id: str) -> dict[str, object]:
        return {}

    # Upstream accepts tasks/cancel but never acts on it (keeps answering "working")
    h.runtime.mcp.cancel_task = _noop_cancel  # type: ignore[method-assign]

    r_post = h.client.post(
        "/v1/videos",
        headers=h.auth_headers(op_uuid),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_post.status_code == 202

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        op = h.runtime.store.get_by_uuid(op_uuid)
        if op and op.get("upstream_task_id"):
            break
        time.sleep(0.02)
    assert op is not None and op.get("upstream_task_id")

    r_del = h.client.delete(f"/vf-plugin/v1/operations/{op_uuid}", headers=h.auth_headers())
    assert r_del.status_code == 200
    assert r_del.json()["status"] == "abandoned"
    lpa_at_abandon = h.runtime.store.get_by_uuid(op_uuid)["last_progress_at"]

    # The background task that runs the poll loop is still alive while it is inside the timeout
    # window. _run_video_operation() awaits _poll_video_task() directly, so that outer task IS the
    # poll loop. Other long-lived tasks (e.g. the daily media cleanup loop) are tracked too, so
    # identify it by its coroutine rather than by counting live tasks.
    poll_tasks = [
        t
        for t in h.runtime.background_tasks
        if not t.done() and t.get_coro().__qualname__ == "_run_video_operation"
    ]
    assert len(poll_tasks) == 1
    poll_task = poll_tasks[0]

    clock.advance(1801)

    # The loop calls check_abandoned_timeouts() every iteration and must now return
    exited = False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if poll_task.done():
            exited = True
            break
        time.sleep(0.02)
    assert exited, "poll loop did not exit after abandoned timeout"
    final = h.runtime.store.get_by_uuid(op_uuid)
    assert final["status"] == "reconcile_needed"
    assert final["error"]["code"] == "OPERATION_TIMED_OUT"
    # Root cause #2: upstream "working" on an abandoned job is NOT progress, so the timeout
    # reference must never be refreshed — otherwise the window is pushed out forever.
    assert final["last_progress_at"] == lpa_at_abandon


def test_abandoned_job_that_completes_upstream_still_succeeds(harness_factory):
    """Guard: upstream reporting completed AFTER the user abandoned must still yield the media.

    The abandoned timeout only fires when no outcome is known; a real completion inside the window
    keeps the pre-existing mark_succeeded_video behaviour (SPEC: GET may return succeeded later).
    """
    clock = FakeClock(initial=1_700_000_000.0)
    h = harness_factory(clock=clock, mock_state={"polls_before_done": 1_000_000})
    op_uuid = str(uuid.uuid4())

    async def _noop_cancel(_task_id: str) -> dict[str, object]:
        return {}

    # tasks/cancel is accepted but ignored, so the upstream job keeps running and later completes
    h.runtime.mcp.cancel_task = _noop_cancel  # type: ignore[method-assign]

    r_post = h.client.post(
        "/v1/videos",
        headers=h.auth_headers(op_uuid),
        json={"prompt": "cat", "size": "1280x720", "seconds": 8, "image": VALID_PNG_DATA_URL},
    )
    assert r_post.status_code == 202

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        op = h.runtime.store.get_by_uuid(op_uuid)
        if op and op.get("upstream_task_id"):
            break
        time.sleep(0.02)
    assert op is not None and op.get("upstream_task_id")

    r_del = h.client.delete(f"/vf-plugin/v1/operations/{op_uuid}", headers=h.auth_headers())
    assert r_del.status_code == 200
    assert r_del.json()["status"] == "abandoned"

    # Upstream finishes while we are still well inside the 1800s window
    clock.advance(60)
    write_mock_state(h.state_file, {**read_mock_state(h.state_file), "polls_before_done": 1})

    status = ""
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        status = h.client.get(f"/vf-plugin/v1/operations/{op_uuid}", headers=h.auth_headers()).json()["status"]
        if status == "succeeded":
            break
        time.sleep(0.05)
    assert status == "succeeded"
    assert int(read_mock_state(h.state_file).get("generate_video_calls") or 0) == 1


def test_media_retention_7d_30d_budget_and_410_gone(harness_factory):
    clock = FakeClock(initial=1_700_000_000.0)
    # Use a budget of 80 bytes: 1 file (64 bytes) fits, 2 files (128 bytes) triggers LRU eviction of oldest
    h = harness_factory(clock=clock, media_budget_bytes=80)
    store = h.runtime.store
    assert h.runtime.media_cleanup_interval_s == 86400  # default cadence: rescan daily

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
