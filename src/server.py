"""
MCP Server – sandboxed Python code execution via AWS Lambda.

Observability
-------------
  Structured JSON logging to stderr (visible in Claude Code logs)
  Correlation IDs generated per request and forwarded to Lambda
  Invocation latency measured and logged

Tool exposed:
  execute_python – run Python code in an AWS Lambda sandbox
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

# ---------------------------------------------------------------------------
# Structured JSON logger (writes to stderr so it does not pollute MCP stdio)
# ---------------------------------------------------------------------------
class _Logger:
    def __init__(self, name: str):
        self._inner = logging.getLogger(name)
        self._inner.setLevel(logging.INFO)
        if not self._inner.handlers:
            h = logging.StreamHandler(sys.stderr)
            h.setFormatter(logging.Formatter("%(message)s"))
            self._inner.addHandler(h)

    def _emit(self, level: str, event: str, **fields):
        self._inner.info(json.dumps({
            "level": level,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "service": "mcp-server",
            "event": event,
            **fields,
        }))

    def info(self, event: str, **kw):  self._emit("INFO",  event, **kw)
    def warn(self, event: str, **kw):  self._emit("WARN",  event, **kw)
    def error(self, event: str, **kw): self._emit("ERROR", event, **kw)


log = _Logger("code_execution_mcp")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LAMBDA_FUNCTION_NAME = os.environ.get("LAMBDA_FUNCTION_NAME", "code-execution-sandbox")
AWS_REGION           = os.environ.get("AWS_REGION", "us-east-1")
MAX_TIMEOUT          = int(os.environ.get("MAX_EXECUTION_TIMEOUT", "30"))
# Optional custom endpoint (LocalStack / testing)
_ENDPOINT_URL        = os.environ.get("AWS_ENDPOINT_URL") or None


# ---------------------------------------------------------------------------
# Lambda invocation
# ---------------------------------------------------------------------------
def _invoke_lambda(
    code: str,
    timeout: int,
    stdin_data: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Synchronously invoke the sandbox Lambda and return its parsed payload."""
    client = boto3.client(
        "lambda",
        region_name=AWS_REGION,
        **({"endpoint_url": _ENDPOINT_URL} if _ENDPOINT_URL else {}),
    )

    payload = {
        "code": code,
        "timeout": timeout,
        "stdin": stdin_data,
        "correlation_id": correlation_id,
    }

    wall_start = time.monotonic()
    try:
        response = client.invoke(
            FunctionName=LAMBDA_FUNCTION_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
    except (BotoCoreError, ClientError) as exc:
        wall_ms = int((time.monotonic() - wall_start) * 1000)
        log.error(
            "lambda_invoke_failed",
            correlation_id=correlation_id,
            error=str(exc),
            wall_ms=wall_ms,
        )
        return {
            "stdout": "", "stderr": f"AWS error invoking Lambda: {exc}",
            "exit_code": -1, "execution_time_ms": 0,
            "timed_out": False, "blocked": False,
            "correlation_id": correlation_id,
        }

    wall_ms = int((time.monotonic() - wall_start) * 1000)
    function_error = response.get("FunctionError")
    raw = response["Payload"].read()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"error": raw.decode()}

    if function_error:
        error_msg = result.get("errorMessage", str(result))
        log.error(
            "lambda_function_error",
            correlation_id=correlation_id,
            function_error=function_error,
            error_message=error_msg,
            wall_ms=wall_ms,
        )
        return {
            "stdout": "", "stderr": f"Lambda error ({function_error}): {error_msg}",
            "exit_code": -1, "execution_time_ms": 0,
            "timed_out": False, "blocked": False,
            "correlation_id": correlation_id,
        }

    log.info(
        "lambda_invoke_success",
        correlation_id=correlation_id,
        exit_code=result.get("exit_code", -1),
        execution_time_ms=result.get("execution_time_ms", 0),
        wall_ms=wall_ms,
        timed_out=result.get("timed_out", False),
        blocked=result.get("blocked", False),
    )
    return result


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
server = Server("code-execution-mcp")


@server.list_tools()
async def handle_list_tools() -> ListToolsResult:
    return ListToolsResult(
        tools=[
            Tool(
                name="execute_python",
                description=(
                    "Execute Python 3 code inside a secure AWS Lambda sandbox.\n\n"
                    "Returns stdout, stderr, exit code, and wall-clock execution time.\n\n"
                    "Security restrictions (enforced automatically):\n"
                    "  - No network access (VPC with no internet egress)\n"
                    "  - Blocked modules: subprocess, socket, ctypes, urllib, requests, …\n"
                    "  - Blocked builtins: exec(), eval(), compile()\n"
                    "  - Memory: 256 MB cap  |  Disk writes: 10 MB cap\n"
                    "  - Max execution: 30 s  |  Max code size: 64 KB\n\n"
                    "Code violating these rules is rejected before execution."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Python 3 source code to execute.",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Max execution time in seconds (1–30). Defaults to 10.",
                            "default": 10,
                            "minimum": 1,
                            "maximum": 30,
                        },
                        "stdin": {
                            "type": "string",
                            "description": "Optional data passed to stdin.",
                            "default": "",
                        },
                    },
                    "required": ["code"],
                },
            )
        ]
    )


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> CallToolResult:
    if name != "execute_python":
        return CallToolResult(
            content=[TextContent(type="text", text=f"Unknown tool: {name}")]
        )

    correlation_id = str(uuid.uuid4())
    code       = arguments.get("code", "")
    timeout    = min(int(arguments.get("timeout", 10)), MAX_TIMEOUT)
    stdin_data = arguments.get("stdin", "") or ""

    log.info(
        "tool_call_received",
        tool="execute_python",
        correlation_id=correlation_id,
        code_length=len(code),
        timeout=timeout,
    )

    if not code.strip():
        return CallToolResult(
            content=[TextContent(type="text", text="Error: no code provided.")]
        )

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: _invoke_lambda(code, timeout, stdin_data, correlation_id),
    )

    # --- format response ---------------------------------------------------
    parts: list[str] = []

    if result.get("blocked"):
        parts.append(
            "**Code rejected by security policy**\n\n"
            + result.get("stderr", "")
        )
    else:
        if result.get("stdout"):
            parts.append(f"**stdout:**\n```\n{result['stdout'].rstrip()}\n```")
        if result.get("stderr"):
            parts.append(f"**stderr:**\n```\n{result['stderr'].rstrip()}\n```")

    parts.append(f"**exit code:** {result.get('exit_code', -1)}")
    parts.append(f"**execution time:** {result.get('execution_time_ms', 0)} ms")
    parts.append(f"**correlation id:** `{correlation_id}`")

    return CallToolResult(
        content=[TextContent(type="text", text="\n\n".join(parts) or "No output.")]
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    log.info(
        "server_starting",
        lambda_function=LAMBDA_FUNCTION_NAME,
        region=AWS_REGION,
        max_timeout=MAX_TIMEOUT,
        endpoint=_ENDPOINT_URL or "aws-default",
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
