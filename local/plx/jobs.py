"""Container Apps job emulation: executions with init containers, replica timeout and history."""

from __future__ import annotations

import json
import os
import secrets as random_secrets
import shutil
import signal
import string
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .context import Context
from .cron import CronExpression
from .paths import LOCAL_DIR
from .platform import container_command, cpu_count, memory_arg
from .runtime import ContainerSpec
from .state import from_iso, to_iso, utc_now, write_json_atomic

TERMINAL = {"Succeeded", "Failed", "Stopped"}


def execution_name(azure_name: str) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return f"{azure_name}-{''.join(random_secrets.choice(alphabet) for _ in range(7))}"


def container_spec(ctx: Context, job: dict[str, Any], container: dict[str, Any], name: str, labels: dict[str, str]) -> ContainerSpec:
    image = ctx.local_image(container["image"])
    arch = ctx.image_arch(image)
    # Every Azure Files volume of a job is the predixlab-data share.
    binds = [(str(ctx.home.data), mount["mountPath"], False) for mount in container.get("volumeMounts", [])]
    command, args = container_command(container)
    return ContainerSpec(
        name=name,
        image=image,
        command=command,
        args=args,
        env=ctx.resolver.env(container["env"], job["name"], container["name"]),
        binds=binds,
        network=ctx.network,
        cpus=cpu_count(container["cpu"]),
        memory=memory_arg(container["memory"]),
        arch=arch,
        rosetta=arch == "amd64",
        labels={"plx.managed": "true", **labels},
    )


class Execution:
    def __init__(self, ctx: Context, data: dict[str, Any]) -> None:
        self.ctx = ctx
        self.data = data

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def path(self) -> Path:
        return self.ctx.home.executions / f"{self.id}.json"

    @property
    def log_path(self) -> Path:
        return Path(self.data["log"])

    @classmethod
    def create(cls, ctx: Context, job_name: str, trigger: str, scheduled_for: datetime | None = None, skip_init: bool = False) -> "Execution":
        job = ctx.platform.job(job_name)
        execution_id = execution_name(job["azureName"])
        log_dir = ctx.home.job_logs / job["name"]
        log_dir.mkdir(parents=True, exist_ok=True)
        execution = cls(ctx, {
            "id": execution_id,
            "job": job["name"],
            "azureName": job["azureName"],
            "trigger": trigger,
            "scheduledFor": to_iso(scheduled_for) if scheduled_for else None,
            "createdAt": to_iso(utc_now()),
            "startedAt": None,
            "finishedAt": None,
            "status": "Pending",
            "reason": None,
            "pid": None,
            "skipInit": skip_init,
            "replicaTimeout": job["replicaTimeout"],
            "containers": [],
            "log": str(log_dir / f"{execution_id}.log"),
        })
        execution.save()
        return execution

    @classmethod
    def load(cls, ctx: Context, execution_id: str) -> "Execution":
        path = ctx.home.executions / f"{execution_id}.json"
        if not path.exists():
            raise KeyError(f"unknown execution {execution_id}")
        with path.open(encoding="utf-8") as handle:
            return cls(ctx, json.load(handle))

    def save(self) -> None:
        write_json_atomic(self.path, self.data)

    def finish(self, status: str, reason: str | None = None) -> None:
        self.data["status"] = status
        self.data["reason"] = reason
        self.data["finishedAt"] = to_iso(utc_now())
        self.save()
        history = self.ctx.home.history_file
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self.data, ensure_ascii=False) + "\n")


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class JobLock:
    """One active execution per job, like a schedule-triggered job with parallelism 1."""

    def __init__(self, ctx: Context, job: str) -> None:
        self.path = ctx.home.locks / f"{job}.lock"

    def holder(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            return None
        return data if _pid_alive(data.get("pid")) else None

    def acquire(self, execution_id: str) -> bool:
        if self.holder():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"execution": execution_id, "pid": os.getpid()}), encoding="utf-8")
        return True

    def release(self) -> None:
        self.path.unlink(missing_ok=True)


