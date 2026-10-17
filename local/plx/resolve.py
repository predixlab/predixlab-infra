"""Resolves platform.json env entries into concrete values for the local environment."""

from __future__ import annotations

from typing import Any, Callable

from .config import LocalConfig, SecretStore
from .platform import Platform, entries_for

# Well-known, public Azurite development account (not a secret).
AZURITE_ACCOUNT = "devstoreaccount1"
AZURITE_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="


class Resolver:
    def __init__(self, platform: Platform, config: LocalConfig, secrets: SecretStore, gateway: str, mongo_host: Callable[[], str]) -> None:
        self.platform = platform
        self.config = config
        self.secrets = secrets
        self.gateway = gateway
        self.mongo_host = mongo_host

    @property
    def emulators(self) -> dict[str, Any]:
        return self.platform.local["emulators"]

    def mongodb_url(self) -> str:
        return f"mongodb://{self.mongo_host()}:{self.emulators['mongodb']['port']}"

    def azurite_endpoint(self, service: str) -> str:
        return f"http://{self.gateway}:{self.emulators['azurite']['ports'][service]}/{AZURITE_ACCOUNT}"

    def azurite_connection_string(self) -> str:
        return (
            f"DefaultEndpointsProtocol=http;AccountName={AZURITE_ACCOUNT};AccountKey={AZURITE_KEY};"
            f"BlobEndpoint={self.azurite_endpoint('blob')};"
            f"QueueEndpoint={self.azurite_endpoint('queue')};"
            f"TableEndpoint={self.azurite_endpoint('table')};"
        )

    def secret(self, key: str) -> str:
        explicit = self.secrets.get(key)
        if explicit is not None:
            return explicit
        provider = self.platform.secrets[key].get("local")
        if provider == "mongodb":
            return self.mongodb_url()
        if provider == "azurite":
            return self.azurite_connection_string()
        if provider == "generated":
            raise KeyError(f"secret {key!r} has not been generated yet; run `plx init`")
        return ""

    def env(self, entries: list[dict[str, Any]], workload: str, container: str | None = None) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for entry in entries_for(entries):
            if entry.secret:
                resolved[entry.name] = self.secret(entry.secret)
            elif entry.parameter:
                resolved[entry.name] = self.platform.parameter_value(entry.parameter)
            else:
                resolved[entry.name] = entry.value or ""
        for key, value in self.config.env_overrides(workload, container).items():
            if value is None:
                resolved.pop(key, None)
            else:
                resolved[key] = str(value)
        return resolved

    def secret_names(self, entries: list[dict[str, Any]]) -> set[str]:
        return {entry.name for entry in entries_for(entries) if entry.secret}
