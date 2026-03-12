#!/usr/bin/env python3
"""
Automated MCP integration test.

Spawns the MCP server as a subprocess and verifies tool execution via
JSON-RPC over stdio – no browser or external tooling required.

Usage
-----
# Against LocalStack (set env vars first – see README Option A step 5):
  python scripts/test_mcp.py

# Against real AWS:
  LAMBDA_FUNCTION_NAME=code-execution-dev-sandbox \\
  AWS_REGION=us-east-1 \\
  python scripts/test_mcp.py
"""

import json
import os
import subprocess
import sys
import time

SERVER_CMD = [sys.executable, os.path.join(os.path.dirname(__file__), "..", "src", "server.py")]

# ---------------------------------------------------------------------------
# JSON-RPC message templates
# ---------------------------------------------------------------------------
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.1"},
    },
}

LIST_TOOLS = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {},
}

CALL_HELLO = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
        "name": "execute_python",
        "arguments": {"code": "print('hello from MCP sandbox')", "timeout": 5},
    },
}

CALL_ARITHMETIC = {
    "jsonrpc": "2.0",
    "id": 4,
    "method": "tools/call",
    "params": {
        "name": "execute_python",
        "arguments": {"code": "result = 2 ** 10\nprint(f'2^10 = {result}')", "timeout": 5},
    },
}

CALL_STDERR = {
    "jsonrpc": "2.0",
    "id": 5,
    "method": "tools/call",
    "params": {
        "name": "execute_python",
        "arguments": {
            "code": "import sys\nprint('out')\nprint('err', file=sys.stderr)",
            "timeout": 5,
        },
    },
}

CALL_TIMEOUT = {
    "jsonrpc": "2.0",
    "id": 6,
    "method": "tools/call",
    "params": {
        "name": "execute_python",
        "arguments": {"code": "import time; time.sleep(60)", "timeout": 2},
    },
}

CALL_SYNTAX_ERROR = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "tools/call",
    "params": {
        "name": "execute_python",
        "arguments": {"code": "def foo(\n    pass", "timeout": 5},
    },
}

CALL_STDIN = {
    "jsonrpc": "2.0",
    "id": 8,
    "method": "tools/call",
    "params": {
        "name": "execute_python",
        "arguments": {
            "code": "import sys\ndata = sys.stdin.read()\nprint('received:', data.strip())",
            "timeout": 5,
            "stdin": "hello stdin",
        },
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def send(proc: subprocess.Popen, msg: dict) -> dict:
    """Send one JSON-RPC message and read the response line."""
    line = json.dumps(msg) + "\n"
    proc.stdin.write(line.encode())
    proc.stdin.flush()
    raw = proc.stdout.readline()
    return json.loads(raw)


def assert_text_contains(response: dict, substring: str, label: str):
    content = response["result"]["content"][0]["text"]
    assert substring in content, f"{label}: expected '{substring}' in:\n{content}"
    return content


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def main():
    env = {**os.environ}
    proc = subprocess.Popen(
        SERVER_CMD,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    time.sleep(0.5)  # allow server startup

    passed = 0
    failed = 0

    try:
        # 1. Initialize
        r = send(proc, INITIALIZE)
        assert "result" in r, f"initialize failed: {r}"
        print("✓ initialize")
        passed += 1

        # 2. List tools
        r = send(proc, LIST_TOOLS)
        tools = r["result"]["tools"]
        assert any(t["name"] == "execute_python" for t in tools), "execute_python not listed"
        print("✓ list_tools  –  execute_python is present")
        passed += 1

        # 3. Basic print
        r = send(proc, CALL_HELLO)
        content = assert_text_contains(r, "hello from MCP sandbox", "basic print")
        print(f"✓ execute_python (print)  –  stdout: {content!r:.60}")
        passed += 1

        # 4. Arithmetic
        r = send(proc, CALL_ARITHMETIC)
        content = assert_text_contains(r, "2^10 = 1024", "arithmetic")
        print(f"✓ execute_python (arithmetic)  –  {content!r:.60}")
        passed += 1

        # 5. stderr capture
        r = send(proc, CALL_STDERR)
        text = r["result"]["content"][0]["text"]
        assert "out" in text and "err" in text, f"stderr test failed: {text}"
        print("✓ execute_python (stderr)  –  both stdout and stderr captured")
        passed += 1

        # 6. Timeout enforcement
        r = send(proc, CALL_TIMEOUT)
        content = assert_text_contains(r, "124", "timeout exit code")
        print(f"✓ execute_python (timeout)  –  correctly returned exit code 124")
        passed += 1

        # 7. Syntax error propagates via stderr + non-zero exit
        r = send(proc, CALL_SYNTAX_ERROR)
        text = r["result"]["content"][0]["text"]
        # Should have a non-zero exit code
        assert "exit code:** 0" not in text, f"syntax error should not exit 0: {text}"
        print("✓ execute_python (syntax error)  –  non-zero exit code returned")
        passed += 1

        # 8. stdin forwarding
        r = send(proc, CALL_STDIN)
        content = assert_text_contains(r, "received: hello stdin", "stdin")
        print(f"✓ execute_python (stdin)  –  {content!r:.60}")
        passed += 1

    except AssertionError as exc:
        print(f"\n✗ FAIL: {exc}")
        failed += 1
    except Exception as exc:  # noqa: BLE001
        print(f"\n✗ ERROR: {exc}")
        failed += 1
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    print(f"\n{'='*40}")
    print(f"Results: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
