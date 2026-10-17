"""Thin wrapper around the Apple `container` CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

CONTAINER_BIN = os.environ.get("PLX_CONTAINER_BIN", "container")


class RuntimeError_(RuntimeError):
    pass


@dataclass
class ContainerSpec:
    name: str
    image: str
    command: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    binds: list[tuple[str, str, bool]] = field(default_factory=list)
    volumes: list[tuple[str, str]] = field(default_factory=list)
    publish: list[str] = field(default_factory=list)
    network: str | None = None
    cpus: int | None = None
    memory: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    arch: str | None = None
    rosetta: bool = False
    workdir: str | None = None
    init: bool = False

    def cli_args(self, env_file: Path | None) -> list[str]:
        args: list[str] = ["--name", self.name, "--progress", "none"]
        if self.network:
            args += ["--network", self.network]
        if self.cpus:
            args += ["--cpus", str(self.cpus)]
        if self.memory:
            args += ["--memory", self.memory]
        if self.arch:
            args += ["--arch", self.arch]
        if self.rosetta:
            args.append("--rosetta")
        if self.init:
            args.append("--init")
        if self.workdir:
            args += ["--workdir", self.workdir]
        for key, value in sorted(self.labels.items()):
            args += ["--label", f"{key}={value}"]
        for source, target, readonly in self.binds:
            args += ["--volume", f"{source}:{target}{':ro' if readonly else ''}"]
        for volume, target in self.volumes:
            args += ["--volume", f"{volume}:{target}"]
        for spec in self.publish:
            args += ["--publish", spec]
        if env_file is not None:
            args += ["--env-file", str(env_file)]
        # Azure semantics: command replaces ENTRYPOINT, args replace CMD.
        if self.command:
            args += ["--entrypoint", self.command[0]]
        args.append(self.image)
        args += self.command[1:] + self.args
        return args


def azure_registry(reference: str) -> bool:
    """MCR and ACR downloads stall or fail with parallel HTTP/2 streams on container 1.2."""
    registry = reference.split("/", 1)[0]
    return registry == "mcr.microsoft.com" or registry.endswith(".azurecr.io")


def write_env_file(directory: Path, name: str, env: dict[str, str]) -> Path:
    """Writes a 0600 env file (values are passed verbatim, one per line)."""
    directory.mkdir(parents=True, exist_ok=True)
    for key, value in env.items():
        if "\n" in value or "\r" in value:
            raise RuntimeError_(f"environment variable {key} contains a newline, which env files cannot carry")
    fd, path = tempfile.mkstemp(prefix=f"{name}.", suffix=".env", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for key, value in env.items():
            handle.write(f"{key}={value}\n")
    os.chmod(path, 0o600)
    return Path(path)


class Container:
    def __init__(self, binary: str = CONTAINER_BIN) -> None:
        self.binary = binary

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def call(self, args: Sequence[str], check: bool = True, timeout: float | None = None, capture: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [self.binary, *args],
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError_(f"container {' '.join(args[:3])} failed: {detail}")
        return result

    def call_json(self, args: Sequence[str]) -> Any:
        result = self.call(args)
        return json.loads(result.stdout or "null")

    # -- system -----------------------------------------------------------
    def version(self) -> str:
        return self.call(["--version"]).stdout.strip()

    def system_running(self) -> bool:
        result = self.call(["system", "status"], check=False, timeout=30)
        return result.returncode == 0 and "running" in result.stdout

    def system_start(self, install_kernel: bool = False) -> None:
        flag = "--enable-kernel-install" if install_kernel else "--disable-kernel-install"
        self.call(["system", "start", flag, "--timeout", "120"], capture=False, timeout=300)

    # -- networks & volumes ----------------------------------------------
    def network(self, name: str) -> dict[str, Any] | None:
        result = self.call(["network", "inspect", name], check=False)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "[]")
        return data[0] if data else None

    def network_create(self, name: str, subnet: str | None) -> None:
        args = ["network", "create", "--label", "plx.managed=true"]
        if subnet:
            args += ["--subnet", subnet]
        self.call([*args, name])

    def network_delete(self, name: str) -> None:
        self.call(["network", "delete", name], check=False)

    def volume_exists(self, name: str) -> bool:
        return self.call(["volume", "inspect", name], check=False).returncode == 0

    def volume_create(self, name: str, size: str | None = None) -> None:
        args = ["volume", "create", "--label", "plx.managed=true"]
        if size:
            args += ["-s", size]
        self.call([*args, name])

    def volume_delete(self, name: str) -> None:
        self.call(["volume", "delete", name], check=False)

    # -- images -----------------------------------------------------------
    def image(self, reference: str) -> dict[str, Any] | None:
        result = self.call(["image", "inspect", reference], check=False)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "[]")
        return data[0] if data else None

    def image_exists(self, reference: str) -> bool:
        return self.image(reference) is not None

    def pull(self, reference: str, platform: str | None = None, single_stream: bool | None = None) -> None:
        args = ["image", "pull", "--progress", "plain"]
        if platform:
            args += ["--platform", platform]
        if single_stream is None:
            single_stream = azure_registry(reference)
        if single_stream:
            # Parallel HTTP/2 streams to mcr.microsoft.com fail with StreamClosed on container 1.2.
            args += ["--max-concurrent-downloads", "1"]
        self.call([*args, reference], capture=False)

    def tag(self, source: str, target: str) -> None:
        self.call(["image", "tag", source, target])

    def build(
        self,
        context: Path,
        dockerfile: Path,
        tag: str,
        platform: str | None = None,
        build_args: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
        no_cache: bool = False,
        cpus: int | None = None,
        memory: str | None = None,
    ) -> None:
        args = ["build", "--progress", "plain", "-f", str(dockerfile), "-t", tag]
        if platform:
            args += ["--platform", platform]
        for key, value in (build_args or {}).items():
            args += ["--build-arg", f"{key}={value}"]
        for key, value in (labels or {}).items():
            args += ["--label", f"{key}={value}"]
        if no_cache:
            args.append("--no-cache")
        if cpus:
            args += ["--cpus", str(cpus)]
        if memory:
            args += ["--memory", memory]
        self.call([*args, str(context)], capture=False)

    # -- containers -------------------------------------------------------
    def inspect(self, name: str) -> dict[str, Any] | None:
        result = self.call(["inspect", name], check=False)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "[]")
        return data[0] if data else None

    def state(self, name: str) -> str | None:
        info = self.inspect(name)
        return info["status"]["state"] if info else None

    def ip_address(self, name: str) -> str | None:
        info = self.inspect(name)
        if not info:
            return None
        for network in info["status"].get("networks") or []:
            address = network.get("ipv4Address")
            if address:
                return address.split("/")[0]
        return None

    def list(self, all_containers: bool = True) -> list[dict[str, Any]]:
        args = ["list", "--format", "json"]
        if all_containers:
            args.insert(1, "--all")
        return self.call_json(args) or []

    def run_detached(self, spec: ContainerSpec, env_dir: Path) -> None:
        env_file = write_env_file(env_dir, spec.name, spec.env) if spec.env else None
        try:
            self.call(["run", "--detach", *spec.cli_args(env_file)])
        finally:
            if env_file:
                env_file.unlink(missing_ok=True)

    def run_attached(
        self,
        spec: ContainerSpec,
        env_dir: Path,
        on_line: Callable[[str], None],
        deadline: float | None = None,
        remove: bool = True,
    ) -> int | None:
        """Runs to completion and passes every stdout/stderr line to ``on_line``.

        Returns the exit code, or None when ``deadline`` (epoch seconds) was reached
        and the container was killed.
        """
        env_file = write_env_file(env_dir, spec.name, spec.env) if spec.env else None
        args = ["run", *(["--rm"] if remove else []), *spec.cli_args(env_file)]
        process = subprocess.Popen(
            [self.binary, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )

        def pump() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                on_line(line.rstrip("\n"))

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        timed_out = False
        try:
            while True:
                try:
                    process.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    if deadline is not None and time.time() > deadline:
                        timed_out = True
                        self.kill(spec.name)
                        try:
                            process.wait(timeout=60)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        break
        finally:
            reader.join(timeout=10)
            if env_file:
                env_file.unlink(missing_ok=True)
            if remove:
                self.remove(spec.name)
        return None if timed_out else process.returncode

    def run_interactive(self, spec: ContainerSpec, env_dir: Path, tty: bool) -> int:
        env_file = write_env_file(env_dir, spec.name, spec.env) if spec.env else None
        flags = ["--interactive", "--tty"] if tty else []
        try:
            return subprocess.call([self.binary, "run", "--rm", *flags, *spec.cli_args(env_file)])
        finally:
            if env_file:
                env_file.unlink(missing_ok=True)

    def stop(self, name: str, seconds: int = 10) -> None:
        self.call(["stop", "--time", str(seconds), name], check=False, timeout=seconds + 60)

    def kill(self, name: str) -> None:
        self.call(["kill", name], check=False, timeout=60)

    def remove(self, name: str) -> None:
        self.call(["delete", "--force", name], check=False, timeout=120)

    def logs(self, name: str, follow: bool = False, tail: int | None = None) -> int:
        args = ["logs"]
        if follow:
            args.append("--follow")
        if tail is not None:
            args += ["-n", str(tail)]
        return subprocess.call([self.binary, *args, name])

    def logs_text(self, name: str, tail: int | None = None) -> str:
        args = ["logs"]
        if tail is not None:
            args += ["-n", str(tail)]
        result = self.call([*args, name], check=False)
        return result.stdout

    def exec(self, name: str, command: Sequence[str], interactive: bool = False) -> int:
        args = ["exec"]
        if interactive:
            args += ["--interactive", "--tty"]
        return subprocess.call([self.binary, *args, name, *command])
