# Observability & Triage Guide

This document explains **how data flows through the system**, how logs and
traces are structured, and how to use correlation IDs to investigate any
execution end-to-end.

---

## 1. End-to-End Execution Flow with Observability

```
┌──────────────────────────────────────────────────────────────────────────┐
│  1. Claude / AI client calls execute_python({code, timeout})             │
└─────────────────────────────┬────────────────────────────────────────────┘
                              │
                ┌─────────────▼──────────────────────┐
                │  MCP Server  (src/server.py)         │
                │                                      │
                │  a) Generate correlation_id = UUID4  │
                │  b) Log  tool_call_received  [JSON]  │
                │  c) Build Lambda payload:            │
                │     {code, timeout, stdin,           │
                │      correlation_id}                 │
                │  d) boto3.lambda.invoke() →          │
                └──────────────┬───────────────────────┘
                               │  RequestResponse (synchronous)
                               │  correlation_id travels in payload
                               ▼
                ┌──────────────────────────────────────┐
                │  AWS Lambda  (lambda/handler.py)     │
                │                                      │
                │  e) Extract correlation_id           │
                │  f) Annotate X-Ray root segment      │
                │  g) Log  execution_start  [JSON]     │
                │  h) Run _analyse_code() (AST scan)   │
                │     → if violations:                 │
                │       Log  code_blocked  [JSON]      │
                │       Emit BlockedSubmissions metric │
                │       Return {blocked:true, ...}     │
                │  i) Open X-Ray subsegment            │
                │     "subprocess_execute"             │
                │  j) Fork: python3 -I -u <tmp>.py    │
                │  k) Wait (timeout enforced)          │
                │  l) Annotate subsegment:             │
                │     exit_code, exec_ms, timed_out   │
                │  m) Log  execution_complete  [JSON]  │
                │  n) Emit EMF metrics                 │
                │  o) Return {stdout, stderr,          │
                │     exit_code, exec_ms,              │
                │     correlation_id, ...}             │
                └──────────────┬───────────────────────┘
                               │
                ┌──────────────▼───────────────────────┐
                │  MCP Server  (src/server.py)         │
                │                                      │
                │  p) Log  lambda_invoke_success       │
                │     or   lambda_invoke_failed  [JSON]│
                │  q) Format response markdown         │
                │  r) Return TextContent to Claude     │
                └──────────────────────────────────────┘
```

---

## 2. Log Formats

### 2.1 MCP Server logs (stderr)

All log lines are newline-delimited JSON. The MCP server writes to **stderr**
so the log stream is separate from the stdio JSON-RPC protocol on stdout.

#### `tool_call_received`
Emitted when the MCP client invokes `execute_python`.

```json
{
  "level": "INFO",
  "timestamp": "2026-03-12T10:23:44.000Z",
  "service": "mcp-server",
  "event": "tool_call_received",
  "tool": "execute_python",
  "correlation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "code_length": 42,
  "timeout": 10
}
```

#### `lambda_invoke_success`
Emitted after Lambda returns, whether or not the code succeeded.

```json
{
  "level": "INFO",
  "timestamp": "2026-03-12T10:23:44.350Z",
  "service": "mcp-server",
  "event": "lambda_invoke_success",
  "correlation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "exit_code": 0,
  "execution_time_ms": 87,
  "wall_ms": 312,
  "timed_out": false,
  "blocked": false
}
```

> **wall_ms** = total time from boto3 call to response, including Lambda cold
> start + network RTT. **execution_time_ms** = time the subprocess ran inside
> Lambda.  The difference is Lambda + network overhead.

#### `lambda_invoke_failed`
Emitted when boto3 raises an exception (network error, IAM, etc.).

```json
{
  "level": "ERROR",
  "timestamp": "2026-03-12T10:23:44.350Z",
  "service": "mcp-server",
  "event": "lambda_invoke_failed",
  "correlation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "error": "An error occurred (AccessDeniedException)…",
  "wall_ms": 210
}
```

#### `lambda_function_error`
Emitted when Lambda itself returned a `FunctionError` header (unhandled
exception, OOM, timeout at the Lambda level).

```json
{
  "level": "ERROR",
  "event": "lambda_function_error",
  "correlation_id": "a1b2c3d4-...",
  "function_error": "Unhandled",
  "error_message": "Task timed out after 35.00 seconds",
  "wall_ms": 35412
}
```

