"""Builds local images from the sibling application repositories with the same Dockerfiles CI uses."""

from __future__ import annotations

import fnmatch
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any, Iterable

from .context import Context
from .paths import IMAGES_DIR, repo_path
from .platform import entries_for
from .state import to_iso, utc_now


class BuildError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise BuildError(f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


def is_git_repo(repo: Path) -> bool:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--git-dir"], capture_output=True).returncode == 0


def source_labels(repo: Path, ref: str | None) -> dict[str, str]:
    labels = {"plx.built-at": to_iso(utc_now()), "plx.source.repo": repo.name}
    if not is_git_repo(repo):
        return labels
    revision = _git(repo, "rev-parse", ref or "HEAD").strip()
    dirty = False if ref else bool(_git(repo, "status", "--porcelain").strip())
    labels.update({
        "org.opencontainers.image.revision": revision,
        "plx.source.ref": ref or "working-tree",
        "plx.source.dirty": "true" if dirty else "false",
    })
    remote = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True)
    if remote.returncode == 0:
        labels["org.opencontainers.image.source"] = remote.stdout.strip()
    return labels


def _ignore_patterns(path: Path) -> list[str]:
    if not path.exists():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def ignored(relative: str, patterns: Iterable[str]) -> bool:
    """Minimal .funcignore matching: "dir/" matches a path segment, otherwise glob on name or path."""
    parts = relative.split("/")
    for pattern in patterns:
        if pattern.endswith("/"):
            name = pattern.rstrip("/")
            if any(fnmatch.fnmatch(part, name) for part in parts[:-1]) or fnmatch.fnmatch(relative, f"{name}/*"):
                return True
        elif fnmatch.fnmatch(parts[-1], pattern) or fnmatch.fnmatch(relative, pattern) or any(fnmatch.fnmatch(part, pattern) for part in parts[:-1]):
            return True
    return False


_JUNK_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", ".pytest_cache"}


def _working_tree_files(repo: Path) -> list[str]:
    if is_git_repo(repo):
        output = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        return [path for path in output.split("\0") if path]
    files = []
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in _JUNK_DIRS]
        for name in names:
            files.append(os.path.relpath(os.path.join(root, name), repo))
    return files


def stage_context(repo: Path, destination: Path, ref: str | None = None, ignore_patterns: list[str] | None = None) -> Path:
    """Copies what CI would check out: the git tree (or a ref) without ignored files."""
    if not repo.exists():
        raise BuildError(f"repository {repo} not found")
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    patterns = ignore_patterns or []
    if ref:
        archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", ref], capture_output=True)
        if archive.returncode != 0:
            raise BuildError(f"git archive {ref} failed: {archive.stderr.decode().strip()}")
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            members = [member for member in tar.getmembers() if not ignored(member.name, patterns)]
            tar.extractall(destination, members=members)
        return destination
    for relative in _working_tree_files(repo):
        source = repo / relative
        if not source.is_file() or ignored(relative, patterns):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return destination


