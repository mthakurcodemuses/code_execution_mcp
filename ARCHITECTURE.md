# Architecture – Code Execution MCP Server

## Table of Contents

1. [Current Architecture](#1-current-architecture)
2. [Component Deep-Dive](#2-component-deep-dive)
3. [Data Flow](#3-data-flow)
4. [Security Model](#4-security-model)
5. [Observability](#5-observability)
6. [Scalability Analysis](#6-scalability-analysis)
7. [Enterprise-Grade Target Architecture](#7-enterprise-grade-target-architecture)
8. [Migration Roadmap](#8-migration-roadmap)

---

## 1. Current Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Developer machine / Claude Code session                                 │
│                                                                          │
│  ┌──────────────────┐   JSON-RPC / stdio   ┌─────────────────────────┐  │
│  │   Claude / AI    │ ──────────────────►  │   MCP Server            │  │
│  │   Client         │ ◄──────────────────  │   src/server.py         │  │
│  └──────────────────┘    tool results      │   (Python asyncio)      │  │
│                                            └───────────┬─────────────┘  │
└───────────────────────────────────────────────────────┼────────────────┘
                                                         │
                              AWS SDK (boto3)            │ lambda:InvokeFunction
                              (synchronous)              │
                                                         ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  AWS Account                                                             │
│                                                                          │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  VPC  10.10.0.0/16  (no IGW, no NAT GW)                           │  │
│  │                                                                    │  │
│  │  ┌────────────────┐    ┌────────────────┐                         │  │
│  │  │ Private Subnet │    │ Private Subnet │  (one per AZ)           │  │
│  │  │ 10.10.1.0/24   │    │ 10.10.2.0/24   │                         │  │
│  │  │                │    │                │                         │  │
│  │  │  ┌──────────┐  │    │                │                         │  │
│  │  │  │  Lambda  │  │    │                │                         │  │
│  │  │  │  ENI     │  │    │                │                         │  │
│  │  │  └────┬─────┘  │    │                │                         │  │
│  │  └───────┼────────┘    └────────────────┘                         │  │
│  │          │                                                         │  │
│  │          │ HTTPS (443) – VPC endpoint only                        │  │
│  │          ▼                                                         │  │
│  │  ┌───────────────────────┐   ┌─────────────────────────────────┐  │  │
│  │  │ VPC Endpoint          │   │ VPC Endpoint                    │  │  │
│  │  │ com.amazonaws.*.logs  │   │ com.amazonaws.*.lambda          │  │  │
│  │  └───────────────────────┘   └─────────────────────────────────┘  │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐   │
│  │  Lambda Function  code-execution-{env}-sandbox                   │   │
│  │                                                                   │   │
│  │  Runtime  : Python 3.12                                          │   │
│  │  Memory   : 512 MB                                               │   │
│  │  Timeout  : 35 s  (code timeout ≤ 30 s)                         │   │
│  │  Package  : zip (handler.py)                                     │   │
│  │                                                                   │   │
│  │  Execution model:                                                 │   │
│  │    1. Receive event {code, timeout, stdin}                        │   │
│  │    2. Clean /tmp from previous warm invocation                    │   │
│  │    3. Write code to /tmp/<uuid>.py                                │   │
│  │    4. spawn python3 -I -u /tmp/<uuid>.py                         │   │
│  │       with RLIMIT_AS=256MB, RLIMIT_FSIZE=10MB, RLIMIT_NOFILE=32  │   │
│  │    5. Capture stdout/stderr, enforce timeout                      │   │
│  │    6. Return {stdout, stderr, exit_code, execution_time_ms}       │   │
│  └───────────────────────────────────────────────────────────────────┘   │
│                                                                          │
│  ┌────────────────────────────────────┐                                  │
│  │  CloudWatch Log Group              │                                  │
│  │  /aws/lambda/code-execution-*      │                                  │
│  │  Retention: 14 days                │                                  │
│  └────────────────────────────────────┘                                  │
│                                                                          │
│  IAM                                                                     │
│  ├── lambda-exec role  (AWSLambdaBasicExecutionRole +                    │
│  │                      AWSLambdaVPCAccessExecutionRole +                │
│  │                      deny-all AWS API policy)                         │
│  └── mcp-invoke policy (lambda:InvokeFunction on sandbox ARN only)       │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Component Deep-Dive

### 2.1 MCP Server (`src/server.py`)

| Aspect | Detail |
|--------|--------|
| Transport | stdio (JSON-RPC 2.0 over stdin/stdout) |
| Concurrency | Single asyncio event loop; Lambda calls dispatched to thread pool via `run_in_executor` to avoid blocking |
| Tool | `execute_python` — forwards code + timeout to Lambda, formats response |
| Auth to AWS | IAM credentials from environment / instance profile / ECS task role |
| Retry | None (intentional — callers should control retry policy) |

### 2.2 Lambda Handler (`lambda/handler.py`)

| Aspect | Detail |
|--------|--------|
| Isolation | Separate OS process per invocation |
| Python flags | `-I` (isolated: ignores `PYTHONPATH`, `PYTHONSTARTUP`, user site) |
| Env stripping | Only `PATH`, `HOME=/tmp`, `TMPDIR=/tmp` passed to child |
| Resource limits | `RLIMIT_AS` 256 MB, `RLIMIT_FSIZE` 10 MB, `RLIMIT_NOFILE` 32 |
| Timeout | `subprocess.run(timeout=N)` → `SIGKILL`; exit code 124 |
| /tmp cleanup | Performed at the start of every invocation (warm container hygiene) |
| Packages | Only stdlib — no network access means `pip install` at runtime is impossible |

### 2.3 Terraform Infrastructure

| Resource | Reason |
|----------|--------|
| Private VPC (no IGW/NAT) | Network-level egress block — executed code cannot reach the internet |
| Security group (egress 443 → VPC CIDR only) | Layer 2 egress control |
| VPC Interface Endpoints (logs, lambda) | Allow Lambda runtime to reach CloudWatch without internet |
| Lambda execution role | Least-privilege: only log writes + ENI management |
| Deny-all AWS API policy | Defence-in-depth: even if code escapes the subprocess it cannot call AWS APIs |
| mcp-invoke IAM policy | Narrowly scoped to a single Lambda ARN |

---

## 3. Data Flow

```
[1] User asks Claude to run Python code
        │
[2] Claude calls MCP tool execute_python({code, timeout})
        │
[3] MCP server (server.py) calls boto3 lambda.invoke()
        │  Payload: {code, timeout, stdin}
        │  InvocationType: RequestResponse (synchronous)
        │
[4] AWS Lambda control plane routes to a warm/cold container
        │
[5] handler.lambda_handler() runs:
    ├─ Validates input
    ├─ Cleans /tmp
    ├─ Writes code to /tmp/<name>.py
    ├─ Forks: python3 -I -u /tmp/<name>.py
    │         (with resource limits applied via preexec_fn)
    ├─ Waits up to `timeout` seconds
    └─ Returns {stdout, stderr, exit_code, execution_time_ms}
        │
[6] MCP server formats result → TextContent markdown
        │
[7] Claude receives and displays the output
```

**Latency breakdown (warm container, typical):**

| Step | Typical time |
|------|-------------|
| MCP → Lambda invoke (network) | 10–30 ms |
| Lambda handler overhead | 5–10 ms |
| Child process spawn | 30–80 ms |
| Code execution | variable |
| Lambda → MCP response | 10–30 ms |
| **Total overhead** | **~55–150 ms** |

Cold start adds ~400–800 ms (Python 3.12 runtime, 512 MB, VPC ENI attachment).

---

## 4. Security Model

### Threat model

| Threat | Mitigation |
|--------|-----------|
| Malicious code reads Lambda env vars / AWS credentials | `python3 -I` strips env; child env has no `AWS_*` vars |
| Code exfiltrates data over network | VPC has no IGW/NAT; SG blocks all egress except to VPC endpoints |
| Code exhausts memory | `RLIMIT_AS = 256 MB` kills the process |
| Code writes large files to `/tmp` | `RLIMIT_FSIZE = 10 MB` |
| Code opens many file descriptors | `RLIMIT_NOFILE = 32` |
| Code runs forever | `subprocess.run(timeout=N)` → SIGKILL |
| Code calls AWS APIs from within subprocess | Deny-all IAM policy on execution role; no credentials in env |
| Container reuse leaks data between executions | `/tmp` cleaned at start of every invocation |
| MCP server caller has overly broad AWS permissions | `mcp-invoke` policy scoped to a single Lambda ARN |

### What the sandbox does NOT protect against

- CPU-bound DoS within the timeout window (intentional — Lambda billing covers this)
- Kernel exploits targeting the Lambda runtime container
- Filesystem read access to Lambda deployment package (`/var/task`)

For higher assurance, consider gVisor (`runsc`) or Firecracker microVM isolation (see §7).

---

## 5. Observability

The current implementation adds **structured JSON logging** (both MCP server and Lambda), **AWS X-Ray distributed tracing**, and **CloudWatch custom metrics**.

### Log schema

Every Lambda invocation emits a structured JSON log line:

```json
{
  "level": "INFO",
  "timestamp": "2026-03-12T10:23:45.123Z",
  "correlation_id": "req-abc123",
  "language": "python",
  "code_length": 42,
  "timeout_requested": 10,
  "exit_code": 0,
  "execution_time_ms": 87,
  "timed_out": false
}
```

### X-Ray trace map

```
MCP Server (local)
  └── Segment: invoke_lambda
        └── AWS::Lambda::Function  code-execution-sandbox
              └── Subsegment: subprocess_execute
                    └── Annotation: exit_code, exec_ms, timed_out
```

### CloudWatch metrics (custom namespace `CodeExecution`)

| Metric | Unit | Description |
|--------|------|-------------|
| `ExecutionTime` | Milliseconds | Wall-clock time of the subprocess |
| `ExecutionErrors` | Count | Non-zero exit codes |
| `TimeoutCount` | Count | Exit code 124 invocations |
| `CodeLength` | Bytes | Size of submitted code |

### Alarms

| Alarm | Threshold | Action |
|-------|-----------|--------|
| High error rate | > 10% of invocations in 5 min | SNS notification |
| High timeout rate | > 5% in 5 min | SNS notification |
| Lambda duration P99 | > 28 000 ms | SNS notification |
| Lambda throttles | Any in 1 min | SNS notification |

---

## 6. Scalability Analysis

### What scales automatically

| Dimension | Behaviour |
|-----------|-----------|
| **Concurrent executions** | Lambda scales to account concurrency limit (default 1 000 per region). Each MCP tool call gets its own Lambda instance. |
| **Storage** | No persistent storage; /tmp is ephemeral per container. |
| **Network** | VPC endpoints scale with AWS infrastructure. |

### Current bottlenecks

#### 6.1 Synchronous invocation couples MCP server throughput to Lambda duration

The MCP server calls Lambda with `InvocationType=RequestResponse`. While waiting, the asyncio event loop thread is blocked (delegated to `run_in_executor`, so the loop itself is not blocked, but the thread pool has a finite size).

**Impact:** If 10 concurrent users each run a 30-second snippet, 10 threads are held for 30 s each. With the default `ThreadPoolExecutor` size of `min(32, os.cpu_count() + 4)` this saturates quickly.

#### 6.2 Cold starts in VPC are slow

Lambda in a VPC must attach an ENI before the first invocation (and after 15 minutes of idle). This adds 400–800 ms latency.

**Impact:** Interactive sessions feel sluggish after a period of inactivity.

#### 6.3 Account-level Lambda concurrency limit (1 000 by default)

All Lambda functions in the account share the 1 000 concurrent execution limit. An unrelated workload could starve the sandbox.

#### 6.4 Single AWS account / region

All traffic goes to one region. A regional outage takes down the service.

#### 6.5 No request queue

If Lambda throttles (concurrency limit hit), the MCP server gets a `TooManyRequestsException` and the user sees an error. There is no queue to absorb bursts.

#### 6.6 No result persistence

Results are returned synchronously and not stored. There is no way to retrieve results for long-running jobs or replay failed executions.

#### 6.7 No rate limiting at the MCP layer

A single user can flood Lambda invocations; there is no per-caller throttle.

---

## 7. Enterprise-Grade Target Architecture

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Clients (Claude Code, web app, CI pipelines …)                           │
└───────────────────────────────┬────────────────────────────────────────────┘
                                │
                    MCP stdio / HTTPS (API GW)
                                │
┌───────────────────────────────▼────────────────────────────────────────────┐
│  MCP Server Fleet  (ECS Fargate, auto-scaled)                              │
│                                                                            │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  Rate limiter  (per caller ARN, per minute)                        │   │
│  │  Request validator  (max code size, allow-list of imports)         │   │
│  │  Correlation ID injector                                           │   │
│  │  X-Ray SDK (trace propagation)                                     │   │
│  └─────────────────────────┬───────────────────────────────────────────┘   │
│                             │                                              │
│            short jobs       │  long jobs (>5 s)                           │
│            (<5 s)           │                                              │
└─────────────────────────────┼──────────────────────────────────────────────┘
               │              │
  Synchronous  │              │  Asynchronous
  Lambda       │              ▼
  invoke       │   ┌──────────────────────┐     ┌────────────────────────┐
               │   │  SQS FIFO Queue      │────►│  Lambda (worker)       │
               │   │  (per-caller dedupe) │     │  processes job         │
               │   └──────────────────────┘     │  writes result to      │
               │                                │  DynamoDB + S3         │
               │                                └────────────────────────┘
               ▼                                         │
   ┌───────────────────────┐                             │
   │  Lambda (sync)         │                            ▼
   │  Reserved concurrency  │              ┌─────────────────────────────┐
   │  Provisioned concurrency│             │  DynamoDB                   │
   │  (eliminate cold start) │             │  job_id → {status, result}  │
   └───────────────────────┘              │  TTL: 24 h                   │
                                          └─────────────────────────────┘
                                                        │
                                          ┌─────────────▼───────────────┐
                                          │  S3  (output > 6 MB)        │
                                          │  Presigned URL returned      │
                                          └─────────────────────────────┘

Observability plane (all regions)
──────────────────────────────────
  CloudWatch Logs  ──► Log Insights queries
  X-Ray            ──► Service Map + Trace analysis
  CloudWatch Metrics (custom namespace CodeExecution)
    + Dashboards  + Alarms  + SNS → PagerDuty / Slack
  AWS Config       ──► compliance rules (no public Lambda URLs, etc.)
  GuardDuty        ──► threat detection on API calls
  CloudTrail       ──► full API audit log → S3 (immutable)

Multi-region active/active
──────────────────────────
  Route 53 latency-based routing
    us-east-1  (primary)
    eu-west-1  (secondary)
    ap-southeast-1 (tertiary)
  Each region has its own VPC + Lambda fleet
  DynamoDB Global Tables for job state
  S3 Cross-Region Replication for outputs
```

### Enterprise components explained

#### A. Async job API (SQS + DynamoDB)

| Component | Purpose |
|-----------|---------|
| SQS FIFO queue | Absorbs bursts; decouples submission from execution; per-group message deduplication prevents double-execution |
| DynamoDB jobs table | Stores `{job_id, caller_id, status, submitted_at, result, ttl}` |
| S3 output bucket | Stores stdout/stderr for results > 6 MB (Lambda response limit) |
| Polling / WebSocket push | Client polls `GET /jobs/{id}` or receives WebSocket push on completion |

**Job states:** `QUEUED → RUNNING → COMPLETED | FAILED | TIMED_OUT`

#### B. Provisioned Concurrency

Eliminates cold starts for interactive use cases. Keep 5–10 provisioned instances during business hours via Application Auto Scaling (schedule-based) and scale to 0 overnight.

#### C. Reserved Concurrency

Allocate e.g. 200 concurrent executions to the sandbox Lambda so other functions in the account cannot starve it — and so the sandbox cannot consume the full account limit.

#### D. Per-caller rate limiting

Track invocations per `caller_id` (IAM ARN or API key) in DynamoDB or ElastiCache. Return `429 Too Many Requests` before invoking Lambda. Prevents a single user from exhausting Lambda concurrency.

#### E. Input validation / allow-listing

Before executing, statically analyse the submitted code:
- Block `import subprocess`, `import os`, `import ctypes`, `import socket`, etc. via AST inspection
- Enforce maximum code length (e.g. 64 KB)
- Strip shebangs and encoding declarations

#### F. Multi-region active/active

- Route 53 latency-based routing directs users to the nearest region
- DynamoDB Global Tables replicate job state within ~1 s
- S3 Cross-Region Replication copies outputs asynchronously
- Each region is fully self-contained; a regional failure triggers Route 53 health-check failover within 60 s

#### G. Observability stack

| Tool | Purpose |
|------|---------|
| X-Ray | End-to-end distributed traces: MCP server → Lambda → subprocess |
| CloudWatch Logs + Log Insights | Structured JSON log querying; e.g. `stats avg(execution_time_ms) by bin(5m)` |
| CloudWatch Dashboard | Live view of throughput, error rate, p50/p95/p99 latency, timeout rate |
| CloudWatch Alarms → SNS | PagerDuty/Slack alerts for error spikes, throttles, duration outliers |
| CloudTrail | Immutable API audit log to S3 (who invoked Lambda, when, from where) |
| AWS Config | Continuous compliance: no public Lambda URLs, VPC enforced, encryption at rest |
| GuardDuty | Anomaly detection on IAM/API usage patterns |

#### H. Secrets & configuration

| Current | Enterprise |
|---------|-----------|
| Env vars | AWS Systems Manager Parameter Store (encrypted) |
| Hardcoded region | AWS AppConfig (dynamic, hot-reloadable feature flags) |

#### I. Encryption

| Layer | Current | Enterprise |
|-------|---------|-----------|
| Data in transit | HTTPS (TLS 1.2+) | TLS 1.3 enforced via SG/ALB policy |
| Data at rest | Lambda ephemeral storage unencrypted | S3 SSE-KMS; DynamoDB SSE-KMS with CMK |
| Log data | CloudWatch default | CMK-encrypted CloudWatch log group |
| Secrets | Plain env vars | KMS-encrypted SSM Parameter Store / Secrets Manager |

---

## 8. Migration Roadmap

### Phase 1 – Stabilise current design (0–4 weeks)

- [x] Structured JSON logging in Lambda and MCP server
- [x] X-Ray active tracing on Lambda
- [x] Custom CloudWatch metrics
- [x] CloudWatch alarms (error rate, timeout rate, duration P99, throttles)
- [ ] Set Lambda Reserved Concurrency to protect account limit
- [ ] Enable Lambda Insights (memory, init duration)
- [ ] Add CloudTrail trail to S3

### Phase 2 – Remove cold starts & improve resilience (4–8 weeks)

- [ ] Application Auto Scaling for Provisioned Concurrency (business hours schedule)
- [ ] Implement per-caller rate limiting (DynamoDB counter + Lambda authorizer)
- [ ] Add SQS dead-letter queue for failed Lambda invocations
- [ ] Input validation via AST analysis (`ast.parse` before execution)
- [ ] Max code size enforcement (64 KB)

### Phase 3 – Async execution & horizontal scale (8–16 weeks)

- [ ] Add async job submission path (SQS → Lambda worker → DynamoDB → S3)
- [ ] Expose REST API via API Gateway (authentication: IAM SigV4 or Cognito)
- [ ] WebSocket push for job completion notifications (API Gateway WebSocket)
- [ ] Multi-language support via separate Lambda functions per runtime
- [ ] Lambda container image for additional runtimes (Node.js, Ruby, etc.)

### Phase 4 – Enterprise-grade (16–24 weeks)

- [ ] Multi-region deployment (us-east-1 + eu-west-1) with Route 53 failover
- [ ] DynamoDB Global Tables for job state
- [ ] S3 Cross-Region Replication for outputs
- [ ] AWS Config rules for compliance
- [ ] GuardDuty + Security Hub integration
- [ ] Quarterly penetration test of the sandbox escape surface

---

## Appendix – Cost Model (rough estimates, us-east-1, 2026)

| Load | Lambda invocations/month | Avg duration | Estimated cost |
|------|--------------------------|-------------|----------------|
| Dev (single user) | 10 000 | 2 s | ~$0.04 |
| Small team (10 users) | 100 000 | 5 s | ~$1.50 |
| Enterprise (100 users) | 1 000 000 | 5 s | ~$15 |
| High-volume (1 000 users) | 10 000 000 | 5 s | ~$150 + VPC endpoint hours |

VPC Interface Endpoints cost ~$14/month each per AZ (2 endpoints × 2 AZs = ~$56/month fixed).

For high-volume, the VPC endpoint fixed cost dominates at low concurrency; NAT Gateway becomes cost-competitive only above ~5 TB/month egress.
