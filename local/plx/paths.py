"""Filesystem layout of the infra repository, the sibling workspace and PLX_HOME."""

from __future__ import annotations

import os
from pathlib import Path

INFRA_ROOT = Path(__file__).resolve().parents[2]
PLATFORM_FILE = INFRA_ROOT / "config" / "platform.json"
LOCAL_DIR = INFRA_ROOT / "local"
IMAGES_DIR = LOCAL_DIR / "images"


def workspace_root() -> Path:
    """Directory that contains the application repositories (siblings of predixlab-infra)."""
    return Path(os.environ.get("PLX_WORKSPACE", str(INFRA_ROOT.parent))).expanduser().resolve()


def repo_path(repo: str) -> Path:
    return workspace_root() / repo


class Home:
    """Local state and shared data directories."""

    def __init__(self, root: Path | None = None, data_root: Path | None = None) -> None:
        self.root = (root or Path(os.environ.get("PLX_HOME", "~/.predixlab-staging"))).expanduser()
        self.data_root = (data_root or Path(os.environ.get("PLX_DATA_DIR", "~/.predixlab"))).expanduser()

    @property
    def data(self) -> Path:
        """Existing raw data tree, mounted at /data in workload containers."""
        return self.data_root

    @property
    def secrets_file(self) -> Path:
        return self.root / "secrets.env"

    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def state_file(self) -> Path:
        return self.root / "state.json"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def job_logs(self) -> Path:
        return self.logs / "jobs"

    @property
    def agent_log(self) -> Path:
        return self.logs / "agent.log"

    @property
    def history_file(self) -> Path:
        return self.root / "history" / "executions.jsonl"

    @property
    def executions(self) -> Path:
        return self.root / "run" / "executions"

    @property
    def env_files(self) -> Path:
        return self.root / "run" / "env"

    @property
    def locks(self) -> Path:
        return self.root / "run" / "locks"

    @property
    def build(self) -> Path:
        return self.root / "build"

    def ensure(self) -> None:
        for path in (
            self.root,
            self.data,
            self.logs,
            self.job_logs,
            self.history_file.parent,
            self.executions,
            self.env_files,
            self.locks,
            self.build,
        ):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.env_files, 0o700)
