"""Seeds the local predixlab-data share and MongoDB so staging starts from production-like state."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .context import Context

# Directories of the shared data tree the workloads read (see each repository's data contract).
DATA_DIRECTORIES = [
    "calendar",
    "instruments",
    "stock_data",
    "normalized_data",
    "reference_data",
    "snapshots",
    "models",
    "predictions",
    "picks",
]
OPTIONS_DIRECTORY = "options_data"

# Market data only: no users, sessions, referrals, watchlists or delivery records.
DEFAULT_COLLECTIONS = [
    "market_overviews",
    "pivot_picks",
    "alpharank",
    "alpharank_generations",
    "options_anomalies",
    "options_gex_highlights",
    "options_structure_highlights",
]
LARGE_COLLECTIONS = ["options_gex", "options_structure", "picks", "predictions"]
PERSONAL_COLLECTIONS = {
    "users", "user_sessions", "verification_codes", "login_codes", "oauth_tokens", "refresh_tokens",
    "referrals", "referral_clicks", "watchlists", "alerts", "screener_strategies", "campaign_deliveries",
}

PROD_RESOURCE_GROUP = "predixlab"
PROD_STORAGE_ACCOUNT = "predixlabstorage"
PROD_KEY_VAULT = "predixlab-kv"
PROD_MONGO_SECRET = "predixlab-mongodb-connection-string"


class SeedError(RuntimeError):
    pass


def _latest_option_days(source: Path, days: int) -> list[str]:
    root = source / OPTIONS_DIRECTORY
    if days <= 0 or not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if entry.is_dir())[-days:]


def seed_from_directory(ctx: Context, source: Path, options_days: int, dry_run: bool = False) -> list[str]:
    """Copies selected directories from another local data tree."""
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise SeedError(f"{source} is not a directory")
    target = ctx.home.data
    if source == target.resolve():
        raise SeedError(f"{source} is already the mounted data directory; no copy is needed")
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    items = [name for name in DATA_DIRECTORIES if (source / name).exists()]
    items += [f"{OPTIONS_DIRECTORY}/{day}" for day in _latest_option_days(source, options_days)]
    for item in items:
        destination = target / item
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = ["rsync", "-a", "--exclude", ".DS_Store", "--exclude", "*.bak-*"]
        if dry_run:
            command.append("--dry-run")
        command += [f"{source / item}/", f"{destination}/"]
        subprocess.run(command, check=True)
        copied.append(item)
    return copied


def _az(*args: str) -> str:
    if not shutil.which("az"):
        raise SeedError("Azure CLI (az) is required")
    result = subprocess.run(["az", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise SeedError(f"az {' '.join(args[:3])} failed: {result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ''}")
    return result.stdout


def seed_from_azure(ctx: Context, directories: list[str], options_days: int, share: str | None = None) -> list[str]:
    """Downloads (read-only) selected directories of the production Azure Files share."""
    share = share or ctx.platform.data["sharedStorage"]["shareName"]
    client = _AzureFilesReader(PROD_STORAGE_ACCOUNT, share)
    target = ctx.home.data
    target.mkdir(parents=True, exist_ok=True)
    paths = list(directories)
    if options_days > 0:
        sessions = sorted(name for name in client.list_directory(OPTIONS_DIRECTORY)[0] if name[:4].isdigit())
        paths += [f"{OPTIONS_DIRECTORY}/{session}" for session in sessions[-options_days:]]
    downloaded = []
    for path in paths:
        files = client.walk(path)
        print(f"downloading {share}/{path}: {len(files)} file(s)")
        client.download_all(files, target)
        downloaded.append(path)
    return downloaded


class _AzureFilesReader:
    """Read-only Azure Files REST client authorised by a short-lived read/list SAS."""

    def __init__(self, account: str, share: str) -> None:
        self.base = f"https://{account}.file.core.windows.net/{share}"
        key = _az("storage", "account", "keys", "list", "-g", PROD_RESOURCE_GROUP, "-n", account, "--query", "[0].value", "-o", "tsv").strip()
        expiry = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%MZ")
        self.sas = _az(
            "storage", "share", "generate-sas", "--name", share, "--account-name", account, "--account-key", key,
            "--permissions", "rl", "--expiry", expiry, "--https-only", "-o", "tsv",
        ).strip()

    def _url(self, path: str, query: str = "") -> str:
        quoted = urllib.parse.quote(path.strip("/"))
        return f"{self.base}/{quoted}?{query + '&' if query else ''}{self.sas}"

    @staticmethod
    def _retry(action, attempts: int = 5):
        for attempt in range(1, attempts + 1):
            try:
                return action()
            except (urllib.error.URLError, OSError):
                if attempt == attempts:
                    raise
                time.sleep(2 ** attempt)

    def _read(self, url: str) -> bytes:
        def fetch() -> bytes:
            with urllib.request.urlopen(url, timeout=120) as response:
                return response.read()

        return self._retry(fetch)

    def list_directory(self, path: str) -> tuple[list[str], list[str]]:
        directories, files, marker = [], [], ""
        while True:
            query = "restype=directory&comp=list" + (f"&marker={urllib.parse.quote(marker)}" if marker else "")
            root = ElementTree.fromstring(self._read(self._url(path, query)))
            entries = root.find("Entries")
            for entry in entries if entries is not None else []:
                (directories if entry.tag == "Directory" else files).append(entry.findtext("Name") or "")
            marker = root.findtext("NextMarker") or ""
            if not marker:
                return directories, files

    def walk(self, path: str) -> list[str]:
        directories, files = self.list_directory(path)
        result = [f"{path}/{name}" for name in files if name != ".DS_Store"]
        for directory in directories:
            result += self.walk(f"{path}/{directory}")
        return result

    def download(self, path: str, target: Path) -> None:
        destination = target / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".plx-download")

        def fetch() -> None:
            with urllib.request.urlopen(self._url(path), timeout=300) as response, temporary.open("wb") as handle:
                shutil.copyfileobj(response, handle)

        self._retry(fetch)
        temporary.replace(destination)

    def download_all(self, paths: list[str], target: Path, workers: int = 8) -> None:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for future in [pool.submit(self.download, path, target) for path in paths]:
                future.result()


def seed_database_from_azure(ctx: Context, collections: list[str]) -> None:
    """Copies allowlisted market-data collections from production Cosmos DB into local MongoDB."""
    personal = sorted(set(collections) & PERSONAL_COLLECTIONS)
    if personal:
        raise SeedError(f"refusing to copy personal-data collections: {', '.join(personal)}")
    source_uri = _az("keyvault", "secret", "show", "--vault-name", PROD_KEY_VAULT, "--name", PROD_MONGO_SECRET, "--query", "value", "-o", "tsv").strip()
    database = ctx.platform.data["databaseName"]
    image = ctx.platform.local["emulators"]["mongodb"]["image"]
    target_uri = ctx.resolver.mongodb_url()
    script = " && ".join(
        f'mongodump --quiet --uri="$SOURCE_URI" --db={database} --collection={collection} --archive'
        f' | mongorestore --quiet --uri="$TARGET_URI" --archive --drop --nsInclude="{database}.{collection}"'
        f' && echo "copied {collection}"'
        for collection in collections
    )
    env_file = ctx.home.env_files / "seed-db.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(f"SOURCE_URI={source_uri}\nTARGET_URI={target_uri}\n", encoding="utf-8")
    env_file.chmod(0o600)
    try:
        subprocess.run(
            [
                "container", "run", "--rm", "--progress", "none", "--network", ctx.network,
                "--env-file", str(env_file), "--entrypoint", "bash", image, "-c", script,
            ],
            check=True,
        )
    finally:
        env_file.unlink(missing_ok=True)


def describe(ctx: Context) -> dict:
    data = ctx.home.data
    summary = {}
    for name in DATA_DIRECTORIES + [OPTIONS_DIRECTORY]:
        path = data / name
        if path.exists():
            files = sum(1 for item in path.rglob("*") if item.is_file())
            summary[name] = files
    models = sorted(str(path.relative_to(data)) for path in (data / "models").rglob("*.pkl")) if (data / "models").exists() else []
    return {"files": summary, "models": models}


def print_description(ctx: Context) -> None:
    print(json.dumps(describe(ctx), indent=2))
