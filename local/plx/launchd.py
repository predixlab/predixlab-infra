"""launchd LaunchAgent that runs `plx agent` for the logged-in user."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .context import Context
from .paths import INFRA_ROOT, LOCAL_DIR

LABEL = "com.predixlab.staging.agent"
PLIST = Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _python() -> str:
    # Prefer the unresolved `python3` on PATH so interpreter upgrades keep working (plx is stdlib-only).
    return os.environ.get("PLX_PYTHON") or shutil.which("python3") or sys.executable


def render(ctx: Context) -> dict:
    path_entries = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    return {
        "Label": LABEL,
        "ProgramArguments": [_python(), "-m", "plx", "agent"],
        "WorkingDirectory": str(INFRA_ROOT),
        "EnvironmentVariables": {
            "PYTHONPATH": str(LOCAL_DIR),
            "PLX_HOME": str(ctx.home.root),
            "PLX_DATA_DIR": str(ctx.home.data),
            "PATH": ":".join(path_entries),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        # Job executions run in their own sessions and must survive agent restarts.
        "AbandonProcessGroup": True,
        "ProcessType": "Background",
        "StandardOutPath": str(ctx.home.logs / "agent.stdout.log"),
        "StandardErrorPath": str(ctx.home.logs / "agent.stderr.log"),
    }


def _bootout() -> None:
    if not loaded():
        return
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False, capture_output=True)
    # bootout returns before the job is fully removed; bootstrapping too early fails with EIO.
    deadline = time.time() + 20
    while loaded() and time.time() < deadline:
        time.sleep(0.5)


def install(ctx: Context) -> None:
    ctx.home.ensure()
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    _bootout()
    with PLIST.open("wb") as handle:
        plistlib.dump(render(ctx), handle)
    for attempt in range(5):
        result = subprocess.run(["launchctl", "bootstrap", _domain(), str(PLIST)], capture_output=True, text=True)
        if result.returncode == 0 or loaded():
            return
        time.sleep(1 + attempt)
    raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()}")


def uninstall() -> None:
    _bootout()
    PLIST.unlink(missing_ok=True)


def loaded() -> bool:
    return subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True).returncode == 0


def status() -> str:
    if not PLIST.exists():
        return "not installed"
    result = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    if result.returncode != 0:
        return f"installed at {PLIST} but not loaded"
    fields = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        for key in ("state", "pid", "last exit code"):
            if line.startswith(f"{key} = "):
                fields[key] = line.split(" = ", 1)[1]
    return ", ".join(f"{key}={value}" for key, value in fields.items()) or "loaded"
