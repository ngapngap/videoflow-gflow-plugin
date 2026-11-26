"""Error translation, HTTP status mapping, and secret/path redaction for VideoFlow gflow plugin."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Literal

ErrorKind = Literal["transient", "business", "quota", "auth", "not_found", "unsupported"]
ErrorScope = Literal["operation", "media_kind", "instance"]

DEFAULT_WAF_COOLDOWN_S = 1800
DEFAULT_PROFILE_LOCK_RETRY_S = 15

_URL_CREDS_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+/=]+")
_WIN_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s\"'<>|\r\n]+")
_POSIX_PATH_RE = re.compile(r"(?<![A-Za-z0-9_:])/(?:Users|home|tmp|var|etc|opt|mnt)/[^\s\"'<>|\r\n]+")


def sanitize_message(
    text: str | None,
    *,
    secrets: Iterable[str] = (),
    prompts: Iterable[str] = (),
) -> str:
    """Strip secrets, credentials, prompts, and machine file paths from a message."""
    if not text:
        return ""
    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= 3:
            out = out.replace(secret, "[REDACTED]")
    for prompt in prompts:
        if prompt and len(prompt) >= 4:
            out = out.replace(prompt, "[REDACTED_PROMPT]")
    out = _URL_CREDS_RE.sub(r"\1[REDACTED]@", out)
    out = _BEARER_RE.sub("Bearer [REDACTED]", out)
    out = _WIN_PATH_RE.sub("[REDACTED_PATH]", out)
    out = _POSIX_PATH_RE.sub("[REDACTED_PATH]", out)
    return out


class RedactingLogFilter(logging.Filter):
    """Logging filter that redacts registered secrets and prompts from log records."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()
        self._prompts: set[str] = set()

    def register_secret(self, value: str | None) -> None:
        if value and len(value) >= 3:
            self._secrets.add(value)

    def register_prompt(self, value: str | None) -> None:
        if value and len(value) >= 4:
            self._prompts.add(value)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            cleaned = sanitize_message(rendered, secrets=self._secrets, prompts=self._prompts)
            if cleaned != rendered:
                record.msg = cleaned
                record.args = ()
        except Exception:
            pass
        return True


@dataclass(slots=True)
class PluginError:
    code: str
    kind: ErrorKind
    retryable: bool
    message: str
    scope: ErrorScope
    retry_after_s: int | None = None
    recoverable_media_id: str | None = None

    def to_dict(
        self,
        *,
        secrets: Iterable[str] = (),
        prompts: Iterable[str] = (),
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "kind": self.kind,
            "retryable": bool(self.retryable),
            "message": sanitize_message(self.message, secrets=secrets, prompts=prompts) or self.code,
            "scope": self.scope,
        }
        if self.retry_after_s is not None:
            payload["retry_after_s"] = int(self.retry_after_s)
        if self.recoverable_media_id:
            payload["recoverable_media_id"] = str(self.recoverable_media_id)
        return payload

    def to_envelope(
        self,
        *,
        secrets: Iterable[str] = (),
        prompts: Iterable[str] = (),
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "error": self.to_dict(secrets=secrets, prompts=prompts),
        }


class PluginHttpError(Exception):
    """HTTP-level error mapped to Contract v1 status code and error body."""

    def __init__(self, http_status: int, error: PluginError) -> None:
        super().__init__(error.message)
        self.http_status = http_status
        self.error = error


def _extract_error_slug(problem_type: str | None) -> str:
    if not problem_type:
        return ""
    return problem_type.rstrip("/").rsplit("/", 1)[-1].lower()