def execute(ctx: Context, execution_id: str) -> int:
    """Runs one execution in the current process. Returns a process exit code."""
    execution = Execution.load(ctx, execution_id)
    job = ctx.platform.job(execution.data["job"])
    lock = JobLock(ctx, job["name"])
    if not lock.acquire(execution.id):
        holder = lock.holder() or {}
        execution.finish("Failed", f"previous execution {holder.get('execution')} is still running")
        return 1

    caffeinate = None
    if shutil.which("caffeinate"):
        # Keep the Mac awake for the duration of the execution.
        caffeinate = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])

    stop_requested = threading.Event()
    current: dict[str, str | None] = {"container": None}

    def request_stop(signum: int, _frame: Any) -> None:
        stop_requested.set()
        if current["container"]:
            ctx.container.kill(current["container"])

    previous_handlers = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    execution.data.update({"status": "Running", "pid": os.getpid(), "startedAt": to_iso(utc_now())})
    execution.save()
    deadline = time.time() + int(job["replicaTimeout"])
    log_lock = threading.Lock()
    status, reason = "Succeeded", None

    with execution.log_path.open("a", encoding="utf-8") as log:
        def write(source: str, line: str) -> None:
            with log_lock:
                log.write(f"{to_iso(utc_now())} [{source}] {line}\n")
                log.flush()

        write("plx", f"execution {execution.id} of {job['azureName']} ({execution.data['trigger']}), replicaTimeout={job['replicaTimeout']}s")
        attempts = int(job.get("replicaRetryLimit", 0)) + 1
        for attempt in range(1, attempts + 1):
            if attempt > 1:
                write("plx", f"retrying replica (attempt {attempt}/{attempts})")
            status, reason = _run_replica(ctx, job, execution, deadline, write, stop_requested, current)
            if status == "Succeeded" or stop_requested.is_set() or reason == "timeout":
                break
        write("plx", f"execution finished: {status}{' (' + reason + ')' if reason else ''}")

    for sig, handler in previous_handlers.items():
        signal.signal(sig, handler)
    lock.release()
    if caffeinate:
        caffeinate.terminate()
    execution.finish(status, reason)
    return 0 if status == "Succeeded" else 1


def _run_replica(ctx, job, execution, deadline, write, stop_requested, current) -> tuple[str, str | None]:
    suffix = execution.id.rsplit("-", 1)[-1]
    steps = [] if execution.data.get("skipInit") else [("init", container) for container in job.get("initContainers", [])]
    steps += [("main", container) for container in job["containers"]]
    for kind, container in steps:
        if stop_requested.is_set():
            return "Stopped", "stop requested"
        name = f"plx-job-{job['name']}-{suffix}-{container['name']}"
        try:
            spec = container_spec(ctx, job, container, name, {
                "plx.role": "job",
                "plx.job": job["name"],
                "plx.execution": execution.id,
            })
        except Exception as error:  # noqa: BLE001 - surfaced in the execution record
            write("plx", f"cannot prepare {container['name']}: {error}")
            return "Failed", f"{container['name']}: {error}"
        if not ctx.container.image_exists(spec.image):
            write("plx", f"image {spec.image} not found; run `plx build {container['image']}`")
            return "Failed", f"missing image {spec.image}"
        record = {"name": container["name"], "kind": kind, "image": spec.image, "startedAt": to_iso(utc_now())}
        execution.data["containers"].append(record)
        execution.save()
        write("plx", f"starting {kind} container {container['name']} ({spec.image})")
        current["container"] = name
        exit_code = ctx.container.run_attached(
            spec,
            ctx.home.env_files,
            on_line=lambda line, source=container["name"]: write(source, line),
            deadline=deadline,
        )
        current["container"] = None
        record.update({"exitCode": exit_code, "finishedAt": to_iso(utc_now())})
        execution.save()
        if stop_requested.is_set():
            return "Stopped", "stop requested"
        if exit_code is None:
            write("plx", f"{container['name']} exceeded replicaTimeout and was killed")
            return "Failed", "timeout"
        if exit_code != 0:
            write("plx", f"{kind} container {container['name']} exited with {exit_code}")
            return "Failed", f"{container['name']} exited with {exit_code}"
    return "Succeeded", None


