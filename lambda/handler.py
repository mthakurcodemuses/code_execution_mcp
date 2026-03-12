"""
AWS Lambda handler – sandboxed Python code execution.

Event schema:
  {
    "code":    str,   # required – Python source to run
    "timeout": int,   # optional, default 10, max MAX_TIMEOUT_SECONDS
    "stdin":   str    # optional – data passed to stdin
  }

Response schema:
  {
    "stdout":            str,
    "stderr":            str,
    "exit_code":         int,
    "execution_time_ms": int
  }
"""

import json
import logging
import os
import resource
import shutil
import subprocess
import tempfile
import time

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MAX_TIMEOUT_SECONDS = int(os.environ.get("MAX_TIMEOUT_SECONDS", "30"))
# 256 MB virtual address space for child process
MAX_MEMORY_BYTES = int(os.environ.get("MAX_MEMORY_BYTES", str(256 * 1024 * 1024)))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _preexec():
    """Tighten resource limits for the child Python process."""
    resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    # Max 10 MB written to disk
    resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))


def _clean_tmp():
    """Remove leftover files from previous warm invocations."""
    for name in os.listdir("/tmp"):
        full = os.path.join("/tmp", name)
        try:
            if os.path.isfile(full) or os.path.islink(full):
                os.unlink(full)
            elif os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
        except OSError:
            pass


def _run_python(code: str, timeout: int, stdin_data: str) -> dict:
    """Write code to /tmp, execute under a fresh subprocess, return output."""
    _clean_tmp()

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir="/tmp", delete=False, encoding="utf-8"
    ) as f:
        f.write(code)
        tmp_path = f.name

    cmd = [
        "python3",
        "-I",   # isolated: ignore PYTHON* env vars, site-packages
        "-u",   # unbuffered output
        tmp_path,
    ]

    stdin_bytes = stdin_data.encode("utf-8") if stdin_data else None

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
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
        return {
            "stdout": proc.stdout.decode("utf-8", errors="replace"),
            "stderr": proc.stderr.decode("utf-8", errors="replace"),
            "exit_code": proc.returncode,
            "execution_time_ms": elapsed_ms,
        }
    except subprocess.TimeoutExpired:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "stdout": "",
            "stderr": f"Execution timed out after {timeout} seconds.",
            "exit_code": 124,
            "execution_time_ms": elapsed_ms,
        }
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return {
            "stdout": "",
            "stderr": f"Internal error: {exc}",
            "exit_code": -1,
            "execution_time_ms": elapsed_ms,
        }
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Lambda entry-point
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context) -> dict:
    code = event.get("code", "")
    timeout = min(int(event.get("timeout", 10)), MAX_TIMEOUT_SECONDS)
    stdin_data = event.get("stdin", "") or ""

    if not code.strip():
        return {
            "stdout": "",
            "stderr": "No code provided.",
            "exit_code": 1,
            "execution_time_ms": 0,
        }

    logger.info("Executing Python code len=%d timeout=%ds", len(code), timeout)
    result = _run_python(code, timeout, stdin_data)
    logger.info(
        "Done exit_code=%d exec_ms=%d",
        result["exit_code"], result["execution_time_ms"],
    )
    return result