class Builder:
    def __init__(self, ctx: Context, ref: str | None = None, no_cache: bool = False) -> None:
        self.ctx = ctx
        self.ref = ref
        self.no_cache = no_cache

    @property
    def targets(self) -> list[str]:
        return (
            list(self.ctx.platform.images)
            + [function_app["name"] for function_app in self.ctx.platform.function_apps]
            + [site["name"] for site in self.ctx.platform.static_sites]
        )

    def _build(self, context: Path, dockerfile: Path, tag: str, labels: dict[str, str], platform: str | None = None, build_args: dict[str, str] | None = None) -> None:
        self.ctx.container.build(
            context,
            dockerfile,
            tag,
            platform=platform,
            build_args=build_args,
            labels=labels,
            no_cache=self.no_cache,
            cpus=int(self.ctx.config.build_setting("cpus", 4)),
            memory=str(self.ctx.config.build_setting("memory", "8G")),
        )

    def build(self, target: str) -> str:
        platform = self.ctx.platform
        if target in platform.images:
            return self.build_image(target)
        for function_app in platform.function_apps:
            if function_app["name"] == target:
                return self.build_function_app(function_app)
        for site in platform.static_sites:
            if site["name"] == target:
                return self.build_static_site(site)
        raise BuildError(f"unknown build target {target!r}; known: {', '.join(self.targets)}")

    def build_image(self, key: str) -> str:
        image: dict[str, Any] = self.ctx.platform.images[key]
        source = image["source"]
        repo = repo_path(source["repo"])
        if not repo.exists():
            raise BuildError(f"{key}: {repo} not found; clone {source['repo']} next to predixlab-infra, or use `plx pull-prod {key}` to run the production image")
        context = stage_context(repo, self.ctx.home.build / key, self.ref)
        tag = self.ctx.local_image(key)
        self._build(context, context / source.get("dockerfile", "Dockerfile"), tag, source_labels(repo, self.ref))
        return tag

    def build_function_app(self, function_app: dict[str, Any]) -> str:
        source = function_app["source"]
        repo = repo_path(source["repo"])
        patterns = _ignore_patterns(repo / source["ignoreFile"]) if source.get("ignoreFile") else []
        context = stage_context(repo, self.ctx.home.build / function_app["name"], self.ref, patterns)
        plx_dir = context / ".plx"
        plx_dir.mkdir(exist_ok=True)
        shutil.copy2(IMAGES_DIR / "function-app" / "Containerfile", plx_dir / "Containerfile")
        (context / ".dockerignore").write_text(".plx\n.dockerignore\n", encoding="utf-8")
        tag = self.ctx.local_image(function_app["name"])
        local = function_app["local"]
        base_image = local["baseImage"]
        if not self.ctx.container.image_exists(base_image):
            # The builder resolves base images through the local store; pull MCR content single-stream first.
            self.ctx.container.pull(base_image, local.get("platform"))
        self._build(
            context,
            plx_dir / "Containerfile",
            tag,
            source_labels(repo, self.ref),
            platform=local.get("platform"),
            build_args={"BASE_IMAGE": base_image, "REQUIREMENTS": source.get("requirements", "requirements.txt")},
        )
        return tag

    def build_static_site(self, site: dict[str, Any]) -> str:
        source = site["source"]
        repo = repo_path(source["repo"])
        context = stage_context(repo, self.ctx.home.build / site["name"], self.ref)
        plx_dir = context / ".plx"
        plx_dir.mkdir(exist_ok=True)
        for name in ("Containerfile", "rewrite-origins.sh"):
            shutil.copy2(IMAGES_DIR / "static-site" / name, plx_dir / name)
        endpoints = self.ctx.platform.endpoints()
        rewrites = [f"{rule['from']}={endpoints[rule['to']]}" for rule in site["local"].get("originRewrites", [])]
        (plx_dir / "origin-rewrites").write_text("\n".join(rewrites) + "\n", encoding="utf-8")
        build_env = []
        for entry in entries_for(site.get("buildEnv", [])):
            if entry.secret:
                raise BuildError(f"{site['name']}: build variable {entry.name!r} cannot reference a secret")
            value = self.ctx.platform.parameter_value(entry.parameter) if entry.parameter else (entry.value or "")
            build_env.append(f"{entry.name}={value}")
        (plx_dir / "build.env").write_text("\n".join(build_env) + ("\n" if build_env else ""), encoding="utf-8")
        (context / ".dockerignore").write_text("node_modules\n.git\n", encoding="utf-8")
        tag = self.ctx.local_image(site["name"])
        self._build(
            context,
            plx_dir / "Containerfile",
            tag,
            source_labels(repo, self.ref),
            build_args={
                "NODE_IMAGE": str(self.ctx.config.build_setting("nodeImage", "docker.io/library/node:22-bookworm-slim")),
                "OUTPUT_LOCATION": source["outputLocation"],
                "APP_LOCATION": source.get("appLocation", "/").strip("/") or ".",
                "CONFIG_FILE": source["configFile"],
                "SWA_CLI_VERSION": str(self.ctx.config.build_setting("swaCliVersion", "2.0.8")),
            },
        )
        return tag
