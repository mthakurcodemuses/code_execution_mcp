"""
AWS Lambda handler – sandboxed Python code execution.

Observability
-------------
  Structured JSON logging  (CloudWatch Logs / Log Insights)
  AWS X-Ray tracing        (Service Map, Trace Analysis) – optional SDK
  CloudWatch Embedded Metric Format (custom metrics, no extra API calls)

Security
--------
  AST-based static analysis rejects dangerous code before any subprocess
  is spawned. No human approval flow – all checks are automated.
  See _analyse_code() for the full list of blocked patterns.

Event schema:
  {
    "code":           str,   # required
    "timeout":        int,   # optional, default 10, max MAX_TIMEOUT_SECONDS
    "stdin":          str,   # optional
    "correlation_id": str    # optional – propagated from the MCP server
  }

Response schema:
  {
    "stdout":            str,
    "stderr":            str,
    "exit_code":         int,
    "execution_time_ms": int,
    "correlation_id":    str,
    "blocked":           bool   # true when static analysis rejected the code
  }
"""

import ast
import json
import logging
import os
import resource
import shutil
import subprocess
import tempfile
import time
import uuid

# ---------------------------------------------------------------------------
# X-Ray – optional; works without the SDK (falls back to no-ops)
# ---------------------------------------------------------------------------
try:
    from aws_xray_sdk.core import xray_recorder, patch_all
    patch_all()
    XRAY_ENABLED = True
except ImportError:
    XRAY_ENABLED = False

    class _NoOpSubseg:
        def put_annotation(self, k, v): pass
        def put_metadata(self, k, v, namespace="default"): pass
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass

    class _NoOpRecorder:
        def begin_subsegment(self, name): return _NoOpSubseg()
        def put_annotation(self, k, v): pass
        def put_metadata(self, k, v, namespace="default"): pass

    xray_recorder = _NoOpRecorder()


# ---------------------------------------------------------------------------
# Structured JSON logger
# ---------------------------------------------------------------------------
class _Logger:
    def __init__(self):
        self._inner = logging.getLogger("code_execution")
        self._inner.setLevel(logging.INFO)
        if not self._inner.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(message)s"))
            self._inner.addHandler(h)

    def _emit(self, level: str, event: str, **fields):
        self._inner.info(json.dumps({
            "level": level,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            **fields,
        }))

    def info(self, event: str, **kw):  self._emit("INFO",  event, **kw)
    def warn(self, event: str, **kw):  self._emit("WARN",  event, **kw)
    def error(self, event: str, **kw): self._emit("ERROR", event, **kw)


log = _Logger()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MAX_TIMEOUT_SECONDS = int(os.environ.get("MAX_TIMEOUT_SECONDS", "30"))
MAX_MEMORY_BYTES    = int(os.environ.get("MAX_MEMORY_BYTES", str(256 * 1024 * 1024)))
MAX_CODE_BYTES      = int(os.environ.get("MAX_CODE_BYTES",   str(64 * 1024)))   # 64 KB
ENVIRONMENT         = os.environ.get("ENVIRONMENT", "dev")

# ---------------------------------------------------------------------------
# CloudWatch Embedded Metric Format helpers
# ---------------------------------------------------------------------------
def _metric(name: str, value: float, unit: str):
    """Emit a CloudWatch metric as an EMF JSON log line."""
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "CodeExecution",
                "Dimensions": [["Environment"]],
                "Metrics": [{"Name": name, "Unit": unit}],
            }],
        },
        name: value,
        "Environment": ENVIRONMENT,
    }), flush=True)


# ---------------------------------------------------------------------------
# Static code analysis  (runs BEFORE any subprocess is created)
# ---------------------------------------------------------------------------

# Modules that give direct access to OS-level process control or networking
_BLOCKED_MODULES = frozenset({
    "subprocess", "multiprocessing", "threading", "concurrent",
    "socket", "ssl", "http", "urllib", "urllib2", "urllib3",
    "httplib", "httplib2", "requests", "aiohttp", "httpx",
    "ftplib", "smtplib", "imaplib", "poplib", "telnetlib",
    "ctypes", "cffi", "cython",
    "importlib", "imp",
    "pty", "tty", "termios",
    "signal",
    "mmap",
    "code", "codeop",
    "pdb", "bdb", "trace",
    "pickle", "shelve",    # arbitrary object execution
    "marshal",
    "zipimport", "zipfile",
    "tarfile", "gzip", "bz2", "lzma",  # can be used to smuggle code
    "xml", "xmlrpc",
    "distutils", "setuptools", "pip",
})

# Attribute access patterns that indicate privilege escalation via dunder chains
_BLOCKED_ATTR_PATTERNS = frozenset({
    "__class__", "__bases__", "__subclasses__", "__mro__",
    "__globals__", "__builtins__", "__loader__", "__spec__",
    "__import__", "__reduce__", "__reduce_ex__",
})

