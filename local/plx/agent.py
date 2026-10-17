"""Background agent: keeps services running (like Container Apps replicas) and fires job schedules in UTC."""

from __future__ import annotations

import logging
import logging.handlers
import signal
import sys
import threading
import time
from datetime import timedelta
from typing import Any

from . import jobs, services
from .context import Context
from .cron import CronExpression
from .state import State, locked, to_iso, utc_now

TICK_SECONDS = 15
ON_TIME_GRACE = timedelta(seconds=90)
MAX_BACKOFF_SECONDS = 300
# Same retention as the production Log Analytics workspaces.
RETENTION_DAYS = 30


def _logger(ctx: Context) -> logging.Logger:
    logger = logging.getLogger("plx.agent")
    if not logger.handlers:
        ctx.home.logs.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(ctx.home.agent_log, maxBytes=5_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S%z"))
        logger.addHandler(handler)
        if sys.stderr.isatty():
            # Under launchd stderr goes to an unrotated file; the rotating log is enough.
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
            logger.addHandler(console)
        logger.setLevel(logging.INFO)
    return logger


class Agent:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.log = _logger(ctx)
        self.stopping = threading.Event()
        self.failures: dict[str, int] = {}
        self.backoff_until: dict[str, float] = {}
        self.restarts: dict[str, int] = {}
        self.last_prune = 0.0
        self.swept = False

    # -- container runtime ---------------------------------------------------
    def ensure_runtime(self) -> bool:
        if self.ctx.container.system_running():
            return True
        self.log.warning("container system is not running; starting it")
        try:
            self.ctx.container.system_start()
        except Exception as error:  # noqa: BLE001
            self.log.error("could not start container system: %s", error)
            return False
        return self.ctx.container.system_running()

    # -- services --------------------------------------------------------------
    def reconcile(self, state: State) -> None:
        for service in services.ordered():
            key = service.key
            if key in state.stopped_services:
                continue
            if time.time() < self.backoff_until.get(key, 0):
                continue
            try:
                if services.missing_image(self.ctx, service):
                    continue
                running = services.is_running(self.ctx, service)
                healthy = running and service.healthy(self.ctx)
                stale_mongo = healthy and key in ("api", "campaign") and not services.uses_current_mongo(self.ctx, service)
                if stale_mongo:
                    healthy = False
            except Exception:  # noqa: BLE001 - one broken service must not stop the others
                self.log.exception("%s: health check failed", key)
                continue
            if healthy:
                self.failures[key] = 0
                self.restarts[key] = 0
                continue
            self.failures[key] = self.failures.get(key, 0) + 1
            if running and self.failures[key] < service.liveness_failures and not stale_mongo:
                continue
            reason = "MongoDB network address changed" if stale_mongo else ("not running" if not running else f"failed {self.failures[key]} health checks")
            restarted = False
            # Hold the state lock so `plx down` cannot interleave with a restart decision.
            with locked(self.ctx.home.state_file):
                current = State(self.ctx.home.state_file)
                if current.desired != "up" or key in current.stopped_services:
                    continue
                self.log.warning("%s: %s; restarting", key, reason)
                try:
                    restarted = services.start(self.ctx, service, log=self.log.info)
                except Exception:  # noqa: BLE001 - treated as a failed restart
                    self.log.exception("%s: restart raised", key)
            if restarted:
                self.log.info("%s: healthy", key)
                self.failures[key] = 0
            else:
                attempts = self.restarts.get(key, 0) + 1
                self.restarts[key] = attempts
                delay = min(MAX_BACKOFF_SECONDS, 10 * 2 ** attempts)
                self.backoff_until[key] = time.time() + delay
                self.log.error("%s: restart failed; backing off %ss", key, delay)

    # -- schedules -------------------------------------------------------------
    def fire_due_jobs(self) -> None:
        with locked(self.ctx.home.state_file):
            self._fire_due_jobs(State(self.ctx.home.state_file))

    def _fire_due_jobs(self, state: State) -> None:
        now = utc_now()
        catch_up = timedelta(minutes=self.ctx.config.catch_up_minutes)
        for job in self.ctx.platform.jobs:
            name = job["name"]
            cron = CronExpression(job["schedule"]["cronExpression"])
            due = cron.previous_at_or_before(now)
            if due is None:
                continue
            last = state.last_fired(name)
            if last is None:
                # First sight of this job: start counting from now, never replay history.
                state.set_last_fired(name, due)
                state.save()
                continue
            if due <= last:
                continue
            state.set_last_fired(name, due)
            state.save()
            lateness = now - due
            if state.desired != "up":
                self.log.info("%s: schedule %s skipped (environment is down; run `plx up`)", name, to_iso(due))
                continue
            if not self.ctx.config.job_enabled(name):
                self.log.info("%s: schedule %s skipped (disabled in config.json)", name, to_iso(due))
                continue
            if lateness <= ON_TIME_GRACE:
                trigger = "schedule"
            elif lateness <= catch_up:
                trigger = "catch-up"
            else:
                self.log.warning("%s: missed schedule %s (host asleep or agent stopped)", name, to_iso(due))
                continue
            holder = jobs.JobLock(self.ctx, name).holder()
            if holder:
                self.log.warning("%s: previous execution %s still running; skipping %s", name, holder.get("execution"), to_iso(due))
                continue
            execution = jobs.Execution.create(self.ctx, name, trigger, scheduled_for=due)
            jobs.start_detached(self.ctx, execution)
            self.log.info("%s: started %s (%s for %s)", name, execution.id, trigger, to_iso(due))

    def _prepare(self) -> bool:
        """Reloads config and checks the runtime; False skips the rest of the tick."""
        self.ctx.reload()
        if not self.ensure_runtime():
            return False
        if not self.swept:
            for execution_id in jobs.sweep_interrupted(self.ctx):
                self.log.warning("marked interrupted execution %s as failed", execution_id)
            self.swept = True
        return True

    def _reconcile_step(self) -> None:
        state = State(self.ctx.home.state_file)
        if state.desired == "up":
            self.reconcile(state)

    def _prune_step(self) -> None:
        if time.time() - self.last_prune > 3600:
            self.last_prune = time.time()
            self.prune()

    def prune(self) -> None:
        removed = jobs.prune(self.ctx, RETENTION_DAYS)
        if removed:
            self.log.info("pruned %s execution records/logs older than %s days", removed, RETENTION_DAYS)

    # -- main loop -------------------------------------------------------------
    def run(self, once: bool = False) -> None:
        self.ctx.home.ensure()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stopping.set())
        self.log.info("agent started (tick %ss, catch-up %s min)", TICK_SECONDS, self.ctx.config.catch_up_minutes)
        while not self.stopping.is_set():
            for step in (self._prepare, self._reconcile_step, self.fire_due_jobs, self._prune_step):
                try:
                    if step() is False:
                        break
                except Exception:  # noqa: BLE001 - each step is isolated; the agent must keep running
                    self.log.exception("agent step %s failed", getattr(step, "__name__", step))
            if once:
                break
            self.stopping.wait(TICK_SECONDS)
        self.log.info("agent stopped")


def describe_schedule(ctx: Context) -> list[dict[str, Any]]:
    state = State(ctx.home.state_file)
    rows = []
    for job, next_fire in jobs.next_runs(ctx):
        last = state.last_fired(job["name"])
        rows.append({
            "job": job["name"],
            "azureName": job["azureName"],
            "cron": job["schedule"]["cronExpression"],
            "enabled": ctx.config.job_enabled(job["name"]),
            "nextUtc": next_fire,
            "lastFired": last,
        })
    return rows