def start_detached(ctx: Context, execution: Execution) -> subprocess.Popen:
    """Spawns `plx job _execute <id>` in its own session so it outlives the caller."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(LOCAL_DIR) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PLX_HOME"] = str(ctx.home.root)
    return subprocess.Popen(
        [sys.executable, "-m", "plx", "job", "_execute", execution.id],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def running_executions(ctx: Context) -> list[Execution]:
    executions = []
    for path in sorted(ctx.home.executions.glob("*.json")):
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("status") not in TERMINAL:
            executions.append(Execution(ctx, data))
    return executions


def sweep_interrupted(ctx: Context) -> list[str]:
    """Marks executions whose supervising process died (e.g. after a reboot) as failed."""
    swept = []
    for execution in running_executions(ctx):
        created = from_iso(execution.data["createdAt"])
        stale_pending = execution.data["status"] == "Pending" and (utc_now() - created).total_seconds() > 300
        if (execution.data["status"] == "Running" and not _pid_alive(execution.data.get("pid"))) or stale_pending:
            for container in ctx.container.list():
                labels = (container.get("configuration") or {}).get("labels") or {}
                if labels.get("plx.execution") == execution.id:
                    ctx.container.remove(container["configuration"]["id"])
            JobLock(ctx, execution.data["job"]).release()
            execution.finish("Failed", "interrupted (supervisor process exited)")
            swept.append(execution.id)
    return swept


def prune(ctx: Context, days: int) -> int:
    """Deletes finished execution records and logs older than ``days``."""
    cutoff = time.time() - days * 86400
    removed = 0
    for path in list(ctx.home.executions.glob("*.json")) + list(ctx.home.job_logs.glob("*/*.log")):
        if path.stat().st_mtime >= cutoff:
            continue
        if path.suffix == ".json":
            with path.open(encoding="utf-8") as handle:
                if json.load(handle).get("status") not in TERMINAL:
                    continue
        path.unlink(missing_ok=True)
        removed += 1
    history_file = ctx.home.history_file
    if history_file.exists():
        kept = []
        with history_file.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                finished = json.loads(line).get("finishedAt")
                if not finished or from_iso(finished).timestamp() >= cutoff:
                    kept.append(line)
        history_file.write_text("".join(kept), encoding="utf-8")
    return removed


def history(ctx: Context, job: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    path = ctx.home.history_file
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                record = json.loads(line)
                if job is None or record["job"] == job:
                    records.append(record)
    return records[-limit:]


def stop_execution(ctx: Context, execution_id: str) -> bool:
    execution = Execution.load(ctx, execution_id)
    pid = execution.data.get("pid")
    if execution.data["status"] in TERMINAL:
        return False
    if _pid_alive(pid):
        os.kill(pid, signal.SIGTERM)
        return True
    for container in ctx.container.list():
        labels = (container.get("configuration") or {}).get("labels") or {}
        if labels.get("plx.execution") == execution_id:
            ctx.container.remove(container["configuration"]["id"])
    execution.finish("Stopped", "stopped while unsupervised")
    return True


def us_market_open(now: datetime | None = None) -> bool:
    """True during NYSE regular hours (holidays ignored)."""
    from zoneinfo import ZoneInfo

    local = (now or utc_now()).astimezone(ZoneInfo("America/New_York"))
    minutes = local.hour * 60 + local.minute
    return local.weekday() < 5 and 9 * 60 + 30 <= minutes < 16 * 60


def collects_market_data(job: dict[str, Any]) -> bool:
    return any(container["image"] == "collector" for container in job.get("initContainers", []) + job["containers"])


def next_runs(ctx: Context, now: datetime | None = None) -> list[tuple[dict[str, Any], datetime]]:
    now = now or utc_now()
    return [(job, CronExpression(job["schedule"]["cronExpression"]).next_after(now)) for job in ctx.platform.jobs]
