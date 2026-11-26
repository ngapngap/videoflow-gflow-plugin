"""Configurable mock gflow-cli MCP server (stdio & Streamable HTTP) for pytest."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import mcp.server.stdio as mcp_stdio
import mcp.types as types
import uvicorn
from mcp.server.lowlevel import NotificationOptions, Server
from pydantic import ConfigDict
from starlette.responses import JSONResponse


class DynamicResult(types.Result):
    model_config = ConfigDict(extra="allow")


def make_valid_mp4_bytes() -> bytes:
    """Create a minimal structurally valid ISO BMFF MP4 (ftyp + moov + mdat)."""
    ftyp = b"\x00\x00\x00\x14ftypisom\x00\x00\x02\x00isom"
    moov = b"\x00\x00\x00\x08moov"
    mdat = b"\x00\x00\x00\x10mdat\x00\x00\x00\x00\x00\x00\x00\x00"
    return ftyp + moov + mdat


def _load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.is_file():
        return {}
    return json.loads(state_file.read_text(encoding="utf-8"))


def _save_state(state_file: Path, data: dict[str, Any]) -> None:
    state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")


def build_mock_server(state_file: Path) -> Server:
    state = _load_state(state_file)
    version = str(state.get("version") or "0.107.0")

    async def handle_list_tools(_ctx: Any, _params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        st = _load_state(state_file)
        renames = st.get("tool_renames") or {}
        hidden = set(st.get("hidden_tools") or [])
        extra_required = st.get("extra_required_params") or {}

        base_tools: list[tuple[str, dict[str, Any]]] = [
            (
                "gflow_generate_video",
                {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "mode": {"type": "string"},
                        "aspect": {"type": "string"},
                        "initial_frame": {"type": "string"},
                        "model": {"type": "string"},
                        "duration": {"type": "integer"},
                        "profile": {"type": "string"},
                        "project": {"type": "string"},
                        "out_file": {"type": "string"},
                        "max_credits": {"type": "integer"},
                        "wait": {"type": "boolean"},
                    },
                    "required": ["prompt"],
                },
            ),
            ("gflow_list_models", {"type": "object", "properties": {}}),
            (
                "gflow_get_credits",
                {"type": "object", "properties": {"profile": {"type": "string"}}},
            ),
            (
                "gflow_auth_status",
                {"type": "object", "properties": {"profile": {"type": "string"}}},
            ),
            (
                "gflow_generate_image",
                {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "model": {"type": "string"},
                        "aspect": {"type": "string"},
                        "count": {"type": "integer"},
                        "reference_images": {"type": "array"},
                        "profile": {"type": "string"},
                        "project": {"type": "string"},
                        "out_file": {"type": "string"},
                        "wait": {"type": "boolean"},
                    },
                    "required": ["prompt"],
                },
            ),
            (
                "gflow_download_media",
                {
                    "type": "object",
                    "properties": {
                        "media_id": {"type": "string"},
                        "out_dir": {"type": "string"},
                        "profile": {"type": "string"},
                    },
                    "required": ["media_id", "out_dir"],
                },
            ),
        ]

        out: list[types.Tool] = []
        for orig_name, schema in base_tools:
            if orig_name in hidden:
                continue
            name = renames.get(orig_name, orig_name)
            sch = json.loads(json.dumps(schema))
            if orig_name in extra_required:
                for p in extra_required[orig_name]:
                    sch["properties"][p] = {"type": "string"}
                    sch.setdefault("required", []).append(p)
            out.append(types.Tool(name=name, description=f"Mock {name}", input_schema=sch))
        return types.ListToolsResult(tools=out)

    async def handle_call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        name = params.name
        args = params.arguments or {}
        st = _load_state(state_file)
        renames = st.get("tool_renames") or {}
        rev = {v: k for k, v in renames.items()}
        logical = rev.get(name, name)

        if logical == "gflow_list_models":
            catalog = st.get("catalog") or {
                "video": {
                    "modes": ["i2v"],
                    "aspects": ["16:9", "9:16"],
                    "models": [
                        {
                            "name": "veo_fast",
                            "label": "Veo 3.1 Fast",
                            "aliases": ["veo-fast", "veo_fast"],
                            "durations": [4, 6, 8],
                            "aspects": ["16:9", "9:16"],
                        }
                    ],
                },
                "image": {
                    "modes": ["i2i"],
                    "aspects": ["1:1", "16:9", "9:16"],
                    "models": [
                        {
                            "name": "nano2",
                            "label": "Nano Banana 2.1",
                            "aliases": ["nano2"],
                            "aspects": ["1:1", "16:9", "9:16"],
                        }
                    ],
                },
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(catalog))],
                structured_content=catalog,
                is_error=False,
            )

        if logical == "gflow_get_credits":
            creds = st.get("credits_response") or {"credits": 100, "total": 100, "label": "Test Account"}
            is_err = bool(creds.get("type"))
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(creds))],
                structured_content=creds,
                is_error=is_err,
            )

        if logical == "gflow_auth_status":
            auth = st.get("auth_response") or {
                "status": "authenticated",
                "profile": args.get("profile", "default"),
                "user_email": "test@example.com",
            }
            is_err = auth.get("status") != "authenticated"
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(auth))],
                structured_content=auth,
                is_error=is_err,
            )

        if logical == "gflow_generate_video":
            st["generate_video_calls"] = int(st.get("generate_video_calls") or 0) + 1
            st.setdefault("video_call_args", []).append(args)
            if st.get("generate_video_error"):
                err = st["generate_video_error"]
                _save_state(state_file, st)
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=json.dumps(err))],
                    structured_content=err,
                    is_error=True,
                )

            task_id = f"up-vid-{uuid.uuid4().hex[:8]}"
            tasks = st.setdefault("tasks", {})
            tasks[task_id] = {
                "polls": 0,
                "out_file": args.get("out_file"),
                "media_id": f"media-{uuid.uuid4().hex[:8]}",
                "project_id": args.get("project") or "proj-mock-1",
            }
            _save_state(state_file, st)
            res = {"status": "pending", "task_id": task_id, "project_id": tasks[task_id]["project_id"]}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(res))],
                structured_content=res,
                is_error=False,
            )

        if logical == "gflow_generate_image":
            st["generate_image_calls"] = int(st.get("generate_image_calls") or 0) + 1
            delay = float(st.get("generate_image_delay_s") or 0)
            _save_state(state_file, st)
            if delay > 0:
                await asyncio.sleep(delay)
            st = _load_state(state_file)
            if st.get("generate_image_error"):
                err = st["generate_image_error"]
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=json.dumps(err))],
                    structured_content=err,
                    is_error=True,
                )
            b64 = st.get("image_b64") or "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNgAAAAAgABSK+kcQAAAABJRU5ErkJggg=="
            res = {
                "status": "completed",
                "b64_json": b64,
                "media_id": f"img-{uuid.uuid4().hex[:8]}",
                "project_id": args.get("project") or "proj-mock-1",
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(res))],
                structured_content=res,
                is_error=False,
            )

        if logical == "gflow_download_media":
            st["download_media_calls"] = int(st.get("download_media_calls") or 0) + 1
            _save_state(state_file, st)
            if st.get("download_media_error"):
                err = st["download_media_error"]
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=json.dumps(err))],
                    structured_content=err,
                    is_error=True,
                )
            out_dir = Path(args["out_dir"])
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"recovered-{args['media_id']}.mp4"
            out_path.write_bytes(make_valid_mp4_bytes())
            res = {"path": str(out_path), "bytes": out_path.stat().st_size, "media_id": args["media_id"]}
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(res))],
                structured_content=res,
                is_error=False,
            )

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps({"detail": f"Unknown tool {name}"}))],
            isError=True,
        )

    async def handle_get_task(_ctx: Any, params: types.GetTaskRequestParams) -> types.Result:
        st = _load_state(state_file)
        task_id = params.task_id
        tasks = st.setdefault("tasks", {})
        t = tasks.get(task_id)
        if t is None:
            return DynamicResult(status="failed", taskId=task_id, statusMessage="Task not found")
        t["polls"] = int(t.get("polls") or 0) + 1
        polls_before_done = int(st.get("polls_before_done") or 1)
        _save_state(state_file, st)

        if t.get("cancelled"):
            return DynamicResult(status="cancelled", taskId=task_id)

        if t["polls"] < polls_before_done:
            return DynamicResult(status="working", taskId=task_id)

        if st.get("task_poll_error"):
            err = st["task_poll_error"]
            return DynamicResult(
                status="completed",
                taskId=task_id,
                result={"isError": True, "structuredContent": err},
            )

        if st.get("outside_output_path"):
            bad_path = str(st["outside_output_path"])
            Path(bad_path).parent.mkdir(parents=True, exist_ok=True)
            Path(bad_path).write_bytes(make_valid_mp4_bytes())
            return DynamicResult(
                status="completed",
                taskId=task_id,
                result={
                    "isError": False,
                    "structuredContent": {
                        "files": [bad_path],
                        "media_id": t["media_id"],
                        "project_id": t["project_id"],
                    },
                },
            )

        out_path = Path(t["out_file"] or (Path(os.environ.get("GFLOW_CLI_OUTPUT_DIR", ".")) / f"{task_id}.mp4"))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if st.get("corrupt_mp4"):
            out_path.write_bytes(b"NOT_AN_MP4_FILE_CORRUPTED_BYTES")
        else:
            out_path.write_bytes(make_valid_mp4_bytes())

        return DynamicResult(
            status="completed",
            taskId=task_id,
            result={
                "isError": False,
                "structuredContent": {
                    "files": [str(out_path)],
                    "media_id": t["media_id"],
                    "project_id": t["project_id"],
                },
            },
        )

    async def handle_cancel_task(_ctx: Any, params: types.CancelTaskRequestParams) -> types.EmptyResult:
        st = _load_state(state_file)
        task_id = params.task_id
        tasks = st.setdefault("tasks", {})
        if task_id in tasks:
            tasks[task_id]["cancelled"] = True
            _save_state(state_file, st)
        return types.EmptyResult()

    server = Server(
        "gflow-cli-mock",
        version=version,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )

    orig_get_caps = server.get_capabilities

    def _get_caps(*args: Any, **kwargs: Any) -> types.ServerCapabilities:
        caps = orig_get_caps(*args, **kwargs)
        if state.get("tasks_supported", True):
            caps.tasks = types.ServerTasksCapability()
            if caps.experimental is None:
                caps.experimental = {}
            caps.experimental["io.modelcontextprotocol/tasks"] = {"supported": True}
        return caps

    server.get_capabilities = _get_caps  # type: ignore[method-assign]
    server.add_request_handler("tasks/get", types.GetTaskRequestParams, handle_get_task)
    server.add_request_handler("tasks/cancel", types.CancelTaskRequestParams, handle_cancel_task)
    return server


async def _run_stdio(state_file: Path) -> None:
    server = build_mock_server(state_file)
    async with mcp_stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def _run_http(state_file: Path, port: int, expected_token: str) -> None:
    server = build_mock_server(state_file)
    inner_app = server.streamable_http_app()

    async def auth_wrapper(scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and expected_token:
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            if headers.get("authorization") != f"Bearer {expected_token}":
                resp = JSONResponse(status_code=401, content={"error": "unauthorized"})
                await resp(scope, receive, send)
                return
        await inner_app(scope, receive, send)

    uvicorn.run(auth_wrapper, host="127.0.0.1", port=port, log_level="warning")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--state-file", type=Path, default=None)
    parser.add_argument("--port", type=int, default=18799)
    parser.add_argument("--token", type=str, default="")
    args = parser.parse_args()

    state_file = args.state_file or Path(os.environ["VF_MOCK_MCP_STATE"])
    if args.transport == "stdio":
        asyncio.run(_run_stdio(state_file))
    else:
        _run_http(state_file, args.port, args.token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
