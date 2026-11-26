"""MCP client for gflow-cli over stdio (`gflow mcp run`) and Streamable HTTP (`/mcp`)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import mcp.types as types
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import TypeAdapter

logger = logging.getLogger("videoflow_gflow.mcp")

MIN_UPSTREAM_VERSION = (0, 107, 0)
TESTED_UPSTREAM_VERSION = (0, 107, 0)

_DICT_ADAPTER: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")

# Centralized logical-to-upstream tool alias table.
TOOL_ALIAS_TABLE: dict[str, list[str]] = {
    "generate_video": ["gflow_generate_video", "generate_video"],
    "list_models": ["gflow_list_models", "list_models"],
    "get_credits": ["gflow_get_credits", "get_credits"],
    "auth_status": ["gflow_auth_status", "auth_status"],
    "generate_image": ["gflow_generate_image", "generate_image"],
    "download_media": ["gflow_download_media", "download_media"],
    "list_projects": ["gflow_list_projects", "list_projects"],
    "extend_video": ["gflow_extend_video", "extend_video"],
    "upscale_video": ["gflow_upscale_video", "upscale_video"],
    "character_list": ["gflow_character_list", "character_list"],
}

# Parameters the plugin knows how to supply for each logical tool.
SUPPORTED_TOOL_PARAMS: dict[str, dict[str, set[str]]] = {
    "generate_video": {
        "prompt": {"string"},
        "mode": {"string"},
        "aspect": {"string"},
        "initial_frame": {"string"},
        "end_frame": {"string"},
        "reference_images": {"array"},
        "model": {"string"},
        "duration": {"integer"},
        "resolution": {"string"},
        "count": {"integer"},
        "profile": {"string"},
        "project": {"string"},
        "project_name": {"string"},
        "ui_mode": {"string"},
        "out_file": {"string"},
        "max_credits": {"integer"},
        "wait": {"boolean"},
    },
    "generate_image": {
        "prompt": {"string"},
        "model": {"string"},
        "aspect": {"string"},
        "count": {"integer"},
        "reference_images": {"array"},
        "profile": {"string"},
        "project": {"string"},
        "project_name": {"string"},
        "ui_mode": {"string"},
        "out_file": {"string"},
        "wait": {"boolean"},
    },
    "list_models": {},
    "get_credits": {
        "profile": {"string"},
        "all_profiles": {"boolean"},
        "refresh": {"boolean"},
    },
    "auth_status": {
        "profile": {"string"},
    },
    "download_media": {
        "media_id": {"string"},
        "out_dir": {"string"},
        "profile": {"string"},
    },
    "list_projects": {
        "profile": {"string"},
        "limit": {"integer"},
        "offset": {"integer"},
    },
    "extend_video": {
        "source": {"string"},
        "prompt": {"string"},
        "duration": {"integer"},
        "reference_images": {"array"},
        "aspect": {"string"},
        "max_credits": {"integer"},
        "profile": {"string"},
        "project": {"string"},
        "project_name": {"string"},
        "out_file": {"string"},
        "wait": {"boolean"},
    },
    "upscale_video": {
        "media_id": {"string"},
        "scale": {"string"},
        "project": {"string"},
        "out_dir": {"string"},
        "profile": {"string"},
    },
    "character_list": {
        "project": {"string"},
        "profile": {"string"},
    },
}

REQUIRED_LOGICAL_TOOLS = ("generate_video", "list_models", "get_credits")


class UpstreamCompatibilityError(Exception):
    """Raised when gflow-cli version, tool schema, or Tasks capability is incompatible."""


class McpTransportError(Exception):
    """Raised when the MCP transport fails to reach or receive a valid reply from upstream."""


def parse_semver(version_str: str | None) -> tuple[int, int, int] | None:
    if not version_str:
        return None
    m = _SEMVER_RE.search(version_str)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _schema_types(prop_schema: dict[str, Any]) -> set[str]:
    t = prop_schema.get("type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {str(x) for x in t if x != "null"}
    any_of = prop_schema.get("anyOf") or prop_schema.get("oneOf") or []
    found: set[str] = set()
    for item in any_of:
        if isinstance(item, dict) and isinstance(item.get("type"), str) and item["type"] != "null":
            found.add(item["type"])
    return found


def check_tool_schema(logical_name: str, tool: types.Tool) -> tuple[bool, str | None]:
    """Verify that required parameters and their types are within plugin capabilities."""
    supported = SUPPORTED_TOOL_PARAMS.get(logical_name, {})
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    required = schema.get("required") or []
    properties = schema.get("properties") or {}

    for req_param in required:
        if req_param not in supported:
            return (
                False,
                f"Tool '{tool.name}' yêu cầu tham số bắt buộc '{req_param}' mà plugin chưa hỗ trợ. "
                f"Hãy cập nhật plugin hoặc kiểm tra phiên bản gflow-cli.",
            )
        prop_def = properties.get(req_param)
        if isinstance(prop_def, dict):
            declared = _schema_types(prop_def)
            allowed = supported[req_param]
            if declared and allowed and not (declared & allowed):
                return (
                    False,
                    f"Tham số '{req_param}' của tool '{tool.name}' có kiểu {sorted(declared)} "
                    f"không tương thích với kiểu plugin cung cấp {sorted(allowed)}.",
                )
    return True, None


def _extract_payload_from_call_result(result: types.CallToolResult) -> dict[str, Any]:
    """Extract structured JSON payload from MCP CallToolResult."""
    data: dict[str, Any] = {}
    if isinstance(result.structured_content, dict):
        data = dict(result.structured_content)
    elif result.content:
        for item in result.content:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text.strip():
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        data = parsed
                        break
                except json.JSONDecodeError:
                    data = {"detail": text}
    if result.is_error and "status" not in data and "type" not in data:
        data["status"] = "failed"
    if result.is_error:
        data["_mcp_is_error"] = True
    return data


@dataclass(slots=True)
class McpCapabilitiesReport:
    upstream_version: str | None = None
    tasks_supported: bool = False
    resolved_tools: dict[str, str] = field(default_factory=dict)
    missing_optional_tools: list[str] = field(default_factory=list)
    compatibility_warnings: list[str] = field(default_factory=list)


class GflowMcpClient:
    """Client for gflow-cli MCP server supporting stdio and Streamable HTTP transports."""

    def __init__(
        self,
        *,
        gflow_home: Path,
        output_dir: Path,
        proxy: str = "",
        daemon_url: str = "",
        daemon_token: str = "",
        extra_aliases: dict[str, list[str]] | None = None,
        stdio_command: list[str] | None = None,
        call_timeout_s: float = 120.0,
    ) -> None:
        self.gflow_home = gflow_home
        self.output_dir = output_dir
        self.proxy = proxy
        self.daemon_url = daemon_url.strip()
        self.daemon_token = daemon_token
        self.call_timeout_s = call_timeout_s
        self._extra_aliases = extra_aliases or {}
        self._stdio_command = stdio_command
        self._report = McpCapabilitiesReport()
        self._lock = asyncio.Lock()

    @property
    def report(self) -> McpCapabilitiesReport:
        return self._report

    def _build_stdio_env(self) -> dict[str, str]:
        """Build sanitized environment for gflow-cli subprocess."""
        env = dict(os.environ)
        # Remove host/plugin secrets that gflow mcp run does not need
        for key in ("VF_PLUGIN_TOKEN", "VF_PLUGIN_SECRETS_JSON", "GFLOW_DAEMON_TOKEN"):
            env.pop(key, None)
        env["GFLOW_CLI_HOME"] = str(self.gflow_home)
        env["GFLOW_CLI_OUTPUT_DIR"] = str(self.output_dir)
        if self.proxy:
            env["GFLOW_CLI_BROWSER_PROXY"] = self.proxy
        else:
            env.pop("GFLOW_CLI_BROWSER_PROXY", None)
        return env

    def _resolve_stdio_command(self) -> tuple[str, list[str]]:
        if self._stdio_command:
            return self._stdio_command[0], self._stdio_command[1:]
        override = os.environ.get("VF_GFLOW_MCP_CMD", "").strip()
        if override:
            parts = [p.strip('"') for p in shlex.split(override, posix=False)]
            return parts[0], parts[1:]
        return "gflow", ["mcp", "run"]

    @staticmethod
    def _find_compat_error(exc: BaseException) -> UpstreamCompatibilityError | None:
        if isinstance(exc, UpstreamCompatibilityError):
            return exc
        if isinstance(exc, BaseExceptionGroup):
            for sub in exc.exceptions:
                found = GflowMcpClient._find_compat_error(sub)
                if found is not None:
                    return found
        return None


    @asynccontextmanager
    async def _open_session(self) -> AsyncIterator[ClientSession]:
        try:
            if self.daemon_url:
                headers: dict[str, str] = {}
                if self.daemon_token:
                    headers["Authorization"] = f"Bearer {self.daemon_token}"
                async with httpx.AsyncClient(headers=headers, timeout=self.call_timeout_s) as http_client:
                    async with streamable_http_client(self.daemon_url, http_client=http_client) as (read, write):
                        async with ClientSession(read, write) as session:
                            await self._handshake_and_validate(session)
                            yield session
            else:
                cmd, args = self._resolve_stdio_command()
                params = StdioServerParameters(
                    command=cmd,
                    args=args,
                    env=self._build_stdio_env(),
                )
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await self._handshake_and_validate(session)
                        yield session
        except UpstreamCompatibilityError:
            raise
        except BaseExceptionGroup as exc:
            compat = self._find_compat_error(exc)
            if compat is not None:
                raise compat from exc
            raise McpTransportError(f"Không thể kết nối MCP server: {exc}") from exc
        except Exception as exc:
            raise McpTransportError(f"Không thể kết nối MCP server: {exc}") from exc

    async def _handshake_and_validate(self, session: ClientSession) -> None:
        init_res = await session.initialize()
        ver_str = init_res.server_info.version if init_res.server_info else None
        parsed_ver = parse_semver(ver_str)
        if parsed_ver is not None and parsed_ver < MIN_UPSTREAM_VERSION:
            raise UpstreamCompatibilityError(
                f"Phiên bản gflow-cli ({ver_str}) thấp hơn mức tối thiểu {'.'.join(map(str, MIN_UPSTREAM_VERSION))}."
            )

        warnings: list[str] = []
        if parsed_ver is not None and parsed_ver > TESTED_UPSTREAM_VERSION:
            warnings.append(
                f"gflow-cli ({ver_str}) mới hơn bản đã kiểm thử ({'.'.join(map(str, TESTED_UPSTREAM_VERSION))})."
            )

        caps = init_res.capabilities
        tasks_supported = False
        if caps is not None:
            if getattr(caps, "tasks", None) is not None:
                tasks_supported = True
            elif isinstance(caps.experimental, dict):
                if "tasks" in caps.experimental or "io.modelcontextprotocol/tasks" in caps.experimental:
                    tasks_supported = True

        tools_res = await session.list_tools()
        tools_by_name = {t.name: t for t in tools_res.tools}

        resolved: dict[str, str] = {}
        missing_optional: list[str] = []

        for logical_name, default_aliases in TOOL_ALIAS_TABLE.items():
            candidates = list(dict.fromkeys([*self._extra_aliases.get(logical_name, []), *default_aliases]))
            matched_tool: types.Tool | None = None
            for cand in candidates:
                if cand in tools_by_name:
                    matched_tool = tools_by_name[cand]
                    break

            if matched_tool is None:
                if logical_name in REQUIRED_LOGICAL_TOOLS:
                    raise UpstreamCompatibilityError(
                        f"MCP server thiếu tool bắt buộc '{logical_name}' (đã tìm: {', '.join(candidates)})."
                    )
                missing_optional.append(logical_name)
                continue

            ok, err_msg = check_tool_schema(logical_name, matched_tool)
            if not ok:
                if logical_name in REQUIRED_LOGICAL_TOOLS:
                    raise UpstreamCompatibilityError(err_msg or f"Tool '{matched_tool.name}' không tương thích.")
                warnings.append(err_msg or f"Tắt '{logical_name}' do schema không tương thích.")
                missing_optional.append(logical_name)
                continue

            resolved[logical_name] = matched_tool.name

        if not tasks_supported:
            raise UpstreamCompatibilityError("gflow-cli MCP server không hỗ trợ SEP-2663 Tasks cho luồng video async.")

        for w in warnings:
            logger.warning("Upstream compatibility notice: %s", w)

        self._report = McpCapabilitiesReport(
            upstream_version=ver_str,
            tasks_supported=tasks_supported,
            resolved_tools=resolved,
            missing_optional_tools=missing_optional,
            compatibility_warnings=warnings,
        )

    async def probe_compatibility(self) -> McpCapabilitiesReport:
        async with self._lock:
            async with self._open_session():
                return self._report

    async def _call_logical_tool(self, logical_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            async with self._open_session() as session:
                tool_name = self._report.resolved_tools.get(logical_name)
                if not tool_name:
                    raise UpstreamCompatibilityError(f"Tool '{logical_name}' không khả dụng trên MCP server.")
                clean_args = {k: v for k, v in arguments.items() if v is not None}
                try:
                    res = await asyncio.wait_for(
                        session.call_tool(tool_name, clean_args),
                        timeout=self.call_timeout_s,
                    )
                except Exception as exc:
                    raise McpTransportError(f"Lỗi khi gọi tool '{tool_name}': {exc}") from exc
                return _extract_payload_from_call_result(res)

    async def list_models(self) -> dict[str, Any]:
        return await self._call_logical_tool("list_models", {})

    async def get_credits(self, profile: str) -> dict[str, Any]:
        return await self._call_logical_tool("get_credits", {"profile": profile})

    async def auth_status(self, profile: str) -> dict[str, Any]:
        if "auth_status" not in self._report.resolved_tools:
            await self.probe_compatibility()
        if "auth_status" not in self._report.resolved_tools:
            return {"status": "unknown"}
        return await self._call_logical_tool("auth_status", {"profile": profile})

    async def list_projects(self, profile: str) -> dict[str, Any]:
        return await self._call_logical_tool("list_projects", {"profile": profile})

    async def generate_video(
        self,
        *,
        prompt: str,
        model: str,
        aspect: str,
        duration: int,
        initial_frame: str,
        profile: str,
        project: str | None = None,
        project_name: str | None = None,
        out_file: str | None = None,
        max_credits: int | None = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "prompt": prompt,
            "mode": "i2v",
            "model": model,
            "aspect": aspect,
            "duration": duration,
            "initial_frame": initial_frame,
            "profile": profile,
            "project": project or None,
            "project_name": project_name or None,
            "out_file": out_file,
            "max_credits": max_credits,
            "wait": wait,
        }
        return await self._call_logical_tool("generate_video", args)

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str,
        aspect: str,
        reference_images: list[str],
        profile: str,
        project: str | None = None,
        project_name: str | None = None,
        out_file: str | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "prompt": prompt,
            "model": model,
            "aspect": aspect,
            "count": 1,
            "reference_images": reference_images,
            "profile": profile,
            "project": project or None,
            "project_name": project_name or None,
            "out_file": out_file,
            "wait": wait,
        }
        return await self._call_logical_tool("generate_image", args)

    async def download_media(self, *, media_id: str, out_dir: str, profile: str) -> dict[str, Any]:
        return await self._call_logical_tool(
            "download_media",
            {"media_id": media_id, "out_dir": out_dir, "profile": profile},
        )

    async def get_task(self, upstream_task_id: str) -> dict[str, Any]:
        """Poll SEP-2663 `tasks/get` and return raw JSON dict including `result`."""
        async with self._lock:
            async with self._open_session() as session:
                req = types.GetTaskRequest(params=types.GetTaskRequestParams(task_id=upstream_task_id))
                try:
                    return await asyncio.wait_for(
                        session.send_request(req, _DICT_ADAPTER),
                        timeout=self.call_timeout_s,
                    )
                except Exception as exc:
                    raise McpTransportError(f"Lỗi khi gọi tasks/get cho '{upstream_task_id}': {exc}") from exc

    async def cancel_task(self, upstream_task_id: str) -> dict[str, Any]:
        """Request SEP-2663 `tasks/cancel` for an active upstream task."""
        async with self._lock:
            async with self._open_session() as session:
                req = types.CancelTaskRequest(params=types.CancelTaskRequestParams(task_id=upstream_task_id))
                try:
                    return await asyncio.wait_for(
                        session.send_request(req, _DICT_ADAPTER),
                        timeout=self.call_timeout_s,
                    )
                except Exception as exc:
                    raise McpTransportError(f"Lỗi khi gọi tasks/cancel cho '{upstream_task_id}': {exc}") from exc