def _safe_int_seconds(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = int(round(float(val)))
        return max(1, num)
    except (TypeError, ValueError):
        return None


def translate_upstream_error(
    envelope: dict[str, Any] | None,
    *,
    media_kind: str = "video",
    stage: Literal["pre_submit", "post_submit", "poll", "download"] = "pre_submit",
    secrets: Iterable[str] = (),
    prompts: Iterable[str] = (),
) -> tuple[int, PluginError, bool]:
    """Translate a gflow-cli RFC 9457 error envelope into (http_status, PluginError, needs_reconcile).

    Prioritizes ``type``, ``charged``, ``retryable``, ``next_action``, and
    ``operation_id`` / ``media_id`` in the envelope.
    """
    env = envelope if isinstance(envelope, dict) else {}
    # Some MCP responses nest ProblemDetails under "error"
    if isinstance(env.get("error"), dict) and "type" not in env:
        env = env["error"]

    slug = _extract_error_slug(env.get("type"))
    charged = str(env.get("charged") or ("maybe" if stage == "post_submit" else "no")).lower()
    raw_retryable = env.get("retryable")
    next_action = str(env.get("next_action") or "").lower()
    detail = sanitize_message(
        str(env.get("detail") or env.get("title") or env.get("message") or ""),
        secrets=secrets,
        prompts=prompts,
    )
    retry_after = _safe_int_seconds(env.get("retry_after") or env.get("retry_after_s"))
    media_id = env.get("media_id") or env.get("operation_id")
    if not media_id and isinstance(env.get("completed_media_ids"), list) and env["completed_media_ids"]:
        media_id = env["completed_media_ids"][-1]
    recoverable_id = str(media_id) if media_id else None

    # 1. WAF / anti-bot rejection
    if slug == "waf-rejection":
        return (
            503,
            PluginError(
                code="PROVIDER_WAF_COOLDOWN",
                kind="transient",
                retryable=False,
                message=detail or "Google Flow tạm chặn (WAF). Tạm dừng nhận việc trên instance này.",
                scope="instance",
                retry_after_s=retry_after or DEFAULT_WAF_COOLDOWN_S,
            ),
            charged != "no",
        )

    # 2. Credit / daily quota exhausted
    if slug == "insufficient-credits":
        msg = detail or "Tài khoản không đủ credits để tạo media."
        return (
            429,
            PluginError(
                code="INSUFFICIENT_CREDITS",
                kind="quota",
                retryable=False,
                message=msg,
                scope="media_kind",
                retry_after_s=retry_after,
            ),
            False,
        )

    # 3. Credit cap exceeded (pre-submit quote > max_credits)
    if slug == "credit-cap-exceeded":
        quoted = env.get("quoted")
        cap = env.get("max_credits")
        cap_info = ""
        if quoted is not None and cap is not None:
            cap_info = f" (giá dự kiến {quoted} credits, cap hiện tại {cap})"
        elif cap is not None:
            cap_info = f" (cap hiện tại {cap})"
        return (
            422,
            PluginError(
                code="CREDIT_CAP_EXCEEDED",
                kind="business",
                retryable=False,
                message=detail or f"Chi phí clip vượt quá giới hạn max_credits_per_clip{cap_info}.",
                scope="operation",
            ),
            False,
        )

    # 4. Rate limit
    if slug == "rate-limit":
        can_retry = charged == "no" and bool(raw_retryable) and next_action in ("retry", "retry_after", "")
        return (
            429,
            PluginError(
                code="PROVIDER_RATE_LIMITED",
                kind="transient",
                retryable=can_retry,
                message=detail or "Google Flow đang giới hạn tần suất yêu cầu.",
                scope="instance",
                retry_after_s=retry_after or 60,
            ),
            charged != "no",
        )

    # 5. Auth / session / account access issues
    if slug in (
        "auth-expired",
        "auth-missing",
        "aisandbox-auth",
        "flow-account-chooser",
        "auth-browser-rejected",
        "auth-login-timeout",
    ):
        return (
            424,
            PluginError(
                code="UPSTREAM_AUTH_REQUIRED",
                kind="auth",
                retryable=False,
                message=detail or "Phiên đăng nhập Google Flow đã hết hạn. Vui lòng chạy action Đăng nhập Flow.",
                scope="instance",
            ),
            charged != "no" and stage == "post_submit",
        )

    if slug == "flow-access-unavailable":
        return (
            424,
            PluginError(
                code="FLOW_ACCESS_UNAVAILABLE",
                kind="auth",
                retryable=False,
                message=detail or "Tài khoản Google này chưa có quyền truy cập Google Flow.",
                scope="instance",
            ),
            False,
        )

    # 6. Profile lock / access
    if slug == "profile-locked":
        if stage == "pre_submit" and charged == "no":
            return (
                503,
                PluginError(
                    code="PROFILE_BUSY",
                    kind="transient",
                    retryable=True,
                    message=detail or "Profile Chrome đang bận bởi tiến trình khác, vui lòng thử lại sau.",
                    scope="instance",
                    retry_after_s=retry_after or DEFAULT_PROFILE_LOCK_RETRY_S,
                ),
                False,
            )
        return (
            424,
            PluginError(
                code="PROFILE_LOCKED",
                kind="auth",
                retryable=False,
                message=detail or "Profile Chrome đang bị giữ hoặc cần xác minh lại.",
                scope="instance",
            ),
            charged != "no",
        )

    if slug in ("profile-access", "profile-engine-downgrade"):
        return (
            424,
            PluginError(
                code="PROFILE_ACCESS_ERROR",
                kind="auth",
                retryable=False,
                message=detail or "Không thể truy cập thư mục profile Chrome. Kiểm tra quyền hoặc đăng nhập lại.",
                scope="instance",
            ),
            False,
        )

    # 7. Media download failure after generation was billed
    if slug == "media-download":
        return (
            503,
            PluginError(
                code="MEDIA_DOWNLOAD_FAILED",
                kind="transient",
                retryable=False,
                message=detail or "Tải media đã tạo thất bại. Dùng chức năng khôi phục media để tải lại.",
                scope="operation",
                recoverable_media_id=recoverable_id,
            ),
            True,
        )

    # 8. Content policy
    if slug == "content-policy":
        return (
            422,
            PluginError(
                code="CONTENT_POLICY_REJECTED",
                kind="business",
                retryable=False,
                message=detail or "Yêu cầu bị từ chối bởi chính sách nội dung của Google Flow.",
                scope="operation",
            ),
            False,
        )

    # 9. Transient network / timeout / browser closed
    if slug in ("transport-timeout", "network", "browser-session-closed", "flow-app", "recaptcha-mint"):
        safe_to_retry = charged == "no" and stage == "pre_submit" and (raw_retryable is not False)
        needs_reconcile = not safe_to_retry and (charged != "no" or stage in ("post_submit", "poll", "download") or next_action == "reconcile")
        code_map = {
            "transport-timeout": "UPSTREAM_TIMEOUT",
            "network": "UPSTREAM_NETWORK_ERROR",
            "browser-session-closed": "BROWSER_SESSION_CLOSED",
            "flow-app": "FLOW_APP_ERROR",
            "recaptcha-mint": "RECAPTCHA_MINT_FAILED",
        }
        return (
            503,
            PluginError(
                code=code_map.get(slug, "UPSTREAM_TRANSIENT_ERROR"),
                kind="transient",
                retryable=safe_to_retry,
                message=detail or "Lỗi kết nối hoặc trình duyệt khi gọi Google Flow.",
                scope="operation",
                retry_after_s=retry_after,
                recoverable_media_id=recoverable_id,
            ),
            needs_reconcile,
        )

    # 10. Unsupported / selector drift / host migrated / incompatible model
    if slug in (
        "ui-selector-drift",
        "flow-host-migrated",
        "upscale-unavailable",
        "extend-unavailable",
        "model-mode-incompatibility",
        "ui-mode-unavailable",
    ):
        needs_reconcile = charged != "no" or stage == "post_submit" or next_action == "reconcile"
        return (
            501 if slug in ("upscale-unavailable", "extend-unavailable", "flow-host-migrated") else 422,
            PluginError(
                code=slug.upper().replace("-", "_"),
                kind="unsupported",
                retryable=False,
                message=detail or "Tính năng hoặc giao diện upstream chưa được hỗ trợ.",
                scope="operation",
                recoverable_media_id=recoverable_id,
            ),
            needs_reconcile,
        )

    if slug in ("wire-format", "configuration", "video-model-selection", "reference-not-found", "media-upload-rejected"):
        needs_reconcile = charged != "no" or stage == "post_submit"
        return (
            422,
            PluginError(
                code=slug.upper().replace("-", "_"),
                kind="business",
                retryable=False,
                message=detail or "Tham số hoặc định dạng không hợp lệ với Google Flow.",
                scope="operation",
                recoverable_media_id=recoverable_id,
            ),
            needs_reconcile,
        )

    # 11. Unknown error or single exit code where stage is ambiguous
    ambiguous_or_charged = charged != "no" or stage in ("post_submit", "poll", "download") or next_action == "reconcile"
    if ambiguous_or_charged:
        return (
            503,
            PluginError(
                code="UPSTREAM_RECONCILE_NEEDED",
                kind="business",
                retryable=False,
                message=detail or "Không xác định được kết quả từ upstream; giữ trạng thái để đối soát.",
                scope="operation",
                recoverable_media_id=recoverable_id,
            ),
            True,
        )

    return (
        503,
        PluginError(
            code="UPSTREAM_UNKNOWN_ERROR",
            kind="business",
            retryable=False,
            message=detail or "Lỗi không xác định từ upstream gflow-cli.",
            scope="operation",
        ),
        False,
    )
