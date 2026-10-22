"""Typed access to config/platform.json, the workload definition shared with Bicep."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .paths import PLATFORM_FILE

ENVIRONMENT = "local"


class PlatformError(ValueError):
    pass


@dataclass(frozen=True)
class EnvEntry:
    name: str
    value: str | None = None
    secret: str | None = None
    parameter: str | None = None


def entries_for(entries: list[dict[str, Any]], environment: str = ENVIRONMENT) -> list[EnvEntry]:
    """Selects the env entries that apply to ``environment`` (same rules as platform-functions.bicep)."""
    selected: list[EnvEntry] = []
    for entry in entries:
        if "secret" in entry:
            selected.append(EnvEntry(entry["name"], secret=entry["secret"]))
        elif "keyVaultReference" in entry:
            selected.append(EnvEntry(entry["name"], secret=entry["keyVaultReference"]))
        elif "value" in entry:
            selected.append(EnvEntry(entry["name"], value=str(entry["value"])))
        elif "parameter" in entry:
            selected.append(EnvEntry(entry["name"], parameter=entry["parameter"]))
        elif environment in entry.get("values", {}):
            selected.append(EnvEntry(entry["name"], value=str(entry["values"][environment])))
    return selected


def parameter_env_name(key: str) -> str:
    return f"PLX_PARAM_{key.upper().replace('-', '_')}"


def container_command(container: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(command, args) for local runs; container.local.command replaces both when present."""
    local_command = (container.get("local") or {}).get("command")
    if local_command:
        return list(local_command), []
    return list(container.get("command", [])), list(container.get("args", []))


def cpu_count(cpu: str | float | int) -> int:
    """Apple container only accepts whole CPUs; fractional Azure sizes round up."""
    return max(1, math.ceil(float(cpu)))


_MEMORY = re.compile(r"^(\d+(?:\.\d+)?)\s*(Ki|Mi|Gi|Ti|K|M|G|T)?$")
_MEMORY_MIB = {None: 1 / (1024 * 1024), "Ki": 1 / 1024, "K": 1 / 1024, "Mi": 1, "M": 1, "Gi": 1024, "G": 1024, "Ti": 1024 * 1024, "T": 1024 * 1024}


def memory_arg(memory: str) -> str:
    """Converts Azure memory sizes (e.g. 1Gi, 1536Mi) to the container CLI format."""
    match = _MEMORY.match(memory.strip())
    if not match:
        raise PlatformError(f"unsupported memory size {memory!r}")
    mebibytes = math.ceil(float(match.group(1)) * _MEMORY_MIB[match.group(2)])
    return f"{mebibytes}M"