# Built-in calls that must not appear anywhere in submitted code
_BLOCKED_BUILTINS = frozenset({
    "exec", "eval", "compile",
    "__import__",
    "breakpoint",
    "open",   # see _check_open_calls for the nuanced rule
})


class _CodeViolation(Exception):
    """Raised when the AST analyser finds a disallowed pattern."""


class _ASTChecker(ast.NodeVisitor):
    def __init__(self):
        self.violations: list[str] = []

    # -- import statements --------------------------------------------------
    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in _BLOCKED_MODULES:
                self.violations.append(
                    f"line {node.lineno}: import of blocked module '{alias.name}'"
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        root = (node.module or "").split(".")[0]
        if root in _BLOCKED_MODULES:
            self.violations.append(
                f"line {node.lineno}: import from blocked module '{node.module}'"
            )
        self.generic_visit(node)

    # -- dangerous built-in calls -------------------------------------------
    def visit_Call(self, node: ast.Call):
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        if func_name in _BLOCKED_BUILTINS:
            if func_name == "open":
                # Allow read-only open() or open() within /tmp
                # Reject write/append/binary-write mode outside /tmp
                self._check_open_call(node)
            else:
                self.violations.append(
                    f"line {node.lineno}: call to blocked built-in '{func_name}'"
                )

        # os.system / os.popen / os.execv* / os.spawn*
        if isinstance(node.func, ast.Attribute):
            if (isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                    and node.func.attr in {
                        "system", "popen", "popen2", "popen3", "popen4",
                        "execv", "execve", "execvp", "execvpe",
                        "spawnl", "spawnle", "spawnlp", "spawnlpe",
                        "spawnv", "spawnve", "spawnvp", "spawnvpe",
                        "fork", "forkpty",
                    }):
                self.violations.append(
                    f"line {node.lineno}: call to blocked os function "
                    f"'os.{node.func.attr}'"
                )

        self.generic_visit(node)

    def _check_open_call(self, node: ast.Call):
        """Block open() with write/append modes."""
        write_modes = {"w", "a", "x", "wb", "ab", "xb", "w+", "a+", "x+",
                       "rb+", "wb+", "ab+"}
        if len(node.args) >= 2:
            mode_arg = node.args[1]
            if isinstance(mode_arg, ast.Constant) and mode_arg.value in write_modes:
                self.violations.append(
                    f"line {node.lineno}: open() with write mode "
                    f"'{mode_arg.value}' is not allowed"
                )
        # Also check keyword `mode=`
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                if kw.value.value in write_modes:
                    self.violations.append(
                        f"line {node.lineno}: open(mode='{kw.value.value}') "
                        "is not allowed"
                    )

    # -- dunder attribute access (class hierarchy traversal) ----------------
    def visit_Attribute(self, node: ast.Attribute):
        if node.attr in _BLOCKED_ATTR_PATTERNS:
            self.violations.append(
                f"line {node.lineno}: access to restricted attribute "
                f"'{node.attr}'"
            )
        self.generic_visit(node)

    # -- wildcard import (from foo import *) --------------------------------
    def visit_ImportFrom_star(self, node: ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                self.violations.append(
                    f"line {node.lineno}: wildcard import not allowed"
                )

    def visit_ImportFrom(self, node: ast.ImportFrom):  # noqa: F811
        root = (node.module or "").split(".")[0]
        if root in _BLOCKED_MODULES:
            self.violations.append(
                f"line {node.lineno}: import from blocked module '{node.module}'"
            )
        for alias in node.names:
            if alias.name == "*":
                self.violations.append(
                    f"line {node.lineno}: wildcard import not allowed"
                )
        self.generic_visit(node)


def _analyse_code(code: str) -> list[str]:
    """
    Parse and statically analyse *code*.

    Returns a list of violation strings.  An empty list means the code
    passed all checks and is safe to execute.
    """
    # Size guard (before parsing)
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return [f"code exceeds maximum allowed size of {MAX_CODE_BYTES} bytes"]

    try:
        tree = ast.parse(code, filename="<submitted>")
    except SyntaxError as exc:
        # Syntax errors are allowed through – the subprocess will report them
        return []

    checker = _ASTChecker()
    checker.visit(tree)
    return checker.violations


# ---------------------------------------------------------------------------
# Resource limits (applied in the forked child before exec)
# ---------------------------------------------------------------------------
def _preexec():
    resource.setrlimit(resource.RLIMIT_AS,    (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))


# ---------------------------------------------------------------------------
# /tmp hygiene
# ---------------------------------------------------------------------------
def _clean_tmp():
    for name in os.listdir("/tmp"):
        full = os.path.join("/tmp", name)
        try:
            if os.path.isfile(full) or os.path.islink(full):
                os.unlink(full)
            elif os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Subprocess execution
# ---------------------------------------------------------------------------
def _run_python(code: str, timeout: int, stdin_data: str, correlation_id: str) -> dict:
    _clean_tmp()

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir="/tmp", delete=False, encoding="utf-8"
    ) as f:
        f.write(code)
        tmp_path = f.name

    stdin_bytes = stdin_data.encode("utf-8") if stdin_data else None

    subseg = xray_recorder.begin_subsegment("subprocess_execute")
    start  = time.monotonic()
    timed_out = False
    result = {}

    try:
        proc = subprocess.run(
            ["python3", "-I", "-u", tmp_path],
            input=stdin_bytes,
            capture_output=True,
            timeout=timeout,
            preexec_fn=_preexec,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/tmp",
                "TMPDIR": "/tmp",
            },
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)
        result = {
            "stdout": proc.stdout.decode("utf-8", errors="replace"),
            "stderr": proc.stderr.decode("utf-8", errors="replace"),
            "exit_code": proc.returncode,
            "execution_time_ms": elapsed_ms,
            "timed_out": False,
            "blocked": False,
            "correlation_id": correlation_id,
        }

    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        timed_out = True
        result = {
            "stdout": "",
            "stderr": f"Execution timed out after {timeout} seconds.",
            "exit_code": 124,
            "execution_time_ms": elapsed_ms,
            "timed_out": True,
            "blocked": False,
            "correlation_id": correlation_id,
        }

    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - start) * 1000)
        result = {
            "stdout": "",
            "stderr": f"Internal error: {exc}",
            "exit_code": -1,
            "execution_time_ms": elapsed_ms,
            "timed_out": False,
            "blocked": False,
            "correlation_id": correlation_id,
        }

    finally:
        subseg.put_annotation("exit_code", result.get("exit_code", -1))
        subseg.put_annotation("execution_time_ms", result.get("execution_time_ms", 0))
        subseg.put_annotation("timed_out", timed_out)
        subseg.put_annotation("correlation_id", correlation_id)
        subseg.close()
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return result


