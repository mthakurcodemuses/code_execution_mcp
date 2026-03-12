# Code Execution MCP Server

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that executes Python code safely inside an **AWS Lambda sandbox** and returns the results.

---

## Architecture

```
 Claude / AI client
       │  MCP (stdio)
       ▼
 ┌─────────────┐        lambda:InvokeFunction        ┌──────────────────────────┐
 │  MCP Server │ ──────────────────────────────────► │  Lambda (Python 3.12)    │
 │ src/server  │ ◄────────────────────────────────── │  lambda/handler.py       │
 └─────────────┘      stdout / stderr / exit code    │                          │
                                                      │  Private VPC subnet      │
                                                      │  No internet egress      │
                                                      └──────────────────────────┘
```

### Components

| Component | Purpose |
|-----------|---------|
| `src/server.py` | MCP server (stdio transport) exposing `execute_python` tool |
| `lambda/handler.py` | Lambda handler – writes code to `/tmp`, spawns a restricted subprocess |
| `infrastructure/` | Terraform managing all AWS resources |
| `deploy.sh` | One-shot build + deploy + smoke-test script |

### AWS Resources Created by Terraform

| Resource | Description |
|----------|-------------|
| `aws_lambda_function` | Python 3.12 sandbox function |
| `aws_iam_role` (lambda-exec) | Lambda execution role – only CloudWatch Logs + VPC ENI permissions |
| `aws_iam_policy` (deny-aws-apis) | Defence-in-depth deny policy on the Lambda role |
| `aws_iam_policy` (mcp-invoke) | Allows the MCP server principal to call `lambda:InvokeFunction` |
| `aws_vpc` | Isolated VPC – no IGW, no NAT GW |
| `aws_subnet` × 2 | Private subnets (one per AZ) |
| `aws_security_group` | Deny all inbound; allow HTTPS to VPC endpoints only |
| `aws_vpc_endpoint` (logs, lambda) | Interface endpoints so the runtime reaches CloudWatch without internet |
| `aws_cloudwatch_log_group` | `/aws/lambda/<name>-sandbox`, 14-day retention |

---

## Security Model

1. **No internet egress** – The Lambda VPC has no Internet Gateway and no NAT Gateway. Executed code cannot make outbound network calls.
2. **Minimal IAM** – The execution role has only the two AWS-managed policies needed for Lambda-in-VPC. A deny-all policy is layered on top.
3. **Resource limits** – The subprocess inherits `RLIMIT_AS` (256 MB), `RLIMIT_FSIZE` (10 MB), and `RLIMIT_NOFILE` (32 fds) via `resource.setrlimit`.
4. **Subprocess isolation** – Code runs as a separate `python3 -I` process (isolated mode, no site-packages, stripped environment).
5. **Hard timeout** – Lambda timeout is 35 s; handler kills the subprocess after the caller-requested timeout (≤ 30 s).
6. **Ephemeral `/tmp`** – Leftover files from warm invocations are removed before each execution.

---

## Quick Start

### Prerequisites

- AWS CLI configured (`aws configure` or env vars)
- Terraform ≥ 1.6
- Python ≥ 3.10 with `pip`

### 1. Deploy

```bash
./deploy.sh --env dev --region us-east-1
```

This will:
- Install MCP server dependencies (`pip install -r requirements.txt`)
- Run `terraform init` and `terraform apply`
- Write `.env` with `LAMBDA_FUNCTION_NAME` and `AWS_REGION`
- Run a smoke test against the deployed Lambda

### 2. Attach the invoke policy to your caller

After deployment, Terraform prints the `mcp_invoke_policy_arn`. Attach it to the IAM role or user your MCP server runs as:

```bash
aws iam attach-role-policy \
  --role-name <your-mcp-server-role> \
  --policy-arn <mcp_invoke_policy_arn from output>
```

### 3. Run the MCP server

```bash
export $(cat .env | xargs)
python src/server.py
```

### 4. Add to Claude Code

In `~/.claude/claude.json`:

