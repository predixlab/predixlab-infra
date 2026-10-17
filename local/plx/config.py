"""Local-only settings: PLX_HOME/config.json overrides and the PLX_HOME/secrets.env store."""

from __future__ import annotations

import json
import os
import secrets as random_secrets
from pathlib import Path
from typing import Any

from .paths import Home
from .platform import Platform

SECRETS_HEADER = """\
# PredixLab local staging secrets, keyed by the secret catalog names in
# config/platform.json (the production values live in Key Vault predixlab-kv).
# Never commit this file. Empty values disable the corresponding integration
# (e.g. no ACS connection string = no email). Use Stripe *test-mode* keys only.
"""


class LocalConfig:
    """Merges platform.json "local" defaults with the optional PLX_HOME/config.json."""

    def __init__(self, platform: Platform, home: Home) -> None:
        self.platform = platform
        self.home = home
        self.overrides: dict[str, Any] = {}
        if home.config_file.exists():
            with home.config_file.open(encoding="utf-8") as handle:
                self.overrides = json.load(handle)

    @property
    def network_name(self) -> str:
        return self.overrides.get("network", {}).get("name") or self.platform.local["network"]["name"]

    @property
    def subnet(self) -> str:
        return os.environ.get("PLX_SUBNET") or self.overrides.get("network", {}).get("subnet") or self.platform.local["network"]["subnet"]

    @property
    def catch_up_minutes(self) -> int:
        return int(self.overrides.get("catchUpMinutes", self.platform.local.get("catchUpMinutes", 0)))

    def job_enabled(self, job: str) -> bool:
        return bool(self.overrides.get("jobs", {}).get(job, {}).get("enabled", True))

    def env_overrides(self, workload: str, container: str | None = None) -> dict[str, str | None]:
        """Per-workload (``api``) or per-container (``signal/predixlab-signals``) env overrides."""
        env = self.overrides.get("env", {})
        merged: dict[str, str | None] = dict(env.get(workload, {}))
        if container:
            merged.update(env.get(f"{workload}/{container}", {}))
        return merged

    def build_setting(self, key: str, default: Any) -> Any:
        return self.overrides.get("build", {}).get(key, default)


class SecretStore:
    """Plain KEY=VALUE file with mode 0600; keys are secret catalog names."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.values: dict[str, str] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                self.values[key.strip()] = value.strip()

    def get(self, key: str) -> str | None:
        value = self.values.get(key)
        return value if value else None

    def initialize(self, platform: Platform) -> list[str]:
        """Adds every catalog key to the file and generates missing 'generated' secrets."""
        created: list[str] = []
        for key, secret in platform.secrets.items():
            if secret.get("local") == "generated" and not self.values.get(key):
                self.values[key] = random_secrets.token_hex(32)
                created.append(key)
            self.values.setdefault(key, "")
        self._write(platform)
        return created

    def _write(self, platform: Platform) -> None:
        lines = [SECRETS_HEADER]
        for key, secret in platform.secrets.items():
            provider = secret.get("local")
            note = {
                "mongodb": "empty = local MongoDB container",
                "azurite": "empty = local Azurite container",
                "generated": "generated locally",
                "optional": "optional",
            }.get(provider, "")
            description = secret.get("description")
            lines.append(f"# {key} ({note}){' - ' + description if description else ''}")
            lines.append(f"{key}={self.values.get(key, '')}")
        extra = sorted(set(self.values) - set(platform.secrets))
        if extra:
            lines.append("# Unknown keys (kept as-is)")
            lines.extend(f"{key}={self.values[key]}" for key in extra)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.chmod(self.path, 0o600)