---

### 2.2 Lambda logs (CloudWatch Logs)

Log group: `/aws/lambda/<project>-<env>-sandbox`

All lines are structured JSON.  Use **CloudWatch Log Insights** (§4) to query
them.

#### `execution_start`
```json
{
  "level": "INFO",
  "timestamp": "2026-03-12T10:23:44.050Z",
  "event": "execution_start",
  "correlation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "code_length": 42,
  "timeout_requested": 10
}
```

#### `execution_complete`
```json
{
  "level": "INFO",
  "timestamp": "2026-03-12T10:23:44.145Z",
  "event": "execution_complete",
  "correlation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "exit_code": 0,
  "execution_time_ms": 87,
  "timed_out": false,
  "stdout_bytes": 14,
  "stderr_bytes": 0
}
```

#### `code_blocked_by_static_analysis`
```json
{
  "level": "WARN",
  "timestamp": "2026-03-12T10:23:44.060Z",
  "event": "code_blocked_by_static_analysis",
  "correlation_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "violation_count": 2,
  "violations": [
    "line 3: import of blocked module 'subprocess'",
    "line 7: call to blocked built-in 'eval'"
  ]
}
```

#### `empty_code`
```json
{
  "level": "WARN",
  "event": "empty_code",
  "correlation_id": "a1b2c3d4-..."
}
```

---

## 3. X-Ray Tracing

X-Ray is enabled via **Active Tracing** on the Lambda function.

### Trace structure

```
Trace  (1 per invocation)
│
├── Segment: code-execution-sandbox  (Lambda root – automatic)
│     Annotations:
│       correlation_id  = "a1b2c3d4-…"
│       code_length     = 42
│       timeout_requested = 10
│
│     Subsegment: Initialization  (cold-start only)
│
└── Subsegment: subprocess_execute  (added by handler.py)
      Annotations:
        exit_code          = 0
        execution_time_ms  = 87
        timed_out          = false
        correlation_id     = "a1b2c3d4-…"
```

### Finding a trace by correlation ID

1. Open **AWS X-Ray → Traces** in the console.
2. In the filter bar enter:

   ```
   annotation.correlation_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
   ```

3. Click the matching trace to see the full waterfall.

### Service map

X-Ray automatically builds a service map. You will see:

```
[MCP Server – not visible in X-Ray]  →  Lambda: code-execution-sandbox
```

To propagate the trace from the MCP server into Lambda, set the
`_X_AMZN_TRACE_ID` environment variable in the Lambda payload or use the
X-Ray SDK's `inject_context`. (Optional advanced setup.)

---

## 4. CloudWatch Log Insights Queries

Open **CloudWatch → Log Insights**, select the log group
`/aws/lambda/<name>-sandbox`, then run any of the queries below.

### 4.1 Find all events for a single correlation ID

```
filter correlation_id = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
| fields timestamp, event, exit_code, execution_time_ms, timed_out
| sort @timestamp asc
```

### 4.2 Error rate over time (5-minute buckets)

```
filter event = "execution_complete"
| stats
    count(*) as total,
    sum(exit_code != 0) as errors,
    100 * sum(exit_code != 0) / count(*) as error_pct
  by bin(5m)
```

### 4.3 P50 / P95 / P99 execution time

```
filter event = "execution_complete"
| stats
    pct(execution_time_ms, 50) as p50,
    pct(execution_time_ms, 95) as p95,
    pct(execution_time_ms, 99) as p99
  by bin(5m)
```

### 4.4 Timeout distribution

```
filter event = "execution_complete" and timed_out = 1
| stats count(*) as timeouts by bin(5m)
```

### 4.5 Blocked submissions with violation details

```
filter event = "code_blocked_by_static_analysis"
| fields timestamp, correlation_id, violation_count, violations
| sort @timestamp desc
| limit 50
```

### 4.6 Cold start duration

```
filter @message like "Init Duration"
| parse @message "Init Duration: * ms" as init_ms
| stats avg(init_ms), max(init_ms) by bin(1h)
```

### 4.7 Slowest executions in the last hour

```
filter event = "execution_complete"
| sort execution_time_ms desc
| fields timestamp, correlation_id, execution_time_ms, exit_code
| limit 20
```

---

## 5. CloudWatch Metrics (EMF)

