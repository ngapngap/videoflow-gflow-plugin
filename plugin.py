"""VideoFlow gflow-cli plugin HTTP bridge (Contract v1)."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hmac
import json
import logging
import os
import re
import subprocess
import sys
import tomllib
import uuid
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from gflow_errors import (
    PluginError,
    PluginHttpError,
    RedactingLogFilter,
    sanitize_message,
    translate_upstream_error,
)
from gflow_models import (
    DEFAULT_CATALOG_PAYLOAD,
    ModelCatalogManager,
    parse_and_validate_image_request,
    parse_and_validate_video_request,
)
from mcp_client import GflowMcpClient, McpTransportError, UpstreamCompatibilityError
from storage import (
    Clock,
    JobStore,
    SubmitStage,
    SystemClock,
    validate_confined_path,
    validate_mp4_file,
)

logger = logging.getLogger("videoflow_gflow")
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(slots=True)
class ScopeBlock:
    http_status: int
    error: PluginError
    expires_at: float | None  # None means requires explicit health/quota recovery


@dataclass(slots=True)
class PluginRuntime:
    data_dir: Path
    gflow_home: Path
    output_dir: Path
    tmp_dir: Path
    config_snapshot: dict[str, Any]
    token: str
    proxy: str
    daemon_token: str
    profile: str
    store: JobStore
    mcp: GflowMcpClient
    catalog: ModelCatalogManager
    clock: Clock
    redactor: RedactingLogFilter
    secrets: tuple[str, ...]
    identity_degraded: bool = False
    identity_message: str = ""
    auth_degraded: bool = False
    auth_message: str = ""
    browser_lost: bool = False
    compatibility_error: str | None = None
    storage_pressure: bool = False
    generation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    instance_block: ScopeBlock | None = None
    media_kind_blocks: dict[str, ScopeBlock] = field(default_factory=dict)
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    poll_interval_s: float = 0.25
    image_sync_wait_s: float = 2.0
    startup_complete: bool = False
    prompts_seen: set[str] = field(default_factory=set)

    def register_prompt(self, prompt: str) -> None:
        if prompt:
            self.prompts_seen.add(prompt)
            self.redactor.register_prompt(prompt)

    def track_task(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task


def load_config_snapshot(config_file: Path) -> dict[str, Any]:
    """Read host config_file once at startup."""
    if not config_file.is_file():
        return {}
    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_secrets_from_env() -> tuple[str, str, str]:
    """Read VF_PLUGIN_TOKEN and VF_PLUGIN_SECRETS_JSON from environment."""
    token = os.environ.get("VF_PLUGIN_TOKEN", "")
    raw_secrets = os.environ.get("VF_PLUGIN_SECRETS_JSON", "{}")
    proxy = ""
    daemon_token = ""
    try:
        parsed = json.loads(raw_secrets) if raw_secrets.strip() else {}
        if isinstance(parsed, dict):
            proxy = str(parsed.get("proxy") or "")
            daemon_token = str(parsed.get("daemon_token") or "")
    except Exception:
        pass
    return token, proxy, daemon_token


def resolve_profile_name(config_snapshot: dict[str, Any], gflow_home: Path) -> tuple[str, bool, str]:
    """Resolve effective Flow profile name following gflow-cli precedence."""
    cfg_profile = str(config_snapshot.get("profile") or "").strip()
    if cfg_profile:
        if not _PROFILE_NAME_RE.match(cfg_profile) or ".." in cfg_profile:
            return cfg_profile, True, "Tên profile không hợp lệ (không được chứa dấu phân cách đường dẫn)."
        return cfg_profile, False, ""

    env_profile = os.environ.get("GFLOW_CLI_PROFILE", "").strip()
    if env_profile:
        if not _PROFILE_NAME_RE.match(env_profile) or ".." in env_profile:
            return env_profile, True, "Biến GFLOW_CLI_PROFILE không hợp lệ."
        return env_profile, False, ""

    config_toml = gflow_home / "config.toml"
    if config_toml.is_file():
        try:
            parsed = tomllib.loads(config_toml.read_text(encoding="utf-8"))
            dp = str(parsed.get("default_profile") or "").strip()
            if dp and _PROFILE_NAME_RE.match(dp):
                return dp, False, ""
        except Exception:
            pass

    if gflow_home.is_dir():
        profile_dirs = [
            p.name[len("profile_") :]
            for p in gflow_home.iterdir()
            if p.is_dir() and p.name.startswith("profile_") and len(p.name) > len("profile_")
        ]
        if len(profile_dirs) == 1 and _PROFILE_NAME_RE.match(profile_dirs[0]):
            return profile_dirs[0], False, ""

    return "default", False, ""


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def detect_stale_browser_lock(gflow_home: Path, profile: str, active_jobs: int) -> tuple[bool, str]:
    """Distinguish idle profile (leftover lease file is normal) from lost active browser."""
    if active_jobs <= 0:
        return False, "Profile rảnh"
    profile_dir = gflow_home / f"profile_{profile}"
    if not profile_dir.is_dir():
        return False, "Đang xử lý"
    for lock_name in ("lease.pid", "chrome.pid", "SingletonLock.pid"):
        lock_file = profile_dir / lock_name
        if lock_file.is_file():
            try:
                pid_text = lock_file.read_text(encoding="utf-8").strip()
                pid = int(pid_text)
                if not _is_pid_alive(pid):
                    return True, "Trình duyệt đang xử lý đã bị mất (stale lock)."
            except Exception:
                return True, "File khóa trình duyệt bị hỏng trong lúc đang xử lý."
    return False, "Đang xử lý"


def _operation_to_record(op: dict[str, Any]) -> dict[str, Any]:
    """Convert internal DB operation row into Contract v1 operation JSON."""
    raw_status = op["status"]
    status = "processing" if raw_status in ("reserved", "submitting") else raw_status
    media_kind = op.get("media_kind")
    rec: dict[str, Any] = {
        "operation_id": op["operation_id"],
        "status": status,
    }
    if media_kind:
        rec["media_kind"] = media_kind
    if op.get("task_id"):
        rec["task_id"] = op["task_id"]

    if media_kind == "video":
        if status == "succeeded":
            if op.get("media_purged") == 1:
                rec["status"] = "reconcile_needed"
                rec["error"] = PluginError(
                    code="MEDIA_EXPIRED",
                    kind="not_found",
                    retryable=False,
                    message="File media đã hết thời hạn lưu cục bộ; có thể khôi phục từ upstream.",
                    scope="operation",
                    recoverable_media_id=op.get("upstream_media_id"),
                ).to_dict()
                return rec
            if op.get("media_name"):
                rec["media_name"] = op["media_name"]
    elif media_kind == "image":
        if status == "succeeded" and op.get("image_b64_json"):
            rec["data"] = [{"b64_json": op["image_b64_json"]}]

    if rec["status"] in ("failed", "reconcile_needed") and op.get("error"):
        rec["error"] = op["error"]
    return rec


def _check_scope_blocks(rt: PluginRuntime, media_kind: str) -> None:
    now = rt.clock.now()
    if rt.instance_block is not None:
        blk = rt.instance_block
        if blk.expires_at is not None and now >= blk.expires_at:
            rt.instance_block = None
        else:
            err = PluginError(
                code=blk.error.code,
                kind=blk.error.kind,
                retryable=blk.error.retryable,
                message=blk.error.message,
                scope="instance",
                retry_after_s=max(1, int(blk.expires_at - now)) if blk.expires_at else blk.error.retry_after_s,
            )
            raise PluginHttpError(blk.http_status, err)

    mk_blk = rt.media_kind_blocks.get(media_kind)
    if mk_blk is not None:
        if mk_blk.expires_at is not None and now >= mk_blk.expires_at:
            rt.media_kind_blocks.pop(media_kind, None)
        else:
            err = PluginError(
                code=mk_blk.error.code,
                kind=mk_blk.error.kind,
                retryable=mk_blk.error.retryable,
                message=mk_blk.error.message,
                scope="media_kind",
                retry_after_s=max(1, int(mk_blk.expires_at - now)) if mk_blk.expires_at else mk_blk.error.retry_after_s,
            )
            raise PluginHttpError(mk_blk.http_status, err)


def _record_scope_block(rt: PluginRuntime, http_status: int, err: PluginError, media_kind: str) -> None:
    now = rt.clock.now()
    expires_at = (now + err.retry_after_s) if err.retry_after_s else None
    if err.scope == "instance":
        rt.instance_block = ScopeBlock(http_status=http_status, error=err, expires_at=expires_at)
    elif err.scope == "media_kind":
        rt.media_kind_blocks[media_kind] = ScopeBlock(http_status=http_status, error=err, expires_at=expires_at)


async def _ensure_project_id(rt: PluginRuntime, snapshot: dict[str, Any]) -> str | None:
    explicit = str(snapshot.get("project_id") or "").strip()
    if explicit:
        return explicit
    stored = rt.store.get_meta("default_project_id")
    if stored:
        return stored
    return None


async def _refresh_catalog(rt: PluginRuntime) -> None:
    try:
        report = await rt.mcp.probe_compatibility()
        rt.compatibility_error = None
        raw = await rt.mcp.list_models()
        image_enabled = "generate_image" in report.resolved_tools
        rt.catalog.update_from_upstream(raw, video_enabled=True, image_enabled=image_enabled)
    except UpstreamCompatibilityError as exc:
        rt.compatibility_error = str(exc)
        if not rt.catalog.has_cache():
            rt.catalog.update_from_upstream(DEFAULT_CATALOG_PAYLOAD, video_enabled=True, image_enabled=False)
        rt.catalog.mark_stale()
    except McpTransportError:
        if not rt.catalog.has_cache():
            rt.catalog.update_from_upstream(DEFAULT_CATALOG_PAYLOAD, video_enabled=True, image_enabled=False)
        rt.catalog.mark_stale()


async def _run_video_operation(rt: PluginRuntime, operation_id: str, temp_image_path: Path) -> None:
    op = rt.store.get_by_uuid(operation_id)
    if op is None:
        return
    snapshot = op.get("snapshot") or {}
    prompt = str(snapshot.get("_prompt") or "")

    async with rt.generation_lock:
        if not rt.store.claim_submit_gate(operation_id):
            rt.store.cleanup_temp_image(operation_id)
            return

        project_id = await _ensure_project_id(rt, snapshot)
        out_file = str(rt.output_dir / f"{operation_id}.mp4")

        try:
            res = await rt.mcp.generate_video(
                prompt=prompt,
                model=str(snapshot.get("upstream_model") or snapshot.get("video_model") or "veo_fast"),
                aspect=str(snapshot.get("aspect") or "16:9"),
                duration=int(snapshot.get("seconds") or 8),
                initial_frame=str(temp_image_path),
                profile=rt.profile,
                project=project_id,
                out_file=out_file,
                max_credits=snapshot.get("max_credits_per_clip"),
                wait=False,
            )
        except (McpTransportError, UpstreamCompatibilityError) as exc:
            err = PluginError(
                code="UPSTREAM_SUBMIT_UNCERTAIN",
                kind="transient",
                retryable=False,
                message=sanitize_message(str(exc), secrets=rt.secrets, prompts=rt.prompts_seen),
                scope="operation",
            )
            rt.store.mark_failed(operation_id, error=err, reconcile=True, charged="maybe")
            return

        if res.get("project_id") and not project_id:
            rt.store.set_meta("default_project_id", str(res["project_id"]))

        if res.get("_mcp_is_error") or res.get("status") in ("failed", "error", "rate_limited") or res.get("type"):
            # Upstream only asserts charged=no when the command failed before any submit side effect
            stage: SubmitStage = "pre_submit" if res.get("charged") == "no" else "post_submit"
            http_st, err, reconcile = translate_upstream_error(
                res,
                media_kind="video",
                stage=stage,
                secrets=rt.secrets,
                prompts=rt.prompts_seen,
            )
            _record_scope_block(rt, http_st, err, "video")
            if err.code == "UPSTREAM_AUTH_REQUIRED":
                rt.auth_degraded = True
                rt.auth_message = err.message
            rt.store.mark_failed(
                operation_id,
                error=err,
                reconcile=reconcile,
                charged=str(res.get("charged") or "no"),
                recoverable_media_id=err.recoverable_media_id,
                submit_stage=stage,
            )
            return

        upstream_task_id = str(res.get("task_id") or "")
        if not upstream_task_id:
            err = PluginError(
                code="MISSING_UPSTREAM_TASK_ID",
                kind="business",
                retryable=False,
                message="Upstream không trả về task_id; giữ trạng thái để đối soát.",
                scope="operation",
            )
            rt.store.mark_failed(operation_id, error=err, reconcile=True, charged="maybe")
            return

        op = rt.store.record_upstream_task(operation_id, upstream_task_id)
        if op["status"] == "abandoned":
            try:
                await rt.mcp.cancel_task(upstream_task_id)
            except Exception:
                pass

    await _poll_video_task(rt, operation_id, upstream_task_id)


async def _poll_video_task(rt: PluginRuntime, operation_id: str, upstream_task_id: str) -> None:
    last_seen_status: str | None = None
    while True:
        rt.store.check_abandoned_timeouts()
        op = rt.store.get_by_uuid(operation_id)
        if op is None or op["status"] in ("succeeded", "failed", "cancelled", "reconcile_needed"):
            return

        try:
            task_res = await rt.mcp.get_task(upstream_task_id)
        except McpTransportError:
            await asyncio.sleep(rt.poll_interval_s)
            continue

        task_status = str(task_res.get("status") or "").lower()
        # Re-read after the (slow) probe: DELETE may have abandoned the job while get_task was in flight.
        # A user-cancelled job that upstream still reports as "working" is NOT progress, so never refresh
        # last_progress_at for it — otherwise the abandoned_timeout_s window is pushed out forever (issue #14).
        # Terminal upstream answers (cancelled/completed/failed) below still run so the outcome is recorded.
        cancel_requested = op["status"] == "abandoned" or op.get("cancel_requested") == 1
        if not cancel_requested:
            cur = rt.store.get_by_uuid(operation_id)
            cancel_requested = cur is not None and (cur["status"] == "abandoned" or cur.get("cancel_requested") == 1)
        if not cancel_requested and task_status != last_seen_status and task_status in ("working", "running", "processing"):
            last_seen_status = task_status
            rt.store.touch_progress(operation_id)

        if task_status == "cancelled":
            # Keep abandoned if user already requested cancel after submit, else cancelled
            if op["status"] != "abandoned":
                rt.store.cancel_by_uuid(operation_id)
            return

        if task_status == "completed":
            result_obj = task_res.get("result") if isinstance(task_res.get("result"), dict) else {}
            is_err = bool(result_obj.get("isError"))
            structured = (
                result_obj.get("structuredContent")
                if isinstance(result_obj.get("structuredContent"), dict)
                else result_obj
            )
            if is_err or structured.get("status") in ("failed", "error", "rate_limited") or structured.get("type"):
                http_st, err, reconcile = translate_upstream_error(
                    structured,
                    media_kind="video",
                    stage="post_submit",
                    secrets=rt.secrets,
                    prompts=rt.prompts_seen,
                )
                if err.code == "BROWSER_SESSION_CLOSED":
                    rt.browser_lost = True
                _record_scope_block(rt, http_st, err, "video")
                rt.store.mark_failed(
                    operation_id,
                    error=err,
                    reconcile=reconcile,
                    charged=str(structured.get("charged") or "maybe"),
                    recoverable_media_id=err.recoverable_media_id,
                )
                return

            if structured.get("project_id") and not rt.store.get_meta("default_project_id"):
                rt.store.set_meta("default_project_id", str(structured["project_id"]))

            files = structured.get("files") or []
            raw_path = structured.get("file") or (files[0] if files else None) or str(rt.output_dir / f"{operation_id}.mp4")
            upstream_media_id = structured.get("media_id") or structured.get("operation_id")
            if not upstream_media_id and isinstance(structured.get("media_ids"), list) and structured["media_ids"]:
                upstream_media_id = structured["media_ids"][0]

            try:
                confined = validate_confined_path(str(raw_path), rt.output_dir)
            except ValueError:
                err = PluginError(
                    code="MEDIA_PATH_OUT_OF_BOUNDS",
                    kind="business",
                    retryable=False,
                    message="Đường dẫn media trả về nằm ngoài thư mục output cho phép.",
                    scope="operation",
                    recoverable_media_id=str(upstream_media_id) if upstream_media_id else None,
                )
                rt.store.mark_failed(operation_id, error=err, reconcile=True, charged="yes")
                return

            if not validate_mp4_file(confined):
                err = PluginError(
                    code="INVALID_MP4_FILE",
                    kind="business",
                    retryable=False,
                    message="File MP4 tải về bị hỏng hoặc không hợp lệ.",
                    scope="operation",
                    recoverable_media_id=str(upstream_media_id) if upstream_media_id else None,
                )
                rt.store.mark_failed(
                    operation_id,
                    error=err,
                    reconcile=True,
                    charged="yes",
                    recoverable_media_id=str(upstream_media_id) if upstream_media_id else None,
                )
                return

            rt.browser_lost = False  # a completed video proves the browser session is alive again
            rt.store.mark_succeeded_video(
                operation_id,
                media_path=confined,
                upstream_media_id=str(upstream_media_id) if upstream_media_id else None,
            )
            return

        if task_status == "failed":
            err = PluginError(
                code="UPSTREAM_TASK_FAILED",
                kind="transient",
                retryable=False,
                message=sanitize_message(
                    str(task_res.get("statusMessage") or "Tác vụ upstream thất bại."),
                    secrets=rt.secrets,
                    prompts=rt.prompts_seen,
                ),
                scope="operation",
            )
            rt.store.mark_failed(operation_id, error=err, reconcile=True, charged="maybe")
            return

        await asyncio.sleep(rt.poll_interval_s)


async def _run_image_operation(rt: PluginRuntime, operation_id: str, temp_image_path: Path) -> None:
    op = rt.store.get_by_uuid(operation_id)
    if op is None:
        return
    snapshot = op.get("snapshot") or {}
    prompt = str(snapshot.get("_prompt") or "")

    async with rt.generation_lock:
        if not rt.store.claim_submit_gate(operation_id):
            rt.store.cleanup_temp_image(operation_id)
            return

        project_id = await _ensure_project_id(rt, snapshot)
        out_file = str(rt.output_dir / f"{operation_id}.png")

        try:
            res = await rt.mcp.generate_image(
                prompt=prompt,
                model=str(snapshot.get("upstream_model") or snapshot.get("image_model") or "nano2"),
                aspect=str(snapshot.get("aspect_ratio") or "1:1"),
                reference_images=[str(temp_image_path)],
                profile=rt.profile,
                project=project_id,
                out_file=out_file,
                wait=True,
            )
        except (McpTransportError, UpstreamCompatibilityError) as exc:
            err = PluginError(
                code="UPSTREAM_IMAGE_UNCERTAIN",
                kind="transient",
                retryable=False,
                message=sanitize_message(str(exc), secrets=rt.secrets, prompts=rt.prompts_seen),
                scope="operation",
            )
            rt.store.mark_failed(operation_id, error=err, reconcile=True, charged="maybe")
            return

        if res.get("project_id") and not project_id:
            rt.store.set_meta("default_project_id", str(res["project_id"]))

        if res.get("_mcp_is_error") or res.get("status") in ("failed", "error", "rate_limited") or res.get("type"):
            # Upstream only asserts charged=no when the command failed before any submit side effect
            stage: SubmitStage = "pre_submit" if res.get("charged") == "no" else "post_submit"
            http_st, err, reconcile = translate_upstream_error(
                res,
                media_kind="image",
                stage=stage,
                secrets=rt.secrets,
                prompts=rt.prompts_seen,
            )
            _record_scope_block(rt, http_st, err, "image")
            rt.store.mark_failed(
                operation_id,
                error=err,
                reconcile=reconcile,
                charged=str(res.get("charged") or "no"),
                recoverable_media_id=err.recoverable_media_id,
                submit_stage=stage,
            )
            return

        b64_val = res.get("b64_json")
        if not b64_val and isinstance(res.get("data"), list) and res["data"]:
            b64_val = res["data"][0].get("b64_json")
        if not b64_val:
            files = res.get("files") or []
            raw_path = res.get("file") or (files[0] if files else None)
            if raw_path:
                try:
                    confined = validate_confined_path(str(raw_path), rt.output_dir)
                    if confined.is_file():
                        b64_val = base64.b64encode(confined.read_bytes()).decode("ascii")
                except ValueError:
                    b64_val = None

        if not b64_val:
            err = PluginError(
                code="MISSING_IMAGE_RESULT",
                kind="business",
                retryable=False,
                message="Không nhận được dữ liệu ảnh từ upstream; cần đối soát.",
                scope="operation",
            )
            rt.store.mark_failed(operation_id, error=err, reconcile=True, charged="maybe")
            return

        media_id = res.get("media_id") or res.get("operation_id")
        rt.store.mark_succeeded_image(
            operation_id,
            b64_json=str(b64_val),
            upstream_media_id=str(media_id) if media_id else None,
        )


def create_app(runtime: PluginRuntime) -> FastAPI:
    """Create configured FastAPI application bound to a PluginRuntime."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        rt = runtime
        cleanup_res = rt.store.run_media_cleanup()
        rt.storage_pressure = bool(cleanup_res.get("storage_pressure"))
        resumed = rt.store.recover_on_startup()
        await _refresh_catalog(rt)
        # recover_on_startup() only returns jobs that already hold an upstream_task_id; reserved
        # rows that never reached the submit gate are failed as INTERRUPTED_BEFORE_SUBMIT there.
        for op in resumed:
            if op.get("media_kind") == "video" and op.get("upstream_task_id"):
                rt.track_task(_poll_video_task(rt, op["operation_id"], op["upstream_task_id"]))
        rt.startup_complete = True
        try:
            yield
        finally:
            for t in list(rt.background_tasks):
                t.cancel()
            if rt.background_tasks:
                await asyncio.gather(*rt.background_tasks, return_exceptions=True)
            rt.store.close()

    app = FastAPI(title="VideoFlow gflow-cli Plugin", version="0.1.0", lifespan=lifespan)
    app.state.rt = runtime

    @app.middleware("http")
    async def bearer_auth_middleware(request: Request, call_next: Any):
        rt: PluginRuntime = request.app.state.rt
        auth = request.headers.get("Authorization", "")
        expected = f"Bearer {rt.token}"
        if not rt.token or not hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
            err = PluginError(
                code="PLUGIN_TOKEN_INVALID",
                kind="auth",
                retryable=False,
                message="Bearer token không hợp lệ hoặc bị thiếu.",
                scope="instance",
            )
            return JSONResponse(status_code=401, content=err.to_envelope(secrets=rt.secrets))
        return await call_next(request)

    @app.exception_handler(PluginHttpError)
    async def plugin_http_error_handler(request: Request, exc: PluginHttpError):
        rt: PluginRuntime = request.app.state.rt
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.error.to_envelope(secrets=rt.secrets, prompts=rt.prompts_seen),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        rt: PluginRuntime = request.app.state.rt
        err = PluginError(
            code="INTERNAL_PLUGIN_ERROR",
            kind="transient",
            retryable=False,
            message=sanitize_message(f"Lỗi nội bộ plugin: {type(exc).__name__}", secrets=rt.secrets),
            scope="instance",
        )
        return JSONResponse(status_code=500, content=err.to_envelope(secrets=rt.secrets))

    def _require_ready_for_new_job(rt: PluginRuntime, media_kind: str) -> None:
        if rt.identity_degraded:
            raise PluginHttpError(
                424,
                PluginError(
                    code="PROFILE_IDENTITY_MISMATCH",
                    kind="auth",
                    retryable=False,
                    message=rt.identity_message or "Danh tính profile đã thay đổi; hãy tạo instance mới.",
                    scope="instance",
                ),
            )
        if rt.compatibility_error:
            raise PluginHttpError(
                503,
                PluginError(
                    code="UPSTREAM_INCOMPATIBLE",
                    kind="unsupported",
                    retryable=False,
                    message=rt.compatibility_error,
                    scope="instance",
                ),
            )
        if rt.storage_pressure:
            raise PluginHttpError(
                503,
                PluginError(
                    code="STORAGE_BUDGET_EXCEEDED",
                    kind="transient",
                    retryable=False,
                    message="Dung lượng lưu trữ media vượt ngưỡng và không còn file đủ điều kiện dọn.",
                    scope="instance",
                ),
            )
        _check_scope_blocks(rt, media_kind)

    @app.get("/healthz")
    async def healthz():
        rt = runtime
        active_jobs = rt.store.count_active_jobs()
        stale_lock, lock_msg = detect_stale_browser_lock(rt.gflow_home, rt.profile, active_jobs)

        # Always probe auth_status (unless the probe itself fails) so a previous
        # auth_degraded / browser_lost flag never stays sticky: the flag is re-derived
        # from the live upstream answer on every call and cleared once the user
        # re-authenticates.
        auth_probe_failed = False
        try:
            auth_res = await rt.mcp.auth_status(rt.profile)
            st = str(auth_res.get("status") or "authenticated")
            if st in ("authenticated", "unknown"):
                rt.auth_degraded = False
                rt.auth_message = ""
                rt.browser_lost = False
                if rt.instance_block and rt.instance_block.error.kind == "auth":
                    rt.instance_block = None
                if auth_res.get("user_email"):
                    email = str(auth_res["user_email"])
                    bound_email = rt.store.get_meta("bound_account_email")
                    if bound_email and bound_email != email:
                        rt.identity_degraded = True
                        rt.identity_message = "Tài khoản đăng nhập không khớp với lịch sử instance."
                    elif not bound_email:
                        rt.store.set_meta("bound_account_email", email)
            else:
                rt.auth_degraded = True
                rt.auth_message = "Cần đăng nhập lại"
        except Exception:
            # Probe broken: keep the last known auth verdict instead of inventing one.
            auth_probe_failed = True

        browser_lost = rt.browser_lost or stale_lock

        checks: dict[str, dict[str, str]] = {
            "http": {"status": "ok", "message": "Sẵn sàng"},
            "browser": {
                "status": "degraded" if browser_lost else "ok",
                "message": "Trình duyệt đang xử lý đã bị mất" if rt.browser_lost else lock_msg,
            },
        }
        if rt.identity_degraded:
            checks["identity"] = {"status": "degraded", "message": rt.identity_message}
        if rt.compatibility_error:
            checks["upstream"] = {"status": "degraded", "message": rt.compatibility_error}
        if rt.auth_degraded:
            checks["account"] = {"status": "degraded", "message": rt.auth_message or "Cần đăng nhập lại"}
        elif auth_probe_failed:
            checks["account"] = {"status": "ok", "message": "Chưa kiểm tra upstream"}
        else:
            checks["account"] = {"status": "ok", "message": "Đã xác thực"}

        degraded = any(c["status"] != "ok" for c in checks.values()) or (
            rt.instance_block is not None and rt.instance_block.expires_at is None
        )
        overall = "degraded" if degraded else "ok"
        code = 503 if degraded else 200
        return JSONResponse(
            status_code=code,
            content={
                "status": overall,
                "checks": checks,
                "message": "Tạm dừng nhận job mới" if degraded else "Sẵn sàng",
            },
        )

    @app.get("/vf-plugin/v1/capabilities")
    async def capabilities():
        return runtime.catalog.build_capabilities_response()

    @app.get("/vf-plugin/v1/quota")
    async def quota():
        rt = runtime
        caps = rt.catalog.build_capabilities_response()
        has_image = "image" in caps.get("kinds", [])

        video_status = "unknown"
        video_balance: int | None = None
        video_total: int | None = None
        video_reset: str | None = None
        acct_label = rt.profile

        try:
            res = await rt.mcp.get_credits(rt.profile)
            if res.get("_mcp_is_error") or res.get("type"):
                slug = str(res.get("type") or "").rsplit("/", 1)[-1]
                if "auth" in slug:
                    video_status = "auth_required"
                elif "profile-locked" in slug:
                    video_status = "locked"
                else:
                    video_status = "unknown"
            else:
                bal = res.get("credits") if res.get("credits") is not None else res.get("balance")
                if bal is not None:
                    video_balance = int(bal)
                    video_status = "exhausted" if video_balance <= 0 else "ok"
                    if video_balance > 0:
                        rt.media_kind_blocks.pop("video", None)
                if res.get("total") is not None:
                    video_total = int(res["total"])
                video_reset = res.get("reset_at")
                if res.get("label"):
                    acct_label = str(res["label"])
        except (McpTransportError, UpstreamCompatibilityError):
            video_status = "unknown"

        limits: list[dict[str, Any]] = [
            {
                "media_kind": "video",
                "unit": "credits",
                "accounts": [
                    {
                        "id": rt.profile,
                        "label": acct_label,
                        "balance": video_balance,
                        "total": video_total,
                        "reset_at": video_reset,
                        "status": video_status,
                    }
                ],
                "total_balance": video_balance,
            }
        ]
        if has_image:
            limits.append(
                {
                    "media_kind": "image",
                    "unit": "daily_quota",
                    "accounts": [
                        {
                            "id": rt.profile,
                            "label": acct_label,
                            "balance": None,
                            "total": None,
                            "reset_at": None,
                            "status": "auth_required" if video_status == "auth_required" else "ok",
                        }
                    ],
                    "total_balance": None,
                }
            )
        return {"limits": limits}

    def _require_idempotency_uuid(request: Request) -> str:
        raw_key = request.headers.get("Idempotency-Key", "").strip()
        if not raw_key:
            raise PluginHttpError(
                400,
                PluginError(
                    code="MISSING_IDEMPOTENCY_KEY",
                    kind="business",
                    retryable=False,
                    message="Thiếu header Idempotency-Key.",
                    scope="operation",
                ),
            )
        try:
            return str(uuid.UUID(raw_key))
        except ValueError as exc:
            raise PluginHttpError(
                400,
                PluginError(
                    code="INVALID_IDEMPOTENCY_KEY",
                    kind="business",
                    retryable=False,
                    message="Header Idempotency-Key phải là UUID hợp lệ.",
                    scope="operation",
                ),
            ) from exc

    @app.post("/v1/videos")
    async def post_videos(request: Request):
        rt = runtime
        op_uuid = _require_idempotency_uuid(request)
        raw_body = await request.body()
        validated = parse_and_validate_video_request(raw_body, rt.catalog, rt.config_snapshot)
        rt.register_prompt(validated.prompt)

        # Check existing operation first to support safe replay/409/409-cancelled. A retryable
        # pre-submit failure is about to be flipped back to reserved by reserve_operation(), so the
        # readiness gate must run BEFORE that flip: rejecting here must leave the row terminal.
        existing = rt.store.get_by_uuid(op_uuid)
        if existing is None or JobStore.is_retryable_pre_submit_failure(existing):
            _require_ready_for_new_job(rt, "video")

        snapshot = dict(validated.snapshot)
        snapshot["_prompt"] = validated.prompt

        res = rt.store.reserve_operation(
            operation_id=op_uuid,
            media_kind="video",
            fingerprint=validated.fingerprint,
            snapshot=snapshot,
        )
        op = res.operation

        if res.outcome in ("created", "retry_allowed"):
            temp_path = rt.store.write_temp_image(op_uuid, validated.image_bytes, validated.image_ext)
            rt.track_task(_run_video_operation(rt, op_uuid, temp_path))
            return JSONResponse(
                status_code=202,
                content={"id": op["task_id"], "task_id": op["task_id"], "status": "processing"},
            )

        if res.outcome in ("replay_in_progress", "replay_succeeded"):
            return JSONResponse(
                status_code=202,
                content={"id": op["task_id"], "task_id": op["task_id"], "status": "processing"},
            )

        # Terminal failed or reconcile_needed
        err_dict = op.get("error") or {
            "code": "OPERATION_TERMINAL",
            "kind": "business",
            "retryable": False,
            "message": "Thao tác đã kết thúc ở trạng thái lỗi.",
            "scope": "operation",
        }
        return JSONResponse(status_code=409 if op["status"] == "reconcile_needed" else 422, content={"status": "failed", "error": err_dict})

    @app.get("/v1/videos/{task_id}")
    async def get_video(task_id: str):
        rt = runtime
        rt.store.check_abandoned_timeouts()
        op = rt.store.get_by_task_id(task_id)
        if op is None or op.get("media_kind") != "video":
            raise PluginHttpError(
                404,
                PluginError(
                    code="TASK_NOT_FOUND",
                    kind="not_found",
                    retryable=False,
                    message="Không tìm thấy task video.",
                    scope="operation",
                ),
            )
        rec = _operation_to_record(op)
        status = rec["status"]
        out: dict[str, Any] = {"status": status}
        if status == "succeeded" and rec.get("media_name"):
            out["media_name"] = rec["media_name"]
        if status in ("failed", "reconcile_needed") and rec.get("error"):
            out["error"] = rec["error"]
        return out

    @app.delete("/v1/videos/{task_id}")
    async def delete_video(task_id: str):
        rt = runtime
        op = rt.store.cancel_by_task_id(task_id)
        if op is None or op.get("media_kind") != "video":
            raise PluginHttpError(
                404,
                PluginError(
                    code="TASK_NOT_FOUND",
                    kind="not_found",
                    retryable=False,
                    message="Không tìm thấy task video để hủy.",
                    scope="operation",
                ),
            )
        if op["status"] == "abandoned" and op.get("upstream_task_id"):
            try:
                await rt.mcp.cancel_task(op["upstream_task_id"])
            except Exception:
                pass
        msg_map = {
            "cancelled": "Đã hủy trước khi gửi lệnh lên upstream.",
            "abandoned": "Đã gửi lệnh lên upstream; đã ghi nhận dừng và có thể bị tính phí.",
            "succeeded": "Job đã hoàn tất, giữ nguyên kết quả.",
            "reconcile_needed": "Đã ghi nhận dừng trong lúc submit; giữ trạng thái để đối soát.",
        }
        return {"status": op["status"], "message": msg_map.get(op["status"], "Đã xử lý yêu cầu dừng.")}

    @app.get("/v1/media/{name}")
    async def get_media(name: str):
        rt = runtime
        if "/" in name or "\\" in name or ".." in name:
            raise PluginHttpError(
                404,
                PluginError(
                    code="MEDIA_NOT_FOUND",
                    kind="not_found",
                    retryable=False,
                    message="Tên media không hợp lệ.",
                    scope="operation",
                ),
            )
        op = rt.store.get_by_media_name(name)
        if op is None:
            raise PluginHttpError(
                404,
                PluginError(
                    code="MEDIA_NOT_FOUND",
                    kind="not_found",
                    retryable=False,
                    message="Không tìm thấy file media.",
                    scope="operation",
                ),
            )
        if op.get("media_purged") == 1:
            raise PluginHttpError(
                410,
                PluginError(
                    code="MEDIA_EXPIRED",
                    kind="not_found",
                    retryable=False,
                    message="File media đã hết thời hạn lưu cục bộ.",
                    scope="operation",
                    recoverable_media_id=op.get("upstream_media_id"),
                ),
            )

        rt.store.begin_media_download(name)
        completed = False
        try:
            confined = validate_confined_path(str(op["media_path"]), rt.output_dir)
            if not confined.is_file():
                raise PluginHttpError(
                    410,
                    PluginError(
                        code="MEDIA_EXPIRED",
                        kind="not_found",
                        retryable=False,
                        message="File media không còn trên đĩa.",
                        scope="operation",
                        recoverable_media_id=op.get("upstream_media_id"),
                    ),
                )
            data = confined.read_bytes()
            completed = True
            return Response(content=data, media_type="video/mp4")
        finally:
            rt.store.end_media_download(name, completed=completed)

    @app.post("/v1/images/edits")
    async def post_image_edits(request: Request):
        rt = runtime
        op_uuid = _require_idempotency_uuid(request)
        form = await request.form()
        form_fields: dict[str, Any] = {}
        image_bytes: bytes | None = None
        image_filename: str | None = None

        for k, v in form.multi_items():
            if k == "image" and hasattr(v, "read"):
                image_bytes = await v.read()
                image_filename = getattr(v, "filename", None)
            else:
                form_fields[k] = v

        validated = parse_and_validate_image_request(
            form_fields, image_bytes, image_filename, rt.catalog, rt.config_snapshot
        )
        rt.register_prompt(validated.prompt)

        # Readiness must precede reserve_operation(): for a retryable pre-submit failure that call
        # flips the row back to reserved, and a rejected request must leave it terminal.
        existing = rt.store.get_by_uuid(op_uuid)
        if existing is None or JobStore.is_retryable_pre_submit_failure(existing):
            _require_ready_for_new_job(rt, "image")

        snapshot = dict(validated.snapshot)
        snapshot["_prompt"] = validated.prompt

        res = rt.store.reserve_operation(
            operation_id=op_uuid,
            media_kind="image",
            fingerprint=validated.fingerprint,
            snapshot=snapshot,
        )
        op = res.operation

        if res.outcome == "replay_succeeded":
            return JSONResponse(status_code=200, content={"data": [{"b64_json": op["image_b64_json"]}]})

        if res.outcome == "replay_in_progress":
            return JSONResponse(status_code=202, content=_operation_to_record(op))

        if res.outcome in ("created", "retry_allowed"):
            temp_path = rt.store.write_temp_image(op_uuid, validated.image_bytes, validated.image_ext)
            task = rt.track_task(_run_image_operation(rt, op_uuid, temp_path))
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=rt.image_sync_wait_s)
            except asyncio.TimeoutError:
                cur = rt.store.get_by_uuid(op_uuid) or op
                return JSONResponse(status_code=202, content=_operation_to_record(cur))

            cur = rt.store.get_by_uuid(op_uuid) or op
            if cur["status"] == "succeeded":
                return JSONResponse(status_code=200, content={"data": [{"b64_json": cur["image_b64_json"]}]})
            if cur["status"] in ("processing", "reserved", "submitting"):
                return JSONResponse(status_code=202, content=_operation_to_record(cur))
            if cur["status"] in ("cancelled", "abandoned"):
                return JSONResponse(status_code=409, content=_operation_to_record(cur))
            err_dict = cur.get("error") or {}
            return JSONResponse(status_code=503, content={"status": "failed", "error": err_dict})

        err_dict = op.get("error") or {
            "code": "OPERATION_TERMINAL",
            "kind": "business",
            "retryable": False,
            "message": "Thao tác ảnh đã kết thúc ở trạng thái lỗi.",
            "scope": "operation",
        }
        return JSONResponse(status_code=409 if op["status"] == "reconcile_needed" else 422, content={"status": "failed", "error": err_dict})

    @app.get("/vf-plugin/v1/operations/{op_uuid}")
    async def get_operation(op_uuid: str):
        rt = runtime
        rt.store.check_abandoned_timeouts()
        op = rt.store.get_by_uuid(op_uuid)
        if op is None:
            raise PluginHttpError(
                404,
                PluginError(
                    code="OPERATION_NOT_FOUND",
                    kind="not_found",
                    retryable=False,
                    message="Chưa có bản ghi cho operation UUID này.",
                    scope="operation",
                ),
            )
        return _operation_to_record(op)

    @app.delete("/vf-plugin/v1/operations/{op_uuid}")
    async def delete_operation(op_uuid: str):
        rt = runtime
        op = rt.store.cancel_by_uuid(op_uuid)
        if op["status"] == "abandoned" and op.get("upstream_task_id"):
            try:
                await rt.mcp.cancel_task(op["upstream_task_id"])
            except Exception:
                pass
        return JSONResponse(status_code=200, content=_operation_to_record(op))

    @app.post("/vf-plugin/v1/actions/{action_id}")
    async def post_action(action_id: str, request: Request):
        rt = runtime
        try:
            body_raw = await request.body()
            payload = json.loads(body_raw.decode("utf-8")) if body_raw.strip() else {}
        except Exception as exc:
            raise PluginHttpError(
                400,
                PluginError(
                    code="INVALID_JSON",
                    kind="business",
                    retryable=False,
                    message="Payload action phải là JSON object.",
                    scope="operation",
                ),
            ) from exc
        if not isinstance(payload, dict):
            raise PluginHttpError(
                400,
                PluginError(
                    code="INVALID_JSON",
                    kind="business",
                    retryable=False,
                    message="Payload action phải là JSON object.",
                    scope="operation",
                ),
            )

        if action_id == "login":
            env = rt.mcp._build_stdio_env()
            cmd = os.environ.get("VF_GFLOW_LOGIN_CMD", "").strip()
            argv = cmd.split() if cmd else ["gflow", "auth", "login", "--browser", "chrome", "--profile", rt.profile]
            try:
                subprocess.Popen(argv, env=env)
            except Exception as exc:
                raise PluginHttpError(
                    503,
                    PluginError(
                        code="BROWSER_LAUNCH_FAILED",
                        kind="transient",
                        retryable=True,
                        message=sanitize_message(f"Không mở được trình duyệt đăng nhập: {exc}", secrets=rt.secrets),
                        scope="instance",
                    ),
                ) from exc
            return JSONResponse(status_code=202, content={"status": "started", "message": "Đã mở trình duyệt"})

        if action_id == "open_project":
            proj = str(rt.config_snapshot.get("project_id") or rt.store.get_meta("default_project_id") or "").strip()
            url = f"https://flow.google.com/project/{proj}" if proj else "https://flow.google.com"
            try:
                opened = webbrowser.open(url)
                if not opened:
                    raise RuntimeError("webbrowser.open returned False")
            except Exception as exc:
                raise PluginHttpError(
                    503,
                    PluginError(
                        code="BROWSER_LAUNCH_FAILED",
                        kind="transient",
                        retryable=True,
                        message=sanitize_message(f"Không mở được trình duyệt: {exc}", secrets=rt.secrets),
                        scope="instance",
                    ),
                ) from exc
            return JSONResponse(status_code=202, content={"status": "started", "message": "Đã mở trình duyệt"})

        if action_id == "recover_media":
            rec_id = str(payload.get("recoverable_media_id") or "").strip()
            if not rec_id:
                raise PluginHttpError(
                    400,
                    PluginError(
                        code="MISSING_RECOVERABLE_MEDIA_ID",
                        kind="business",
                        retryable=False,
                        message="Thiếu recoverable_media_id.",
                        scope="operation",
                    ),
                )
            op = rt.store.get_by_recoverable_media_id(rec_id)
            if op is None:
                raise PluginHttpError(
                    404,
                    PluginError(
                        code="RECOVERABLE_MEDIA_NOT_FOUND",
                        kind="not_found",
                        retryable=False,
                        message="Không tìm thấy operation gắn với recoverable_media_id này.",
                        scope="operation",
                    ),
                )
            op_id = op["operation_id"]
            rt.store.begin_recovery(op_id)
            try:
                dl = await rt.mcp.download_media(media_id=rec_id, out_dir=str(rt.output_dir), profile=rt.profile)
                if dl.get("_mcp_is_error") or dl.get("type"):
                    http_st, err, _ = translate_upstream_error(
                        dl,
                        media_kind="video",
                        stage="download",
                        secrets=rt.secrets,
                        prompts=rt.prompts_seen,
                    )
                    raise PluginHttpError(http_st, err)
                raw_path = dl.get("path") or dl.get("file") or str(rt.output_dir / f"{op_id}.mp4")
                confined = validate_confined_path(str(raw_path), rt.output_dir)
                if not validate_mp4_file(confined):
                    raise PluginHttpError(
                        503,
                        PluginError(
                            code="INVALID_MP4_FILE",
                            kind="business",
                            retryable=False,
                            message="File khôi phục không phải MP4 hợp lệ.",
                            scope="operation",
                            recoverable_media_id=rec_id,
                        ),
                    )
                updated = rt.store.mark_succeeded_video(op_id, media_path=confined, upstream_media_id=rec_id)
                return JSONResponse(
                    status_code=200,
                    content={
                        "status": "succeeded",
                        "message": "Đã khôi phục media thành công.",
                        "task_id": updated["task_id"],
                    },
                )
            finally:
                rt.store.end_recovery(op_id)

        raise PluginHttpError(
            404,
            PluginError(
                code="ACTION_NOT_FOUND",
                kind="not_found",
                retryable=False,
                message=f"Không tồn tại action '{action_id}'.",
                scope="operation",
            ),
        )

    return app