# ---------------------------------------------------------------------------
# Lambda entry-point
# ---------------------------------------------------------------------------
def lambda_handler(event: dict, context) -> dict:
    correlation_id = event.get("correlation_id") or str(uuid.uuid4())
    code       = event.get("code", "")
    timeout    = min(int(event.get("timeout", 10)), MAX_TIMEOUT_SECONDS)
    stdin_data = event.get("stdin", "") or ""

    xray_recorder.put_annotation("correlation_id", correlation_id)
    xray_recorder.put_annotation("code_length", len(code))
    xray_recorder.put_annotation("timeout_requested", timeout)

    # -- empty code ---------------------------------------------------------
    if not code.strip():
        log.warn("empty_code", correlation_id=correlation_id)
        return {
            "stdout": "", "stderr": "No code provided.",
            "exit_code": 1, "execution_time_ms": 0,
            "timed_out": False, "blocked": False,
            "correlation_id": correlation_id,
        }

    # -- static analysis ----------------------------------------------------
    violations = _analyse_code(code)
    if violations:
        log.warn(
            "code_blocked_by_static_analysis",
            correlation_id=correlation_id,
            violation_count=len(violations),
            violations=violations,
        )
        _metric("BlockedSubmissions", 1, "Count")
        return {
            "stdout": "",
            "stderr": (
                "Code rejected by static analysis.\n\n"
                "Violations:\n" + "\n".join(f"  - {v}" for v in violations)
            ),
            "exit_code": 1,
            "execution_time_ms": 0,
            "timed_out": False,
            "blocked": True,
            "correlation_id": correlation_id,
        }

    # -- execute ------------------------------------------------------------
    log.info(
        "execution_start",
        correlation_id=correlation_id,
        code_length=len(code),
        timeout_requested=timeout,
    )

    result = _run_python(code, timeout, stdin_data, correlation_id)

    log.info(
        "execution_complete",
        correlation_id=correlation_id,
        exit_code=result["exit_code"],
        execution_time_ms=result["execution_time_ms"],
        timed_out=result["timed_out"],
        stdout_bytes=len(result["stdout"]),
        stderr_bytes=len(result["stderr"]),
    )

    # -- emit CloudWatch metrics (EMF) --------------------------------------
    _metric("ExecutionTime",  result["execution_time_ms"], "Milliseconds")
    _metric("CodeLength",     len(code),                   "Bytes")
    if result["exit_code"] != 0 and not result["timed_out"]:
        _metric("ExecutionErrors", 1, "Count")
    if result["timed_out"]:
        _metric("TimeoutCount", 1, "Count")

    return result
