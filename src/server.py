"""
MCP Server – sandboxed Python code execution via AWS Lambda.

Tools exposed:
  - execute_python: run Python code inside an AWS Lambda sandbox
"""

import asyncio
import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

LAMBDA_FUNCTION_NAME = os.environ.get("LAMBDA_FUNCTION_NAME", "code-execution-sandbox")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MAX_TIMEOUT = int(os.environ.get("MAX_EXECUTION_TIMEOUT", "30"))


# ---------------------------------------------------------------------------
# Lambda invocation
# ---------------------------------------------------------------------------

def _invoke_lambda(code: str, timeout: int, stdin_data: str) -> dict[str, Any]:
    client = boto3.client("lambda", region_name=AWS_REGION)
    payload = {"code": code, "timeout": timeout, "stdin": stdin_data}
    try:
        response = client.invoke(
            FunctionName=LAMBDA_FUNCTION_NAME,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )
    except (BotoCoreError, ClientError) as exc:
        return {
            "stdout": "",
            "stderr": f"AWS error: {exc}",
            "exit_code": -1,
            "execution_time_ms": 0,
        }

    function_error = response.get("FunctionError")
    raw = response["Payload"].read()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        result = {"error": raw.decode()}

    if function_error:
        error_msg = result.get("errorMessage", str(result))
        return {
            "stdout": "",
            "stderr": f"Lambda error ({function_error}): {error_msg}",
            "exit_code": -1,
            "execution_time_ms": 0,
        }

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
                    "Execute Python 3 code inside a secure AWS Lambda sandbox. "
                    "Returns stdout, stderr, exit code, and wall-clock execution time. "
                    "Outbound network access is disabled inside the sandbox. "
                    "Execution is capped at the specified timeout (max 30 s)."
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
                            "description": "Optional data to pass on stdin.",
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

    code = arguments.get("code", "")
    timeout = min(int(arguments.get("timeout", 10)), MAX_TIMEOUT)
    stdin_data = arguments.get("stdin", "") or ""

    if not code.strip():
        return CallToolResult(
            content=[TextContent(type="text", text="Error: no code provided.")]
        )

    logger.info("Executing Python code (%d chars) timeout=%ds", len(code), timeout)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        lambda: _invoke_lambda(code, timeout, stdin_data),
    )

    parts: list[str] = []
    if result.get("stdout"):
        parts.append(f"**stdout:**\n```\n{result['stdout'].rstrip()}\n```")
    if result.get("stderr"):
        parts.append(f"**stderr:**\n```\n{result['stderr'].rstrip()}\n```")
    parts.append(f"**exit code:** {result.get('exit_code', -1)}")
    parts.append(f"**execution time:** {result.get('execution_time_ms', 0)} ms")

    return CallToolResult(
        content=[TextContent(type="text", text="\n\n".join(parts) or "No output.")]
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    logger.info("MCP server starting (Lambda=%s, region=%s)", LAMBDA_FUNCTION_NAME, AWS_REGION)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
