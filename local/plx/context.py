"""Shared runtime context: platform definition, PLX_HOME, local config, secrets and the container CLI."""

from __future__ import annotations

from functools import cached_property

from .config import LocalConfig, SecretStore
from .paths import Home
from .platform import Platform
from .resolve import Resolver
from .runtime import Container

LOCAL_IMAGE_PREFIX = "predixlab-local"


class ContextError(RuntimeError):
    pass


class Context:
    def __init__(self) -> None:
        self.home = Home()
        self.platform = Platform.load()
        self.container = Container()

    def reload(self) -> None:
        """Drops cached config, secrets and resolver so edits apply without restarting the agent."""
        self.platform = Platform.load()
        for name in ("config", "secrets", "resolver"):
            self.__dict__.pop(name, None)

    @cached_property
    def config(self) -> LocalConfig:
        return LocalConfig(self.platform, self.home)

    @cached_property
    def secrets(self) -> SecretStore:
        return SecretStore(self.home.secrets_file)

    @property
    def network(self) -> str:
        return self.config.network_name

    def ensure_network(self) -> dict:
        info = self.container.network(self.network)
        if info is None:
            self.container.network_create(self.network, self.config.subnet)
            info = self.container.network(self.network)
        if info is None:
            raise ContextError(f"could not create network {self.network}")
        return info

    def gateway(self) -> str:
        info = self.container.network(self.network)
        if info is None:
            raise ContextError(f"network {self.network} does not exist; run `plx init`")
        gateway = (info.get("status") or {}).get("ipv4Gateway")
        if not gateway:
            raise ContextError(f"network {self.network} has no IPv4 gateway yet")
        return gateway

    def mongo_host(self) -> str:
        address = self.container.ip_address("plx-mongo")
        if not address:
            raise ContextError("MongoDB container has no network address; run `plx up mongo`")
        return address

    @cached_property
    def resolver(self) -> Resolver:
        return Resolver(self.platform, self.config, self.secrets, self.gateway(), self.mongo_host)

    @staticmethod
    def local_image(key: str) -> str:
        return f"{LOCAL_IMAGE_PREFIX}/{key}:latest"

    def image_labels(self, reference: str) -> dict[str, str]:
        info = self.container.image(reference)
        for variant in (info or {}).get("variants") or []:
            labels = ((variant.get("config") or {}).get("config") or {}).get("Labels")
            if labels:
                return labels
        return {}

    def image_revision(self, reference: str) -> str:
        labels = self.image_labels(reference)
        revision = labels.get("org.opencontainers.image.revision", "")[:12]
        if revision and labels.get("plx.source.dirty") == "true":
            revision += "+dirty"
        return revision

    def image_arch(self, reference: str) -> str | None:
        """Returns "amd64" for amd64-only images (run under Rosetta), else None."""
        info = self.container.image(reference)
        if not info:
            return None
        architectures = {
            (variant.get("platform") or {}).get("architecture")
            for variant in info.get("variants") or []
        }
        if "arm64" in architectures or not architectures:
            return None
        return "amd64" if "amd64" in architectures else None
