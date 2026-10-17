"""Pre-release smoke checks for the local staging environment."""

from __future__ import annotations

import re
import subprocess
import urllib.error
import urllib.request
from typing import Callable

from . import launchd, services
from .context import Context

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _get(url: str, headers: dict[str, str] | None = None, method: str = "GET") -> tuple[int | None, dict[str, str], str]:
    request = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as error:
        return None, {}, str(error)


class Smoke:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.results: list[tuple[str, str, str]] = []

    def check(self, name: str, function: Callable[[], tuple[str, str]]) -> None:
        try:
            status, detail = function()
        except Exception as error:  # noqa: BLE001
            status, detail = FAIL, f"{type(error).__name__}: {error}"
        self.results.append((status, name, detail))

    def run(self) -> int:
        ctx = self.ctx
        endpoints = ctx.platform.endpoints()
        self.check("mongo: accepts connections", self._mongo)
        self.check("azurite: queues", self._azurite)
        self.check("api: GET /health", lambda: self._status(f"{endpoints['api']}/health", 200))
        self.check("api: GET /api/v1/info", lambda: self._status(f"{endpoints['api']}/api/v1/info", 200))
        self.check("api: CORS allows the dashboard origin", lambda: self._cors(endpoints["dashboard"], True))
        self.check("api: CORS rejects unknown origins", lambda: self._cors("https://evil.example", False))
        self.check("campaign: Functions host running", self._campaign)
        self.check("dashboard: index", lambda: self._status(f"{endpoints['dashboard']}/", 200, 'id="root"'))
        self.check("dashboard: SPA fallback for deep links", lambda: self._status(f"{endpoints['dashboard']}/market/overview", 200, 'id="root"'))
        self.check("dashboard: bundle targets the local API", self._dashboard_bundle)
        self.check("portal: index", lambda: self._status(f"{endpoints['portal']}/", 200))
        self.check("portal: /pricing/", lambda: self._status(f"{endpoints['portal']}/pricing/", 200))
        self.check("portal: unknown routes return 404", lambda: self._status(f"{endpoints['portal']}/__plx_missing__/", 404))
        self.check("data: shared tree", self._data)
        self.check("scheduler: launchd agent", self._scheduler)
        width = max(len(name) for _, name, _ in self.results)
        for status, name, detail in self.results:
            print(f"{status:4}  {name:<{width}}  {detail}")
        failures = sum(1 for status, _, _ in self.results if status == FAIL)
        print(f"\n{len(self.results)} checks, {failures} failed, {sum(1 for s, _, _ in self.results if s == WARN)} warnings")
        return 1 if failures else 0

    def _mongo(self) -> tuple[str, str]:
        if not services.SERVICES["mongo"].healthy(self.ctx):
            return FAIL, "MongoDB is not reachable on localhost"
        port = self.ctx.platform.local["emulators"]["mongodb"]["port"]
        return PASS, f"127.0.0.1:{port} (containers: {self.ctx.resolver.mongodb_url()})"

    @staticmethod
    def _status(url: str, expected: int, contains: str | None = None) -> tuple[str, str]:
        status, _, body = _get(url)
        if status != expected:
            return FAIL, f"HTTP {status} (expected {expected}) {url}"
        if contains and contains not in body:
            return FAIL, f"response does not contain {contains!r}"
        return PASS, f"HTTP {status}"

    def _cors(self, origin: str, allowed: bool) -> tuple[str, str]:
        url = f"{self.ctx.platform.endpoints()['api']}/api/v1/auth/login"
        _, headers, _ = _get(url, {"Origin": origin, "Access-Control-Request-Method": "POST"}, method="OPTIONS")
        granted = headers.get("access-control-allow-origin") == origin
        if granted == allowed:
            return PASS, f"{origin} {'allowed' if allowed else 'rejected'}"
        return FAIL, f"{origin} unexpectedly {'allowed' if granted else 'rejected'}"

    def _azurite(self) -> tuple[str, str]:
        client = services.azurite_client(self.ctx)
        queues = set(client.list_queues())
        missing = [queue for queue in self.ctx.platform.queues if queue not in queues]
        if missing:
            return FAIL, f"missing queues: {', '.join(missing)}"
        depths = {queue: client.queue_length(queue) for queue in self.ctx.platform.queues}
        poison = [queue for queue in queues if queue.endswith("-poison") and client.queue_length(queue)]
        detail = ", ".join(f"{queue}={depth}" for queue, depth in depths.items())
        if poison:
            return WARN, f"{detail}; poison messages in {', '.join(poison)}"
        return PASS, detail

    def _campaign(self) -> tuple[str, str]:
        if not services.SERVICES["campaign"].healthy(self.ctx):
            return FAIL, "host not responding"
        logs = self.ctx.container.logs_text(services.container_name("campaign"))
        functions = sorted(set(re.findall(r"campaign_\w+_queue_trigger", logs)))
        if len(functions) < 2:
            return WARN, f"host up, functions seen in logs: {functions or 'none yet'}"
        return PASS, ", ".join(functions)

    def _dashboard_bundle(self) -> tuple[str, str]:
        result = subprocess.run(
            ["container", "exec", services.container_name("dashboard"), "sh", "-c", "grep -rlF 'https://api.predixlab.com' /site || true"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return FAIL, result.stderr.strip()
        if result.stdout.strip():
            return FAIL, f"production API origin still referenced in {result.stdout.split()[0]}"
        return PASS, f"no production API origin; uses {self.ctx.platform.endpoints()['api']}"

    def _data(self) -> tuple[str, str]:
        data = self.ctx.home.data
        required = ["calendar/us.txt", "instruments/us.txt"]
        missing = [path for path in required if not (data / path).exists()]
        models = list((data / "models").rglob("*.pkl")) if (data / "models").exists() else []
        if missing:
            return WARN, f"missing {', '.join(missing)}; run `plx seed data --from-azure` (or let the collector bootstrap)"
        if not models:
            return WARN, "no production models under models/; the signal job will fail until seeded"
        return PASS, f"{len(models)} model file(s)"

    @staticmethod
    def _scheduler() -> tuple[str, str]:
        state = launchd.status()
        return (PASS if "pid=" in state else WARN), state
