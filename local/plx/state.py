"""Persistent local state: desired service state and the last cron fire time per job."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Exclusive lock shared by the CLI and the agent for read-modify-write of state.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def update(path: Path, change: Callable[["State"], None]) -> "State":
    with locked(path):
        state = State(path)
        change(state)
        state.save()
        return state


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"desired": "down", "lastFired": {}}
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                self.data.update(json.load(handle))

    @property
    def desired(self) -> str:
        return self.data.get("desired", "down")

    @desired.setter
    def desired(self, value: str) -> None:
        self.data["desired"] = value

    @property
    def stopped_services(self) -> set[str]:
        """Services stopped individually with `plx down <service>`; the agent leaves them alone."""
        return set(self.data.get("stoppedServices", []))

    def set_service_stopped(self, service: str, stopped: bool) -> None:
        names = self.stopped_services
        if stopped:
            names.add(service)
        else:
            names.discard(service)
        self.data["stoppedServices"] = sorted(names)

    def last_fired(self, job: str) -> datetime | None:
        value = self.data.get("lastFired", {}).get(job)
        return from_iso(value) if value else None

    def set_last_fired(self, job: str, moment: datetime) -> None:
        self.data.setdefault("lastFired", {})[job] = to_iso(moment)

    def save(self) -> None:
        write_json_atomic(self.path, self.data)