Namespace: **`CodeExecution`**  |  Dimension: `Environment`

| Metric | Unit | When emitted |
|--------|------|--------------|
| `ExecutionTime` | Milliseconds | Every successful execution |
| `ExecutionErrors` | Count | exit_code ≠ 0 and not timed_out |
| `TimeoutCount` | Count | exit_code = 124 (timed_out = true) |
| `CodeLength` | Bytes | Every execution attempt |
| `BlockedSubmissions` | Count | Static analysis rejection |

View them in **CloudWatch → Metrics → Custom namespaces → CodeExecution**.

---

## 6. Alarms Summary

| Alarm | Threshold | Meaning |
|-------|-----------|---------|
| `error-rate-high` | > 10% over 5 min | Systematic execution failures |
| `timeout-rate-high` | > 5% over 5 min | Users submitting long-running code |
| `duration-p99-high` | P99 > 28 s | Lambda close to hard timeout limit |
| `throttles` | Any in 1 min | Concurrency limit hit |
| `blocked-submissions` | > 10 in 5 min | Potential abuse / scanning |

All alarms route to the SNS topic `<name>-alarms` → email or PagerDuty/Slack.

---

## 7. Step-by-Step Triage Runbook

### Scenario A – User reports "code didn't run / no output"

1. Ask the user for the **correlation id** shown in the tool response footer
   (`correlation id: \`a1b2c3d4-…\``).
2. Run Log Insights query **4.1** with that ID.
3. Look for:
   - `code_blocked_by_static_analysis` → show user the violation list.
   - `execution_complete` with `exit_code ≠ 0` → check stderr in Lambda logs
     (note: stdout/stderr are not logged to CloudWatch by default, only sizes;
     use X-Ray metadata for short outputs or add CloudWatch logging if needed).
   - No `execution_start` at all → the Lambda was never invoked. Check MCP
     server stderr for `lambda_invoke_failed`.

### Scenario B – Execution is slow

1. Pull P99 from query **4.3**.
2. Check the `duration-p99-high` alarm state.
3. If consistently slow: check for cold starts via query **4.6** and consider
   enabling Provisioned Concurrency.
4. In X-Ray → find the trace by correlation ID → expand the
   `subprocess_execute` subsegment to see subprocess time vs. Lambda overhead.

### Scenario C – High error rate alarm fires

1. Run query **4.2** to confirm the time window.
2. Run query **4.1** on a sample of error correlation IDs (pick from
   `execution_complete` where `exit_code != 0`).
3. Check if errors are clustered around a specific code pattern or are random
   infrastructure failures.
4. For infrastructure failures: check `lambda_invoke_failed` in MCP server
   logs → look for IAM, VPC, or throttle errors.

### Scenario D – Blocked submissions alarm fires

1. Run query **4.5** to see what violations are occurring.
2. If violations are legitimate use cases, consider relaxing a specific rule in
   `_BLOCKED_MODULES` / `_BLOCKED_BUILTINS` in `lambda/handler.py`.
3. If violations look like probing/scanning, note the timing and check
   CloudTrail for the calling IAM principal.

### Scenario E – Lambda throttling

1. Confirm with `aws cloudwatch get-metric-statistics` or the dashboard.
2. Increase the **Reserved Concurrency** for the sandbox function, or raise
   the account-level concurrency limit via AWS Support.
3. Short-term mitigation: add exponential backoff in the MCP server's
   `_invoke_lambda` function.

---

## 8. Key Field Reference

| Field | Where | Meaning |
|-------|-------|---------|
| `correlation_id` | MCP log, Lambda log, X-Ray annotation, tool response | Links one user request across all systems |
| `wall_ms` | MCP server log | Total time from boto3 call to response (includes cold start + network) |
| `execution_time_ms` | Lambda log + response | Time the Python subprocess actually ran |
| `exit_code` | Lambda log + response | OS exit code. `0`=success, `1`=Python error, `124`=timeout, `-1`=internal error |
| `timed_out` | Lambda log + response | `true` when exit_code=124 |
| `blocked` | Lambda log + response | `true` when static analysis rejected the code |
| `stdout_bytes` / `stderr_bytes` | Lambda log | Byte lengths (not content); useful for detecting empty output vs. long output |
| `violation_count` + `violations` | Lambda WARN log | Details of what static analysis rejected |