```json
{
  "mcpServers": {
    "code-execution": {
      "command": "python",
      "args": ["/path/to/code_execution_mcp/src/server.py"],
      "env": {
        "LAMBDA_FUNCTION_NAME": "code-execution-dev-sandbox",
        "AWS_REGION": "us-east-1"
      }
    }
  }
}
```

---

## MCP Tool Reference

### `execute_python`

Execute Python 3 code in the sandbox.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `code` | string | yes | Python source code |
| `timeout` | integer | no | Max execution time in seconds (1–30, default 10) |
| `stdin` | string | no | Data passed to stdin |

**Response** includes `stdout`, `stderr`, `exit code`, and `execution time`.

---

## Configuration

### MCP Server (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `LAMBDA_FUNCTION_NAME` | `code-execution-sandbox` | Lambda function to invoke |
| `AWS_REGION` | `us-east-1` | AWS region |
| `MAX_EXECUTION_TIMEOUT` | `30` | Upper bound on timeout parameter |

### Terraform (variables)

See [`infrastructure/terraform.tfvars.example`](infrastructure/terraform.tfvars.example) for all options.

---

## Tear Down

```bash
cd infrastructure
terraform destroy -var="environment=dev"
```

---

## Local Development & Testing

You can develop and test the entire stack locally without a real AWS account using **LocalStack** (free tier covers Lambda, IAM, and CloudWatch Logs).

### Option A – LocalStack (full AWS mock, recommended)

#### 1. Install LocalStack and `awslocal`

```bash
pip install localstack awscli-local
# Or with Docker:
docker pull localstack/localstack
```

#### 2. Start LocalStack

```bash
# Runs on http://localhost:4566 by default
localstack start -d      # -d = detached (background)
```

Verify it is up:

```bash
curl http://localhost:4566/_localstack/health | python -m json.tool
```

#### 3. Deploy the Lambda to LocalStack

```bash
# Package the handler
cd lambda
zip handler.zip handler.py

# Create the IAM execution role (LocalStack doesn't enforce policies, but the API call is required)
awslocal iam create-role \
  --role-name code-execution-dev-lambda-exec \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

# Create the Lambda function
awslocal lambda create-function \
  --function-name code-execution-dev-sandbox \
  --runtime python3.12 \
  --role arn:aws:iam::000000000000:role/code-execution-dev-lambda-exec \
  --handler handler.lambda_handler \
  --zip-file fileb://handler.zip \
  --timeout 35 \
  --memory-size 512 \
  --environment Variables='{MAX_TIMEOUT_SECONDS=30}'

cd ..
```

#### 4. Smoke-test the Lambda directly

```bash
awslocal lambda invoke \
  --function-name code-execution-dev-sandbox \
  --payload '{"code":"print(\"hello from sandbox\")","timeout":5}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/out.json && cat /tmp/out.json
```

Expected output:
```json
{"stdout": "hello from sandbox\n", "stderr": "", "exit_code": 0, "execution_time_ms": 42}
```

#### 5. Start the MCP server against LocalStack

```bash
pip install -r requirements.txt

export LAMBDA_FUNCTION_NAME=code-execution-dev-sandbox
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
# Point boto3 at LocalStack
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test

python src/server.py
```

The server listens on stdio and is now ready to accept MCP requests.

#### 6. Test MCP tools with the MCP Inspector

Install the inspector:
```bash
npx @modelcontextprotocol/inspector python src/server.py
```

The inspector opens a browser UI at `http://localhost:5173` where you can:
- Click **List Tools** to verify `execute_python` appears.
- Fill in `code` and click **Call Tool** to run a snippet.

#### 7. Test MCP tools with a raw JSON-RPC script

For CI or scripted testing without the browser UI:

```bash
python scripts/test_mcp.py
```

The script at `scripts/test_mcp.py` (created below) spawns the server as a subprocess and sends JSON-RPC messages over stdio.

---

### Option B – Call the Lambda handler directly (no AWS at all)

If you only want to test the execution logic without any AWS dependency:

