"""Runtime model catalog, alias resolution, and strict input validation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any

from gflow_errors import PluginError, PluginHttpError

_SIZE_RE = re.compile(r"^([1-9]\d*)x([1-9]\d*)$")
_DATA_URL_RE = re.compile(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.+)$", re.DOTALL)

DEFAULT_POLL_INTERVAL_S = 15
DEFAULT_POLL_MAX_S = 1800

DEFAULT_CATALOG_PAYLOAD: dict[str, Any] = {
    "video": {
        "modes": ["i2v"],
        "aspects": ["16:9", "9:16"],
        "models": [
            {
                "name": "veo_fast",
                "label": "Veo 3.1 Fast",
                "aliases": ["veo-fast", "veo_fast", "veo3.1-fast"],
                "durations": [4, 6, 8],
                "aspects": ["16:9", "9:16"],
                "retires_on": None,
                "cost_hint": "5 credits",
            },
            {
                "name": "veo_lite",
                "label": "Veo 3.1 Lite",
                "aliases": ["veo-lite", "veo_lite"],
                "durations": [4, 6, 8],
                "aspects": ["16:9", "9:16"],
                "retires_on": None,
                "cost_hint": "3 credits",
            },
            {
                "name": "veo_quality",
                "label": "Veo 3.1 Quality",
                "aliases": ["veo-quality", "veo_quality"],
                "durations": [4, 6, 8],
                "aspects": ["16:9", "9:16"],
                "retires_on": None,
                "cost_hint": "10 credits",
            },
            {
                "name": "omni_flash",
                "label": "Omni Flash",
                "aliases": ["omni-flash", "omni_flash"],
                "durations": [4, 6, 8, 10],
                "aspects": ["16:9", "9:16"],
                "retires_on": None,
                "cost_hint": "4 credits",
            },
            {
                "name": "omni_1_2_flash",
                "label": "Omni 1.2 Flash",
                "aliases": ["omni-1.2-flash", "omni_1_2_flash"],
                "durations": list(range(3, 16)),
                "aspects": ["16:9", "9:16"],
                "retires_on": None,
                "cost_hint": "4 credits",
            },
        ],
    },
    "image": {
        "modes": ["i2i"],
        "aspects": ["16:9", "4:3", "1:1", "3:4", "9:16"],
        "models": [
            {
                "name": "nano2",
                "label": "Nano Banana 2.1",
                "aliases": ["nano2", "nano2.1"],
                "aspects": ["16:9", "4:3", "1:1", "3:4", "9:16"],
                "default": True,
            },
            {
                "name": "nano2-lite",
                "label": "Nano Banana 2 Lite",
                "aliases": ["nano2-lite"],
                "aspects": ["16:9", "4:3", "1:1", "3:4", "9:16"],
            },
            {
                "name": "nano-pro",
                "label": "Nano Banana Pro",
                "aliases": ["nano-pro"],
                "aspects": ["16:9", "4:3", "1:1", "3:4", "9:16"],
            },
        ],
    },
}


@dataclass(slots=True)
class ModelEntry:
    id: str
    label: str
    aspects: list[str]
    durations: list[int] | None = None
    retires_on: str | None = None
    cost_hint: str | None = None
    aliases: tuple[str, ...] = ()
    upstream_name: str = ""


@dataclass(slots=True)
class ValidatedVideoRequest:
    prompt: str
    model: str
    upstream_model: str
    aspect: str
    size: str
    seconds: int
    image_bytes: bytes
    image_ext: str
    fingerprint: str
    snapshot: dict[str, Any]


@dataclass(slots=True)
class ValidatedImageRequest:
    prompt: str
    model: str
    upstream_model: str
    aspect_ratio: str
    image_bytes: bytes
    image_ext: str
    fingerprint: str
    snapshot: dict[str, Any]


def _normalize_model_entry(raw: dict[str, Any], default_aspects: list[str], *, is_video: bool) -> ModelEntry:
    upstream_name = str(raw.get("id") or raw.get("name") or "").strip()
    aliases_raw = [str(a).strip() for a in (raw.get("aliases") or []) if str(a).strip()]
    # Prefer hyphenated alias as public ID if available, else upstream_name
    public_id = upstream_name
    for alias in aliases_raw:
        if "-" in alias:
            public_id = alias
            break
    if not public_id:
        public_id = upstream_name
    all_aliases = tuple(dict.fromkeys([public_id, upstream_name, *aliases_raw]))
    aspects = [str(a) for a in (raw.get("aspects") or default_aspects)]
    durations = None
    if is_video:
        raw_durs = raw.get("durations")
        if isinstance(raw_durs, list) and raw_durs:
            durations = [int(d) for d in raw_durs]
        else:
            max_d = int(raw.get("max_duration") or 8)
            durations = [d for d in (4, 6, 8, 10) if d <= max_d] or [8]
    retires_on = raw.get("retires_on")
    cost_hint = raw.get("cost_hint")
    return ModelEntry(
        id=public_id,
        label=str(raw.get("label") or public_id),
        aspects=aspects,
        durations=durations,
        retires_on=str(retires_on) if retires_on else None,
        cost_hint=str(cost_hint) if cost_hint else None,
        aliases=all_aliases,
        upstream_name=upstream_name or public_id,
    )


class ModelCatalogManager:
    """Maintains runtime capabilities catalog with stale fallback cache."""

    def __init__(self) -> None:
        self._raw_catalog: dict[str, Any] | None = None
        self._stale: bool = False
        self._video_models: list[ModelEntry] = []
        self._image_models: list[ModelEntry] = []
        self._image_enabled: bool = False
        self._video_enabled: bool = True

    def has_cache(self) -> bool:
        return self._raw_catalog is not None

    def update_from_upstream(
        self,
        raw_catalog: dict[str, Any],
        *,
        video_enabled: bool = True,
        image_enabled: bool = False,
    ) -> None:
        self._raw_catalog = raw_catalog
        self._stale = False
        self._video_enabled = video_enabled
        self._image_enabled = image_enabled
        self._rebuild_entries()

    def mark_stale(self, *, video_enabled: bool | None = None, image_enabled: bool | None = None) -> None:
        if video_enabled is not None:
            self._video_enabled = video_enabled
        if image_enabled is not None:
            self._image_enabled = image_enabled
        self._stale = True

    def _rebuild_entries(self) -> None:
        raw = self._raw_catalog or DEFAULT_CATALOG_PAYLOAD
        v_sec = raw.get("video") if isinstance(raw.get("video"), dict) else {}
        v_aspects = [str(a) for a in (v_sec.get("aspects") or ["16:9", "9:16"])]
        self._video_models = [
            _normalize_model_entry(m, v_aspects, is_video=True)
            for m in (v_sec.get("models") or [])
            if isinstance(m, dict)
        ]

        i_sec = raw.get("image") if isinstance(raw.get("image"), dict) else {}
        i_aspects = [str(a) for a in (i_sec.get("aspects") or ["16:9", "4:3", "1:1", "3:4", "9:16"])]
        self._image_models = [
            _normalize_model_entry(m, i_aspects, is_video=False)
            for m in (i_sec.get("models") or [])
            if isinstance(m, dict)
        ]

    def resolve_video_model(self, requested: str) -> ModelEntry | None:
        key = requested.strip().lower()
        for entry in self._video_models:
            if key == entry.id.lower() or key in [a.lower() for a in entry.aliases]:
                return entry
        return None

    def resolve_image_model(self, requested: str) -> ModelEntry | None:
        key = requested.strip().lower()
        for entry in self._image_models:
            if key == entry.id.lower() or key in [a.lower() for a in entry.aliases]:
                return entry
        return None

    def build_capabilities_response(self) -> dict[str, Any]:
        if self._raw_catalog is None:
            raise PluginHttpError(
                503,
                PluginError(
                    code="CATALOG_UNAVAILABLE",
                    kind="transient",
                    retryable=True,
                    message="Chưa lấy được danh sách model từ gflow-cli.",
                    scope="instance",
                ),
            )
        kinds: list[str] = []
        payload: dict[str, Any] = {
            "plugin_api_version": "1",
            "kinds": kinds,
            "stale": self._stale,
        }
        if self._video_enabled:
            kinds.append("video")
            v_models_out = []
            for m in self._video_models:
                item: dict[str, Any] = {
                    "id": m.id,
                    "label": m.label,
                    "aspects": m.aspects,
                    "durations": m.durations or [8],
                }
                if m.retires_on:
                    item["retires_on"] = m.retires_on
                if m.cost_hint:
                    item["cost_hint"] = m.cost_hint
                v_models_out.append(item)
            payload["video"] = {
                "modes": ["i2v"],
                "models": v_models_out,
                "max_concurrent_jobs_per_account": 1,
                "poll_hint": {
                    "interval_s": DEFAULT_POLL_INTERVAL_S,
                    "max_s": DEFAULT_POLL_MAX_S,
                },
                "supports_cancel": "before_submit_only",
            }
        if self._image_enabled and self._image_models:
            kinds.append("image")
            i_models_out = []
            for m in self._image_models:
                item = {
                    "id": m.id,
                    "label": m.label,
                    "aspects": m.aspects,
                }
                if m.retires_on:
                    item["retires_on"] = m.retires_on
                if m.cost_hint:
                    item["cost_hint"] = m.cost_hint
                i_models_out.append(item)
            payload["image"] = {
                "modes": ["i2i"],
                "models": i_models_out,
            }
        return payload


def _bad_request(code: str, message: str) -> PluginHttpError:
    return PluginHttpError(
        400,
        PluginError(
            code=code,
            kind="business",
            retryable=False,
            message=message,
            scope="operation",
        ),
    )


def _unprocessable(code: str, message: str) -> PluginHttpError:
    return PluginHttpError(
        422,
        PluginError(
            code=code,
            kind="business",
            retryable=False,
            message=message,
            scope="operation",
        ),
    )


def decode_image_data_url(value: Any, field_name: str) -> tuple[bytes, str]:
    """Decode a base64 image data URL into (bytes, file_extension)."""
    if not isinstance(value, str) or not value.strip():
        raise _bad_request("INVALID_IMAGE_DATA_URL", f"Trường {field_name} phải là data URL ảnh hợp lệ.")
    m = _DATA_URL_RE.match(value.strip())
    if not m:
        raise _bad_request("INVALID_IMAGE_DATA_URL", f"Trường {field_name} không đúng định dạng data:image/...;base64,...")
    subtype = m.group(1).lower().split("+")[0]
    ext_map = {"jpeg": "jpg", "jpg": "jpg", "png": "png", "webp": "webp"}
    ext = ext_map.get(subtype, "png")
    b64_part = re.sub(r"\s+", "", m.group(2))
    try:
        raw = base64.b64decode(b64_part, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _bad_request("INVALID_IMAGE_DATA_URL", f"Dữ liệu base64 trong {field_name} bị hỏng.") from exc
    if not raw:
        raise _bad_request("INVALID_IMAGE_DATA_URL", f"Ảnh trong {field_name} rỗng.")
    return raw, ext


def parse_and_validate_video_request(
    raw_body: bytes,
    catalog: ModelCatalogManager,
    snapshot_config: dict[str, Any],
) -> ValidatedVideoRequest:
    """Strictly validate POST /v1/videos JSON payload without implicit type coercion."""
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        raise _bad_request("INVALID_JSON", "Payload JSON không hợp lệ.") from exc
    if not isinstance(data, dict):
        raise _bad_request("INVALID_JSON", "Body phải là một JSON object.")

    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _bad_request("INVALID_PROMPT", "Trường prompt phải là chuỗi không rỗng.")

    # Strict size: WxH positive integers only (reject "16:9", ints, etc.)
    size = data.get("size")
    if not isinstance(size, str):
        raise _bad_request("INVALID_SIZE", "Trường size phải là chuỗi dạng WxH, ví dụ 1280x720.")
    size_match = _SIZE_RE.match(size.strip())
    if not size_match:
        raise _bad_request("INVALID_SIZE", "Trường size phải có dạng <width>x<height> với số nguyên dương.")
    width = int(size_match.group(1))
    height = int(size_match.group(2))
    divisor = math.gcd(width, height)
    aspect = f"{width // divisor}:{height // divisor}"

    # Strict seconds: JSON integer > 0 (reject "8", bool, float)
    seconds = data.get("seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise _bad_request("INVALID_SECONDS", "Trường seconds phải là số nguyên JSON dương.")

    # Strict image & input_reference: at least one required, if both present bytes must match
    raw_img = data.get("image")
    raw_ref = data.get("input_reference")
    has_img = raw_img is not None and raw_img != ""
    has_ref = raw_ref is not None and raw_ref != ""
    if not has_img and not has_ref:
        raise _bad_request("MISSING_IMAGE", "Cần gửi ít nhất một trong image hoặc input_reference.")

    img_bytes: bytes | None = None
    img_ext = "png"
    if has_img:
        img_bytes, img_ext = decode_image_data_url(raw_img, "image")
    if has_ref:
        ref_bytes, ref_ext = decode_image_data_url(raw_ref, "input_reference")
        if img_bytes is not None:
            if img_bytes != ref_bytes:
                raise _bad_request("CONFLICTING_IMAGES", "Hai trường image và input_reference mang dữ liệu ảnh khác nhau.")
        else:
            img_bytes, img_ext = ref_bytes, ref_ext

    assert img_bytes is not None

    req_model = data.get("model")
    if req_model is not None and not isinstance(req_model, str):
        raise _bad_request("INVALID_MODEL", "Trường model phải là chuỗi.")
    effective_model_id = (req_model or snapshot_config.get("video_model") or "").strip()
    if not effective_model_id:
        raise _unprocessable("UNSUPPORTED_MODEL", "Chưa cấu hình model video.")

    model_entry = catalog.resolve_video_model(effective_model_id)
    if model_entry is None:
        raise _unprocessable("UNSUPPORTED_MODEL", f"Model video '{effective_model_id}' không có trong capabilities.")
    if aspect not in model_entry.aspects:
        raise _unprocessable("UNSUPPORTED_ASPECT", f"Model '{model_entry.id}' không hỗ trợ tỷ lệ {aspect}.")
    if model_entry.durations and seconds not in model_entry.durations:
        raise _unprocessable(
            "UNSUPPORTED_DURATION",
            f"Model '{model_entry.id}' không hỗ trợ thời lượng {seconds} giây.",
        )

    img_sha = hashlib.sha256(img_bytes).hexdigest()
    fp_payload = {
        "kind": "video",
        "prompt": prompt,
        "model": model_entry.id,
        "size": f"{width}x{height}",
        "aspect": aspect,
        "seconds": seconds,
        "image_sha256": img_sha,
    }
    fingerprint = hashlib.sha256(json.dumps(fp_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    snapshot = {
        "video_model": model_entry.id,
        "upstream_model": model_entry.upstream_name,
        "aspect": aspect,
        "size": f"{width}x{height}",
        "seconds": seconds,
        "project_id": snapshot_config.get("project_id", ""),
        "max_credits_per_clip": snapshot_config.get("max_credits_per_clip", 10),
    }

    return ValidatedVideoRequest(
        prompt=prompt,
        model=model_entry.id,
        upstream_model=model_entry.upstream_name,
        aspect=aspect,
        size=f"{width}x{height}",
        seconds=seconds,
        image_bytes=img_bytes,
        image_ext=img_ext,
        fingerprint=fingerprint,
        snapshot=snapshot,
    )


def parse_and_validate_image_request(
    form_fields: dict[str, Any],
    image_bytes: bytes | None,
    image_filename: str | None,
    catalog: ModelCatalogManager,
    snapshot_config: dict[str, Any],
) -> ValidatedImageRequest:
    """Strictly validate POST /v1/images/edits multipart form."""
    if "size" in form_fields:
        raise _bad_request("UNSUPPORTED_FIELD_SIZE", "Route ảnh không nhận trường size; hãy dùng aspect_ratio.")

    prompt = form_fields.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _bad_request("INVALID_PROMPT", "Trường prompt phải là chuỗi không rỗng.")

    if not image_bytes:
        raise _bad_request("MISSING_IMAGE", "Thiếu file ảnh đầu vào (trường image).")

    n_val = str(form_fields.get("n", "1")).strip()
    if n_val != "1":
        raise _bad_request("INVALID_N", "Hiện chỉ hỗ trợ n=1.")

    resp_fmt = str(form_fields.get("response_format", "b64_json")).strip()
    if resp_fmt != "b64_json":
        raise _bad_request("INVALID_RESPONSE_FORMAT", "Hiện chỉ hỗ trợ response_format=b64_json.")

    req_model = form_fields.get("model")
    if req_model is not None and not isinstance(req_model, str):
        raise _bad_request("INVALID_MODEL", "Trường model phải là chuỗi.")
    effective_model_id = (req_model or snapshot_config.get("image_model") or "nano2").strip()

    model_entry = catalog.resolve_image_model(effective_model_id)
    if model_entry is None:
        raise _unprocessable("UNSUPPORTED_MODEL", f"Model ảnh '{effective_model_id}' không có trong capabilities.")

    raw_aspect = form_fields.get("aspect_ratio")
    if raw_aspect is not None and not isinstance(raw_aspect, str):
        raise _bad_request("INVALID_ASPECT_RATIO", "Trường aspect_ratio phải là chuỗi.")
    effective_aspect = (raw_aspect or "").strip()
    if not effective_aspect:
        effective_aspect = "1:1" if "1:1" in model_entry.aspects else model_entry.aspects[0]
    elif effective_aspect not in model_entry.aspects:
        raise _unprocessable(
            "UNSUPPORTED_ASPECT",
            f"Model '{model_entry.id}' không hỗ trợ aspect_ratio '{effective_aspect}'.",
        )

    ext = "png"
    if image_filename and "." in image_filename:
        cand = image_filename.rsplit(".", 1)[-1].lower()
        if cand in ("png", "jpg", "jpeg", "webp"):
            ext = "jpg" if cand == "jpeg" else cand

    img_sha = hashlib.sha256(image_bytes).hexdigest()
    fp_payload = {
        "kind": "image",
        "prompt": prompt,
        "model": model_entry.id,
        "n": 1,
        "response_format": "b64_json",
        "aspect_ratio": effective_aspect,
        "image_sha256": img_sha,
    }
    fingerprint = hashlib.sha256(json.dumps(fp_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    snapshot = {
        "image_model": model_entry.id,
        "upstream_model": model_entry.upstream_name,
        "aspect_ratio": effective_aspect,
        "project_id": snapshot_config.get("project_id", ""),
    }

    return ValidatedImageRequest(
        prompt=prompt,
        model=model_entry.id,
        upstream_model=model_entry.upstream_name,
        aspect_ratio=effective_aspect,
        image_bytes=image_bytes,
        image_ext=ext,
        fingerprint=fingerprint,
        snapshot=snapshot,
    )