class Platform:
    def __init__(self, data: dict[str, Any], path: Path = PLATFORM_FILE) -> None:
        self.data = data
        self.path = path

    @classmethod
    def load(cls, path: Path = PLATFORM_FILE) -> "Platform":
        with path.open(encoding="utf-8") as handle:
            return cls(json.load(handle), path)

    @property
    def images(self) -> dict[str, dict[str, Any]]:
        return self.data["images"]

    @property
    def secrets(self) -> dict[str, dict[str, Any]]:
        return self.data["secrets"]

    @property
    def parameters(self) -> dict[str, dict[str, Any]]:
        return self.data["parameters"]

    def parameter_value(self, key: str, environment: str = ENVIRONMENT) -> str:
        try:
            return str(self.parameters[key][environment])
        except KeyError as error:
            raise PlatformError(f"parameter {key!r} has no value for environment {environment!r}") from error

    @property
    def local(self) -> dict[str, Any]:
        return self.data["local"]

    @property
    def queues(self) -> list[str]:
        return list(self.data["queues"])

    @property
    def mount_path(self) -> str:
        return self.data["sharedStorage"]["mountPath"]

    def endpoints(self, environment: str = ENVIRONMENT) -> dict[str, str]:
        return self.data["endpoints"][environment]

    @property
    def container_apps(self) -> list[dict[str, Any]]:
        return self.data["containerApps"]

    @property
    def jobs(self) -> list[dict[str, Any]]:
        return self.data["jobs"]

    @property
    def function_apps(self) -> list[dict[str, Any]]:
        return self.data["functionApps"]

    @property
    def static_sites(self) -> list[dict[str, Any]]:
        return self.data["staticSites"]

    def job(self, name: str) -> dict[str, Any]:
        for job in self.jobs:
            if name in (job["name"], job["azureName"]):
                return job
        raise PlatformError(f"unknown job {name!r}; known jobs: {', '.join(j['name'] for j in self.jobs)}")

    def workloads(self) -> Iterator[tuple[str, dict[str, Any]]]:
        for app in self.container_apps:
            yield "containerApp", app
        for job in self.jobs:
            yield "job", job
        for function_app in self.function_apps:
            yield "functionApp", function_app
        for site in self.static_sites:
            yield "staticSite", site

    def validate(self) -> list[str]:
        """Returns consistency problems; an empty list means the file is usable."""
        problems: list[str] = []
        for kind, workload in self.workloads():
            containers: list[dict[str, Any]] = []
            if kind in ("containerApp", "job"):
                containers = list(workload.get("initContainers", [])) + list(workload["containers"])
                volume_names = {volume["name"] for volume in workload.get("volumes", [])}
                for container in containers:
                    if container["image"] not in self.images:
                        problems.append(f"{workload['name']}/{container['name']}: unknown image {container['image']!r}")
                    for mount in container.get("volumeMounts", []):
                        if mount["volumeName"] not in volume_names:
                            problems.append(f"{workload['name']}/{container['name']}: unknown volume {mount['volumeName']!r}")
                    try:
                        cpu_count(container["cpu"])
                        memory_arg(container["memory"])
                    except (PlatformError, ValueError) as error:
                        problems.append(f"{workload['name']}/{container['name']}: {error}")
            entries = [entry for container in containers for entry in container.get("env", [])]
            entries += workload.get("settings", []) + workload.get("buildEnv", [])
            for entry in entries:
                keys = {"value", "secret", "keyVaultReference", "parameter", "values"} & set(entry)
                if len(keys) != 1:
                    problems.append(
                        f"{workload['name']}: env {entry.get('name')!r} needs exactly one of "
                        "value/secret/keyVaultReference/parameter/values"
                    )
                for source in ("secret", "keyVaultReference"):
                    if source in entry and entry[source] not in self.secrets:
                        problems.append(f"{workload['name']}: env {entry['name']!r} references unknown secret {entry[source]!r}")
                if "keyVaultReference" in entry and kind != "functionApp":
                    problems.append(f"{workload['name']}: env {entry['name']!r} uses keyVaultReference outside a Function App")
                if "parameter" in entry and entry["parameter"] not in self.parameters:
                    problems.append(
                        f"{workload['name']}: env {entry['name']!r} references unknown parameter {entry['parameter']!r}"
                    )
                for environment in entry.get("values", {}):
                    if environment not in self.data["endpoints"]:
                        problems.append(f"{workload['name']}: env {entry['name']!r} uses unknown environment {environment!r}")
            for secret in workload.get("additionalSecrets", []):
                if secret not in self.secrets:
                    problems.append(f"{workload['name']}: unknown additional secret {secret!r}")
            if kind == "job":
                from .cron import CronError, CronExpression

                try:
                    CronExpression(workload["schedule"]["cronExpression"])
                except CronError as error:
                    problems.append(f"{workload['name']}: {error}")
        for key, secret in self.secrets.items():
            if secret.get("local") not in ("mongodb", "azurite", "generated", "optional"):
                problems.append(f"secret {key!r}: unsupported local provider {secret.get('local')!r}")
        for key, parameter in self.parameters.items():
            if "local" not in parameter:
                problems.append(f"parameter {key!r}: missing local value")
        return problems