def build_runtime(
    *,
    data_dir: Path,
    config_file: Path,
    clock: Clock | None = None,
    mcp_client: GflowMcpClient | None = None,
    media_budget_bytes: int | None = None,
    abandoned_timeout_s: int | None = None,
    poll_interval_s: float = 0.25,
    image_sync_wait_s: float = 2.0,
) -> PluginRuntime:
    """Construct a PluginRuntime from data_dir, config_file, and environment secrets."""
    data_dir.mkdir(parents=True, exist_ok=True)
    gflow_home = data_dir / "gflow_home"
    output_dir = data_dir / "output"
    tmp_dir = data_dir / "tmp"
    gflow_home.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    config_snapshot = load_config_snapshot(config_file)
    token, proxy, daemon_token = load_secrets_from_env()
    secrets = tuple(s for s in (token, proxy, daemon_token) if s)

    redactor = RedactingLogFilter()
    for s in secrets:
        redactor.register_secret(s)
    logging.getLogger().addFilter(redactor)

    profile, bad_profile, bad_msg = resolve_profile_name(config_snapshot, gflow_home)
    store_kwargs: dict[str, Any] = {"clock": clock or SystemClock()}
    if media_budget_bytes is not None:
        store_kwargs["media_budget_bytes"] = media_budget_bytes
    if abandoned_timeout_s is not None:
        store_kwargs["abandoned_timeout_s"] = abandoned_timeout_s

    store = JobStore(data_dir / "jobs.db", output_dir, tmp_dir, **store_kwargs)

    identity_degraded = bad_profile
    identity_message = bad_msg
    bound_profile = store.get_meta("bound_profile")
    if not identity_degraded:
        if bound_profile and bound_profile != profile:
            identity_degraded = True
            identity_message = (
                f"Profile cấu hình ('{profile}') không khớp với profile đã gắn trên instance ('{bound_profile}')."
            )
        elif not bound_profile:
            store.set_meta("bound_profile", profile)

    daemon_url = str(os.environ.get("VF_GFLOW_DAEMON_URL") or config_snapshot.get("daemon_url") or "").strip()
    mcp = mcp_client or GflowMcpClient(
        gflow_home=gflow_home,
        output_dir=output_dir,
        proxy=proxy,
        daemon_url=daemon_url,
        daemon_token=daemon_token,
    )

    return PluginRuntime(
        data_dir=data_dir,
        gflow_home=gflow_home,
        output_dir=output_dir,
        tmp_dir=tmp_dir,
        config_snapshot=config_snapshot,
        token=token,
        proxy=proxy,
        daemon_token=daemon_token,
        profile=profile,
        store=store,
        mcp=mcp,
        catalog=ModelCatalogManager(),
        clock=clock or SystemClock(),
        redactor=redactor,
        secrets=secrets,
        identity_degraded=identity_degraded,
        identity_message=identity_message,
        poll_interval_s=poll_interval_s,
        image_sync_wait_s=image_sync_wait_s,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VideoFlow gflow-cli plugin bridge")
    parser.add_argument("--port", type=int, required=True, help="Loopback port to bind")
    parser.add_argument("--data-dir", type=Path, required=True, help="Instance data directory")
    parser.add_argument("--config-file", type=Path, required=True, help="Host config JSON file")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    rt = build_runtime(data_dir=args.data_dir, config_file=args.config_file)
    app = create_app(rt)
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
