"""SQLite job ledger, atomic idempotency/cancel state machine, confinement, MP4 validation, and retention."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from gflow_errors import PluginError, PluginHttpError

DEFAULT_ABANDONED_TIMEOUT_S = 1800
DEFAULT_MEDIA_BUDGET_BYTES = 10 * 1024 * 1024 * 1024  # 10 GiB
MIN_MEDIA_RETENTION_S = 7 * 86400
MAX_MEDIA_UNACCESSED_S = 30 * 86400

# Stage at which a failed operation was rejected by upstream. ``pre_submit`` means upstream
# asserted the command failed before any submit side effect, so nothing reached the provider.
SubmitStage = Literal["pre_submit", "post_submit"]
# Internal marker persisted inside error_json by mark_failed(submit_stage=...). It never leaves
# the ledger: _row_to_dict lifts it to the row-level ``submit_stage`` and strips it from ``error``.
_SUBMIT_STAGE_KEY = "_submit_stage"


def _parse_error_json(raw: str | None) -> tuple[dict[str, Any] | None, SubmitStage | None]:
    """Decode error_json into (client-facing error, submit stage marker)."""
    if not raw:
        return None, None
    err = json.loads(raw)
    stage = err.pop(_SUBMIT_STAGE_KEY, None)
    return err, stage  # type: ignore[return-value]


class Clock(Protocol):
    def now(self) -> float: ...


class SystemClock:
    def now(self) -> float:
        return time.time()


@dataclass(slots=True)
class ReserveResult:
    outcome: str  # "created" | "replay_in_progress" | "replay_succeeded" | "replay_terminal" | "retry_allowed"
    operation: dict[str, Any]


def validate_confined_path(candidate: str | Path, output_root: Path) -> Path:
    """Resolve symlinks/relative segments and ensure candidate sits strictly inside output_root."""
    root_resolved = output_root.resolve()
    cand_path = Path(candidate)
    if not cand_path.is_absolute():
        cand_path = root_resolved / cand_path
    resolved = cand_path.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"Đường dẫn media nằm ngoài thư mục output cho phép.") from exc
    if resolved == root_resolved:
        raise ValueError("Đường dẫn media trỏ tới thư mục gốc thay vì file.")
    return resolved


def validate_mp4_file(path: Path) -> bool:
    """Verify file exists, is non-empty, starts with an ISO BMFF ftyp box, and contains moov/mdat."""
    if not path.is_file():
        return False
    size = path.stat().st_size
    if size < 16:
        return False
    try:
        with path.open("rb") as fh:
            header = fh.read(16)
            if len(header) < 12 or header[4:8] != b"ftyp":
                return False
            # Walk top-level ISO BMFF boxes to verify structural integrity
            fh.seek(0)
            offset = 0
            has_moov_or_mdat = False
            while offset < size:
                fh.seek(offset)
                box_hdr = fh.read(8)
                if len(box_hdr) < 8:
                    return False
                box_size = int.from_bytes(box_hdr[0:4], "big")
                box_type = box_hdr[4:8]
                if box_type in (b"moov", b"mdat"):
                    has_moov_or_mdat = True
                if box_size == 0:
                    break
                if box_size == 1:
                    ext = fh.read(8)
                    if len(ext) < 8:
                        return False
                    box_size = int.from_bytes(ext, "big")
                if box_size < 8 or offset + box_size > size:
                    return False
                offset += box_size
            return has_moov_or_mdat
    except OSError:
        return False


class JobStore:
    """Thread-safe SQLite store for operations, profile binding, and media retention."""

    def __init__(
        self,
        db_path: Path,
        output_dir: Path,
        tmp_dir: Path,
        *,
        clock: Clock | None = None,
        media_budget_bytes: int = DEFAULT_MEDIA_BUDGET_BYTES,
        abandoned_timeout_s: int = DEFAULT_ABANDONED_TIMEOUT_S,
    ) -> None:
        self.db_path = db_path
        self.output_dir = output_dir
        self.tmp_dir = tmp_dir
        self.clock: Clock = clock or SystemClock()
        self.media_budget_bytes = media_budget_bytes
        self.abandoned_timeout_s = abandoned_timeout_s

        self._lock = threading.RLock()
        self._active_downloads: set[str] = set()
        self._active_recoveries: set[str] = set()

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS instance_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    task_id TEXT UNIQUE,
                    media_kind TEXT,
                    status TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    request_fingerprint TEXT,
                    snapshot_json TEXT,
                    upstream_task_id TEXT,
                    upstream_media_id TEXT,
                    media_name TEXT UNIQUE,
                    media_path TEXT,
                    media_size_bytes INTEGER,
                    media_purged INTEGER NOT NULL DEFAULT 0,
                    image_b64_json TEXT,
                    error_json TEXT,
                    charged TEXT NOT NULL DEFAULT 'no',
                    created_at REAL NOT NULL,
                    submitted_at REAL,
                    last_progress_at REAL,
                    completed_at REAL,
                    last_downloaded_at REAL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status);
                CREATE INDEX IF NOT EXISTS idx_operations_task_id ON operations(task_id);
                CREATE INDEX IF NOT EXISTS idx_operations_media_name ON operations(media_name);
                """
            )

    def _tx(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                res = fn(self._conn)
                self._conn.execute("COMMIT")
                return res
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        d["snapshot"] = json.loads(d["snapshot_json"]) if d.get("snapshot_json") else {}
        d["error"], d["submit_stage"] = _parse_error_json(d.get("error_json"))
        return d

    # --- Instance metadata (profile binding & default project) ---

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM instance_meta WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO instance_meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

        self._tx(_op)

    # --- Temp input image files ---

    def write_temp_image(self, operation_id: str, data: bytes, ext: str) -> Path:
        safe_id = "".join(c for c in operation_id if c.isalnum() or c in "-_")
        path = self.tmp_dir / f"{safe_id}.{ext}"
        path.write_bytes(data)
        return path

    def cleanup_temp_image(self, operation_id: str) -> None:
        safe_id = "".join(c for c in operation_id if c.isalnum() or c in "-_")
        for p in self.tmp_dir.glob(f"{safe_id}.*"):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # --- Operation lookup ---

    def get_by_uuid(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            return self._row_to_dict(row)

    def get_by_task_id(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM operations WHERE task_id = ?", (task_id,)).fetchone()
            return self._row_to_dict(row)

    def get_by_media_name(self, media_name: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM operations WHERE media_name = ?", (media_name,)).fetchone()
            return self._row_to_dict(row)

    def get_by_recoverable_media_id(self, media_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM operations WHERE upstream_media_id = ? ORDER BY updated_at DESC LIMIT 1",
                (media_id,),
            ).fetchone()
            return self._row_to_dict(row)

    # --- Idempotent reservation, submit gate & cancellation ---

    def reserve_operation(
        self,
        *,
        operation_id: str,
        media_kind: str,
        fingerprint: str,
        snapshot: dict[str, Any],
    ) -> ReserveResult:
        now = self.clock.now()

        def _op(conn: sqlite3.Connection) -> ReserveResult:
            row = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if row is None:
                task_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO operations(
                        operation_id, task_id, media_kind, status, cancel_requested,
                        request_fingerprint, snapshot_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 'reserved', 0, ?, ?, ?, ?)
                    """,
                    (operation_id, task_id, media_kind, fingerprint, json.dumps(snapshot), now, now),
                )
                created = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
                return ReserveResult("created", self._row_to_dict(created))  # type: ignore[arg-type]

            existing = self._row_to_dict(row)
            assert existing is not None

            if existing["status"] == "cancelled" or existing["cancel_requested"] == 1:
                raise PluginHttpError(
                    409,
                    PluginError(
                        code="OPERATION_CANCELLED",
                        kind="business",
                        retryable=False,
                        message="Thao tác đã bị hủy trước đó.",
                        scope="operation",
                    ),
                )

            if existing["media_kind"] and existing["media_kind"] != media_kind:
                raise PluginHttpError(
                    409,
                    PluginError(
                        code="IDEMPOTENCY_KEY_CONFLICT",
                        kind="business",
                        retryable=False,
                        message="Idempotency-Key đã được dùng cho loại media khác.",
                        scope="operation",
                    ),
                )

            if existing["request_fingerprint"] and existing["request_fingerprint"] != fingerprint:
                raise PluginHttpError(
                    409,
                    PluginError(
                        code="IDEMPOTENCY_KEY_CONFLICT",
                        kind="business",
                        retryable=False,
                        message="Idempotency-Key trùng nhưng nội dung yêu cầu khác.",
                        scope="operation",
                    ),
                )

            status = existing["status"]
            if status in ("reserved", "submitting", "processing"):
                return ReserveResult("replay_in_progress", existing)
            if status == "succeeded":
                return ReserveResult("replay_succeeded", existing)

            # Retryable pre-submit failure can only be retried by an active POST.
            if status == "failed" and self.is_retryable_pre_submit_failure(existing):
                conn.execute(
                    """
                    UPDATE operations
                    SET status = 'reserved', error_json = NULL, submitted_at = NULL, updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (now, operation_id),
                )
                refreshed = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
                return ReserveResult("retry_allowed", self._row_to_dict(refreshed))  # type: ignore[arg-type]

            return ReserveResult("replay_terminal", existing)

        return self._tx(_op)

    @staticmethod
    def is_retryable_pre_submit_failure(op: dict[str, Any] | None) -> bool:
        """True when an active POST may move a failed operation back to reserved.

        Needs a retryable, uncharged failure AND proof that nothing reached the provider. The submit
        gate stamps submitted_at before the upstream call, so a claimed gate is NOT such proof: only
        an unclaimed gate or an explicit pre_submit failure is. reconcile_needed never qualifies.
        """
        if not op or op.get("status") != "failed":
            return False
        err = op.get("error") or {}
        nothing_submitted = op.get("submitted_at") is None or op.get("submit_stage") == "pre_submit"
        return err.get("retryable") is True and op.get("charged") == "no" and nothing_submitted

    def claim_submit_gate(self, operation_id: str) -> bool:
        """Atomically transition reserved -> submitting if cancel has not won."""
        now = self.clock.now()

        def _op(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT status, cancel_requested FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if row is None:
                return False
            if row["cancel_requested"] == 1 or row["status"] != "reserved":
                if row["cancel_requested"] == 1 and row["status"] == "reserved":
                    conn.execute(
                        "UPDATE operations SET status = 'cancelled', updated_at = ? WHERE operation_id = ?",
                        (now, operation_id),
                    )
                return False
            conn.execute(
                """
                UPDATE operations
                SET status = 'submitting', submitted_at = ?, last_progress_at = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (now, now, now, operation_id),
            )
            return True

        return bool(self._tx(_op))

    def record_upstream_task(self, operation_id: str, upstream_task_id: str) -> dict[str, Any]:
        now = self.clock.now()

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT cancel_requested FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            next_status = "abandoned" if (row and row["cancel_requested"] == 1) else "processing"
            conn.execute(
                """
                UPDATE operations
                SET upstream_task_id = ?, status = ?, last_progress_at = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (upstream_task_id, next_status, now, now, operation_id),
            )
            updated = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            return self._row_to_dict(updated)  # type: ignore[return-value]

        return self._tx(_op)

    def cancel_by_uuid(self, operation_id: str) -> dict[str, Any]:
        """Serialize cancel decision with submit gate; writes durable tombstone if UUID is unknown."""
        now = self.clock.now()

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            if row is None:
                conn.execute(
                    """
                    INSERT INTO operations(
                        operation_id, status, cancel_requested, created_at, updated_at
                    ) VALUES (?, 'cancelled', 1, ?, ?)
                    """,
                    (operation_id, now, now),
                )
                created = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
                return self._row_to_dict(created)  # type: ignore[return-value]

            cur_status = row["status"]
            if cur_status in ("succeeded", "failed", "cancelled", "abandoned", "reconcile_needed"):
                return self._row_to_dict(row)  # type: ignore[return-value]

            if cur_status == "reserved":
                next_status = "cancelled"
            elif cur_status == "submitting" and not row["upstream_task_id"]:
                next_status = "reconcile_needed"
            else:
                next_status = "abandoned"

            err_json = row["error_json"]
            if next_status == "reconcile_needed" and not err_json:
                err_json = json.dumps(
                    PluginError(
                        code="CANCEL_WHILE_SUBMITTING",
                        kind="business",
                        retryable=False,
                        message="Đã yêu cầu dừng trong lúc gửi lệnh tới upstream; giữ trạng thái để đối soát.",
                        scope="operation",
                    ).to_dict()
                )

            conn.execute(
                """
                UPDATE operations
                SET cancel_requested = 1, status = ?, error_json = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (next_status, err_json, now, operation_id),
            )
            updated = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            return self._row_to_dict(updated)  # type: ignore[return-value]

        return self._tx(_op)

    def cancel_by_task_id(self, task_id: str) -> dict[str, Any] | None:
        existing = self.get_by_task_id(task_id)
        if existing is None:
            return None
        return self.cancel_by_uuid(existing["operation_id"])

    def touch_progress(self, operation_id: str) -> None:
        now = self.clock.now()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE operations SET last_progress_at = ?, updated_at = ? WHERE operation_id = ?",
                (now, now, operation_id),
            )

        self._tx(_op)

    def mark_succeeded_video(
        self,
        operation_id: str,
        *,
        media_path: Path,
        upstream_media_id: str | None = None,
    ) -> dict[str, Any]:
        now = self.clock.now()
        media_name = f"{operation_id}.mp4"
        size_bytes = media_path.stat().st_size

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute(
                """
                UPDATE operations
                SET status = 'succeeded',
                    media_name = ?,
                    media_path = ?,
                    media_size_bytes = ?,
                    media_purged = 0,
                    upstream_media_id = COALESCE(?, upstream_media_id),
                    error_json = NULL,
                    charged = 'yes',
                    completed_at = COALESCE(completed_at, ?),
                    last_downloaded_at = ?,
                    last_progress_at = ?,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (media_name, str(media_path), size_bytes, upstream_media_id, now, now, now, now, operation_id),
            )
            row = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            return self._row_to_dict(row)  # type: ignore[return-value]

        res = self._tx(_op)
        self.cleanup_temp_image(operation_id)
        return res

    def mark_succeeded_image(
        self,
        operation_id: str,
        *,
        b64_json: str,
        upstream_media_id: str | None = None,
    ) -> dict[str, Any]:
        now = self.clock.now()

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            conn.execute(
                """
                UPDATE operations
                SET status = 'succeeded',
                    image_b64_json = ?,
                    upstream_media_id = COALESCE(?, upstream_media_id),
                    error_json = NULL,
                    charged = 'yes',
                    completed_at = COALESCE(completed_at, ?),
                    last_progress_at = ?,
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (b64_json, upstream_media_id, now, now, now, operation_id),
            )
            row = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            return self._row_to_dict(row)  # type: ignore[return-value]

        res = self._tx(_op)
        self.cleanup_temp_image(operation_id)
        return res

    def mark_failed(
        self,
        operation_id: str,
        *,
        error: PluginError,
        reconcile: bool = False,
        charged: str = "no",
        recoverable_media_id: str | None = None,
        submit_stage: SubmitStage | None = None,
    ) -> dict[str, Any]:
        """Record a terminal failure. ``submit_stage`` marks where upstream rejected the command.

        ``pre_submit`` is what later lets reserve_operation() allow an active-POST retry for a
        retryable, uncharged failure; without it a claimed submit gate stays terminal.
        """
        now = self.clock.now()
        status = "reconcile_needed" if reconcile else "failed"
        if recoverable_media_id and not error.recoverable_media_id:
            error.recoverable_media_id = recoverable_media_id
        error_payload = error.to_dict()
        if submit_stage is not None:
            error_payload[_SUBMIT_STAGE_KEY] = submit_stage

        def _op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT cancel_requested, status FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            # Keep cancelled/abandoned if user already cancelled unless we now have a recoverable reconcile state
            if row and row["cancel_requested"] == 1 and row["status"] in ("cancelled", "abandoned") and not reconcile:
                updated = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
                return self._row_to_dict(updated)  # type: ignore[return-value]

            conn.execute(
                """
                UPDATE operations
                SET status = ?,
                    error_json = ?,
                    charged = ?,
                    upstream_media_id = COALESCE(?, upstream_media_id),
                    updated_at = ?
                WHERE operation_id = ?
                """,
                (status, json.dumps(error_payload), charged, recoverable_media_id, now, operation_id),
            )
            updated = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
            return self._row_to_dict(updated)  # type: ignore[return-value]

        res = self._tx(_op)
        if status in ("failed", "reconcile_needed"):
            self.cleanup_temp_image(operation_id)
        return res

    # --- Timeouts & crash recovery ---

    def check_abandoned_timeouts(self) -> list[str]:
        """Expire stalled jobs and return the operation_ids whose status changed.

        * ``processing`` rows with no upstream progress for ``abandoned_timeout_s`` become
          ``reconcile_needed`` (outcome unknown, needs manual reconciliation).
        * ``abandoned`` rows (user cancelled after submit) that upstream never confirmed as
          cancelled/completed within ``abandoned_timeout_s`` also become ``reconcile_needed``
          (SPEC 247): upstream may still be running and billing, so we must not assert
          ``cancelled``; the UUID, media ID and cancel tombstone (``cancel_requested``) are kept
          for reconciliation. Either way the poll loop stops instead of polling forever.
        """
        now = self.clock.now()
        timed_out: list[str] = []

        def _op(conn: sqlite3.Connection) -> None:
            rows = conn.execute(
                "SELECT operation_id, status, last_progress_at, submitted_at, created_at, upstream_media_id, error_json "
                "FROM operations WHERE status IN ('processing', 'abandoned')"
            ).fetchall()
            for r in rows:
                ref_ts = r["last_progress_at"] or r["submitted_at"] or r["created_at"]
                if now - float(ref_ts) < self.abandoned_timeout_s:
                    continue
                if r["status"] == "processing":
                    err = PluginError(
                        code="OPERATION_TIMED_OUT",
                        kind="transient",
                        retryable=False,
                        message="Job quá thời gian chờ tối đa mà không có tiến triển từ upstream; giữ trạng thái để đối soát.",
                        scope="operation",
                        recoverable_media_id=r["upstream_media_id"],
                    )
                    conn.execute(
                        "UPDATE operations SET status = 'reconcile_needed', error_json = ?, updated_at = ? WHERE operation_id = ?",
                        (json.dumps(err.to_dict()), now, r["operation_id"]),
                    )
                else:
                    err_json = r["error_json"]
                    if not err_json:
                        err = PluginError(
                            code="OPERATION_TIMED_OUT",
                            kind="transient",
                            retryable=False,
                            message="Đã ghi nhận hủy nhưng upstream không xác nhận trong thời gian chờ; giữ trạng thái để đối soát.",
                            scope="operation",
                            recoverable_media_id=r["upstream_media_id"],
                        )
                        err_json = json.dumps(err.to_dict())
                    conn.execute(
                        "UPDATE operations SET status = 'reconcile_needed', error_json = ?, updated_at = ? WHERE operation_id = ?",
                        (err_json, now, r["operation_id"]),
                    )
                timed_out.append(r["operation_id"])

        self._tx(_op)
        return timed_out

    def recover_on_startup(self) -> list[dict[str, Any]]:
        """Reconcile interrupted rows after process restart and return jobs to resume polling.

        - ``reserved`` (never claimed the submit gate): nothing was sent upstream, so mark it
          ``failed`` with a retryable, uncharged ``INTERRUPTED_BEFORE_SUBMIT`` error. The same
          Idempotency-Key can then be re-POSTed and goes through the ``retry_allowed`` branch.
        - ``submitting`` without ``upstream_task_id``: ``reconcile_needed`` (may have been charged).
        - ``submitting``/``processing`` with ``upstream_task_id``: ``reconcile_needed`` when the
          abandoned timeout already elapsed, otherwise returned so polling resumes.
        - ``cancel_requested`` rows are honoured first regardless of status.
        """
        now = self.clock.now()
        to_resume: list[dict[str, Any]] = []
        interrupted: list[str] = []

        def _op(conn: sqlite3.Connection) -> None:
            rows = conn.execute(
                "SELECT * FROM operations WHERE status IN ('reserved', 'submitting', 'processing')"
            ).fetchall()
            for r in rows:
                op = self._row_to_dict(r)
                assert op is not None
                op_id = op["operation_id"]
                status = op["status"]

                if op["cancel_requested"] == 1:
                    if status == "reserved":
                        conn.execute(
                            "UPDATE operations SET status = 'cancelled', updated_at = ? WHERE operation_id = ?",
                            (now, op_id),
                        )
                    elif not op["upstream_task_id"]:
                        err = PluginError(
                            code="CANCEL_WHILE_SUBMITTING",
                            kind="business",
                            retryable=False,
                            message="Tiến trình dừng trước khi ghi nhận upstream task ID; cần đối soát.",
                            scope="operation",
                        )
                        conn.execute(
                            "UPDATE operations SET status = 'reconcile_needed', error_json = ?, updated_at = ? WHERE operation_id = ?",
                            (json.dumps(err.to_dict()), now, op_id),
                        )
                    else:
                        conn.execute(
                            "UPDATE operations SET status = 'abandoned', updated_at = ? WHERE operation_id = ?",
                            (now, op_id),
                        )
                    continue

                if status == "submitting" and not op["upstream_task_id"]:
                    err = PluginError(
                        code="CRASH_BEFORE_UPSTREAM_ID",
                        kind="transient",
                        retryable=False,
                        message="Plugin khởi động lại sau thời điểm có thể đã submit nhưng chưa lưu được upstream task ID.",
                        scope="operation",
                    )
                    conn.execute(
                        "UPDATE operations SET status = 'reconcile_needed', error_json = ?, charged = 'maybe', updated_at = ? WHERE operation_id = ?",
                        (json.dumps(err.to_dict()), now, op_id),
                    )
                    continue

                if status in ("submitting", "processing") and op["upstream_task_id"]:
                    ref_ts = op["last_progress_at"] or op["submitted_at"] or op["created_at"]
                    if now - float(ref_ts) >= self.abandoned_timeout_s:
                        err = PluginError(
                            code="OPERATION_TIMED_OUT",
                            kind="transient",
                            retryable=False,
                            message="Job vượt quá giới hạn chờ sau restart; giữ trạng thái để đối soát.",
                            scope="operation",
                            recoverable_media_id=op.get("upstream_media_id"),
                        )
                        conn.execute(
                            "UPDATE operations SET status = 'reconcile_needed', error_json = ?, updated_at = ? WHERE operation_id = ?",
                            (json.dumps(err.to_dict()), now, op_id),
                        )
                    else:
                        if status == "submitting":
                            conn.execute(
                                "UPDATE operations SET status = 'processing', updated_at = ? WHERE operation_id = ?",
                                (now, op_id),
                            )
                            op["status"] = "processing"
                        to_resume.append(op)
                    continue

                if status == "reserved":
                    # Row never claimed the submit gate, so upstream has not seen anything and
                    # nothing was charged. Fail it (retryable, submitted_at NULL) so a re-POST with
                    # the same Idempotency-Key hits the ``retry_allowed`` branch instead of
                    # replaying 202 forever.
                    err = PluginError(
                        code="INTERRUPTED_BEFORE_SUBMIT",
                        kind="transient",
                        retryable=True,
                        message="Plugin dừng trước khi gửi yêu cầu lên upstream; gửi lại với cùng Idempotency-Key.",
                        scope="operation",
                    )
                    conn.execute(
                        "UPDATE operations SET status = 'failed', error_json = ?, charged = 'no', submitted_at = NULL, updated_at = ? WHERE operation_id = ?",
                        (json.dumps(err.to_dict()), now, op_id),
                    )
                    interrupted.append(op_id)

        self._tx(_op)
        for op_id in interrupted:
            self.cleanup_temp_image(op_id)
        return to_resume

    def count_active_jobs(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM operations WHERE status IN ('submitting', 'processing')"
            ).fetchone()
            return int(row["cnt"]) if row else 0

    # --- Media download tracking & retention cleanup ---

    def begin_media_download(self, media_name: str) -> None:
        with self._lock:
            self._active_downloads.add(media_name)

    def end_media_download(self, media_name: str, *, completed: bool) -> None:
        now = self.clock.now()
        with self._lock:
            self._active_downloads.discard(media_name)
        if completed:
            def _op(conn: sqlite3.Connection) -> None:
                conn.execute(
                    "UPDATE operations SET last_downloaded_at = ?, updated_at = ? WHERE media_name = ?",
                    (now, now, media_name),
                )

            self._tx(_op)

    def begin_recovery(self, operation_id: str) -> None:
        with self._lock:
            self._active_recoveries.add(operation_id)

    def end_recovery(self, operation_id: str) -> None:
        with self._lock:
            self._active_recoveries.discard(operation_id)

    def run_media_cleanup(self) -> dict[str, Any]:
        """Enforce 7-day min retention, 30-day max unaccessed expiry, and 10 GiB soft budget."""
        now = self.clock.now()
        deleted_names: list[str] = []

        def _collect_and_purge(conn: sqlite3.Connection) -> dict[str, Any]:
            rows = conn.execute(
                """
                SELECT operation_id, media_name, media_path, media_size_bytes,
                       completed_at, last_downloaded_at, status
                FROM operations
                WHERE media_kind = 'video'
                  AND media_name IS NOT NULL
                  AND media_purged = 0
                  AND status NOT IN ('processing', 'reconcile_needed', 'reserved', 'submitting')
                """
            ).fetchall()

            candidates: list[dict[str, Any]] = []
            total_bytes = 0
            for r in rows:
                p = Path(r["media_path"]) if r["media_path"] else None
                size = int(r["media_size_bytes"] or 0)
                if p and p.is_file():
                    size = p.stat().st_size
                total_bytes += size
                last_access = max(float(r["completed_at"] or 0), float(r["last_downloaded_at"] or 0))
                candidates.append(
                    {
                        "operation_id": r["operation_id"],
                        "media_name": r["media_name"],
                        "media_path": p,
                        "size": size,
                        "last_access": last_access,
                    }
                )

            def _is_busy(item: dict[str, Any]) -> bool:
                return (
                    item["media_name"] in self._active_downloads
                    or item["operation_id"] in self._active_recoveries
                )

            # Pass 1: Delete files unaccessed for >= 30 days
            remaining: list[dict[str, Any]] = []
            for item in candidates:
                age = now - item["last_access"]
                if not _is_busy(item) and age >= MAX_MEDIA_UNACCESSED_S:
                    if item["media_path"]:
                        item["media_path"].unlink(missing_ok=True)
                    conn.execute(
                        "UPDATE operations SET media_purged = 1, updated_at = ? WHERE operation_id = ?",
                        (now, item["operation_id"]),
                    )
                    total_bytes -= item["size"]
                    deleted_names.append(item["media_name"])
                else:
                    remaining.append(item)

            # Pass 2: If still above soft budget, delete eligible (>= 7 days) oldest-accessed first
            if total_bytes > self.media_budget_bytes:
                eligible = [
                    item for item in remaining
                    if not _is_busy(item) and (now - item["last_access"]) >= MIN_MEDIA_RETENTION_S
                ]
                eligible.sort(key=lambda x: x["last_access"])
                for item in eligible:
                    if total_bytes <= self.media_budget_bytes:
                        break
                    if item["media_path"]:
                        item["media_path"].unlink(missing_ok=True)
                    conn.execute(
                        "UPDATE operations SET media_purged = 1, updated_at = ? WHERE operation_id = ?",
                        (now, item["operation_id"]),
                    )
                    total_bytes -= item["size"]
                    deleted_names.append(item["media_name"])

            return {
                "deleted": deleted_names,
                "remaining_bytes": max(0, total_bytes),
                "storage_pressure": total_bytes > self.media_budget_bytes,
            }

        return self._tx(_collect_and_purge)
