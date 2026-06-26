"""Spawn an ephemeral Podman container per /runs call.

Network isolation: sandbox attaches to `sandbox-egress` only (no route to
internal services). Resource quotas: 2 CPU, 2 GB RAM, 200 PIDs. Filesystem
isolation: per-call tmp dir on runner-service's volume, bind-mounted at /work.

The sandbox container has no MinIO credentials and is never given any
internal service URL. Upload to MinIO happens here, in runner-service,
AFTER the sandbox has exited.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

from . import script_generator, storage

SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "mcr.microsoft.com/playwright/python:v1.48.0-jammy")
SANDBOX_NETWORK = os.environ.get("SANDBOX_NETWORK", "sandbox-egress")
# Path INSIDE runner-service where per-sandbox tmp dirs live.
SANDBOX_TMP_BASE = Path(os.environ.get("SANDBOX_TMP_BASE", "/sandbox-tmp"))
# Path on the VM HOST that the same dir is bind-mounted from.
# podman run -v sees VM paths, not runner-service container paths.
SANDBOX_TMP_HOST_BASE = os.environ.get("SANDBOX_TMP_HOST_BASE", "/tmp/runner-sandboxes")
CONTAINER_HOST = os.environ.get("CONTAINER_HOST", "unix:///run/podman/podman.sock")

CONTAINER_NAME_PREFIX = "qa-aqa-sandbox-"
GRACE_SECONDS = 10
KILL_GRACE_SECONDS = 5


def reap_orphans_blocking() -> int:
    """Remove any leftover sandbox containers from a prior crash.

    Returns the number removed. Synchronous because it runs at startup
    before the FastAPI event loop is fully up.
    """
    import subprocess

    try:
        r = subprocess.run(
            ["podman", "--remote", "--url", CONTAINER_HOST, "ps", "-a",
             "--filter", f"name={CONTAINER_NAME_PREFIX}",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return 0
        names = [n.strip() for n in r.stdout.splitlines() if n.strip()]
        for n in names:
            subprocess.run(["podman", "--remote", "--url", CONTAINER_HOST, "rm", "-f", n],
                           capture_output=True, timeout=10)
        return len(names)
    except Exception:
        return 0


async def _podman(*args: str, timeout: float = 30.0) -> tuple[int, bytes, bytes]:
    """Run podman as a subprocess. Returns (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "podman", "--remote", "--url", CONTAINER_HOST, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, b"", b"podman call timed out"
    return proc.returncode or 0, out, err


async def run_sandbox(
    test_case: dict,
    target_url: str | None,
    timeout_seconds: int,
    tenant_id: str,
    workflow_id: str,
    test_case_id: str,
    sandbox_id: str,
    bucket: str,
) -> dict:
    """Spawn ephemeral container, run, capture, upload, return execution_result."""
    storage.ensure_bucket(bucket)

    work_dir = SANDBOX_TMP_BASE / sandbox_id
    work_dir.mkdir(parents=True, exist_ok=True)
    script_path = work_dir / "script.py"
    script_path.write_text(script_generator.generate_script(test_case, target_url, timeout_seconds))
    (work_dir / "screenshots").mkdir(exist_ok=True)

    container_name = f"{CONTAINER_NAME_PREFIX}{sandbox_id}"
    started = time.time()
    overall_timeout = timeout_seconds + GRACE_SECONDS

    # The -v path on the LEFT must exist on the VM, not inside runner-service.
    # runner-service sees the dir at work_dir; the sandbox container needs
    # the equivalent VM-side host path (set via SANDBOX_TMP_HOST_BASE).
    host_work_dir = f"{SANDBOX_TMP_HOST_BASE}/{sandbox_id}"
    cmd = [
        "run", "--rm",
        "--name", container_name,
        "--network", SANDBOX_NETWORK,
        "--cpus", "2",
        "--memory", "2g",
        "--pids-limit", "200",
        "--user", "0:0",
        # Disable SELinux container labels — the bind-mounted /work dir is
        # owned by a different user namespace and SELinux blocks access by
        # default. Dev only; production uses a dedicated runner pool.
        "--security-opt", "label=disable",
        "-v", f"{host_work_dir}:/work",
        "-w", "/work",
        SANDBOX_IMAGE,
        "python", "/work/script.py",
    ]

    rc, _stdout, stderr = await _podman(*cmd, timeout=overall_timeout)
    if rc == 124:  # our internal timeout signal
        # ensure container is dead even if podman client timed out before exit
        await _podman("kill", container_name, timeout=KILL_GRACE_SECONDS)
        await _podman("rm", "-f", container_name, timeout=KILL_GRACE_SECONDS)

    # Read result.json if present
    result_file = work_dir / "result.json"
    if result_file.exists():
        try:
            payload = json.loads(result_file.read_text())
        except Exception as e:
            payload = {
                "mode": "playwright_sandbox",
                "status": "error",
                "duration_ms": int((time.time() - started) * 1000),
                "error_message": f"failed to read result.json: {e}",
                "screenshot_relpaths": [],
                "console_log_relpath": "console.log",
            }
    else:
        payload = {
            "mode": "playwright_sandbox",
            "status": "error" if rc != 0 else "fail",
            "duration_ms": int((time.time() - started) * 1000),
            "error_message": (stderr.decode(errors="replace")[-500:] or "no result.json produced"),
            "screenshot_relpaths": [],
            "console_log_relpath": "console.log",
        }

    # Upload artefacts
    key_prefix = f"executions/{tenant_id}/{workflow_id}/{test_case_id}"
    screenshot_urls = []
    for rel in payload.get("screenshot_relpaths", []):
        local = work_dir / "screenshots" / rel
        if local.exists():
            url = storage.upload_file(bucket, f"{key_prefix}/screenshots/{rel}", local, "image/png")
            screenshot_urls.append(url)

    console_path = work_dir / payload.get("console_log_relpath", "console.log")
    console_url = ""
    if console_path.exists():
        console_url = storage.upload_file(bucket, f"{key_prefix}/console.log", console_path, "text/plain")

    # Cleanup workdir
    try:
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception:
        pass

    # Final shape — matches the v1 (D1.1 inline) contract
    return {
        "mode": "playwright_sandbox",
        "status": payload.get("status", "error"),
        "duration_ms": payload.get("duration_ms", int((time.time() - started) * 1000)),
        "screenshots": screenshot_urls,
        "videos": [],
        "console_log_url": console_url,
        "error_message": payload.get("error_message", ""),
    }