```bash
cd lambda

python - <<'EOF'
import json
from handler import lambda_handler

# Basic print
result = lambda_handler({"code": "print('hello')", "timeout": 5}, None)
print(json.dumps(result, indent=2))

# Timeout behaviour
result = lambda_handler({"code": "import time; time.sleep(60)", "timeout": 2}, None)
print(json.dumps(result, indent=2))

# Runtime error
result = lambda_handler({"code": "1/0", "timeout": 5}, None)
print(json.dumps(result, indent=2))
EOF
```

---

### Helper script – `scripts/test_mcp.py`

Create this file to run automated end-to-end MCP tests:

```python
#!/usr/bin/env python3
"""
Automated MCP integration test.
Spawns the MCP server as a subprocess and verifies tool execution.

Usage:
  # Against LocalStack (set env vars first as shown in Option A step 5)
  python scripts/test_mcp.py

  # Against real AWS (set AWS_PROFILE or AWS_ACCESS_KEY_ID etc.)
  LAMBDA_FUNCTION_NAME=code-execution-dev-sandbox python scripts/test_mcp.py
"""

import json
import os
import subprocess
import sys
import time

SERVER_CMD = [sys.executable, "src/server.py"]

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.1"},
    },
}

LIST_TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

CALL_HELLO = {
    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
    "params": {
        "name": "execute_python",
        "arguments": {"code": "print('hello from MCP')", "timeout": 5},
    },
}

CALL_TIMEOUT = {
    "jsonrpc": "2.0", "id": 4, "method": "tools/call",
    "params": {
        "name": "execute_python",
        "arguments": {"code": "import time; time.sleep(60)", "timeout": 2},
    },
}


def send(proc, msg):
    line = json.dumps(msg) + "\n"
    proc.stdin.write(line.encode())
    proc.stdin.flush()
    raw = proc.stdout.readline()
    return json.loads(raw)


def main():
    env = {**os.environ}
    proc = subprocess.Popen(
        SERVER_CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env
    )
    time.sleep(0.5)  # wait for server start

    try:
        r = send(proc, INITIALIZE)
        assert "result" in r, f"initialize failed: {r}"
        print("✓ initialize")

        r = send(proc, LIST_TOOLS)
        tools = r["result"]["tools"]
        assert any(t["name"] == "execute_python" for t in tools)
        print("✓ list_tools – execute_python present")

        r = send(proc, CALL_HELLO)
        content = r["result"]["content"][0]["text"]
        assert "hello from MCP" in content, f"unexpected output: {content}"
        print(f"✓ execute_python (hello) – output: {content!r}")

        r = send(proc, CALL_TIMEOUT)
        content = r["result"]["content"][0]["text"]
        assert "timed out" in content.lower() or "124" in content, \
            f"expected timeout, got: {content}"
        print(f"✓ execute_python (timeout) – correctly detected timeout")

        print("\nAll tests passed.")
    finally:
        proc.terminate()
        proc.wait()


if __name__ == "__main__":
    main()
```

Run it:

```bash
mkdir -p scripts
# (paste the script above into scripts/test_mcp.py)
python scripts/test_mcp.py
```

---

### Common test cases

```python
# stdout capture
print("hello")

# stderr capture
import sys; print("err", file=sys.stderr)

# multi-line output
for i in range(5):
    print(i)

# stdin
import sys; data = sys.stdin.read(); print("got:", data)
# (pass --stdin "my input" or set the `stdin` parameter)

# timeout enforcement
import time; time.sleep(60)

# memory limit (will be killed by RLIMIT_AS)
x = bytearray(300 * 1024 * 1024)

# import restriction (no internet, so urllib.request.urlopen will fail)
import urllib.request
urllib.request.urlopen("https://example.com")
```

---

### Updating the Lambda after code changes

```bash
cd lambda
zip handler.zip handler.py

# LocalStack
awslocal lambda update-function-code \
  --function-name code-execution-dev-sandbox \
  --zip-file fileb://handler.zip

# Real AWS
aws lambda update-function-code \
  --function-name code-execution-dev-sandbox \
  --zip-file fileb://handler.zip
```
