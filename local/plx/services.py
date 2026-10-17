"""Long-running local services: emulators plus the Container App, Function App and Static Web Apps."""

from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .azurite import AzuriteClient
from .context import Context
from .platform import container_command, cpu_count, memory_arg
from .runtime import ContainerSpec

SERVICE_ORDER = ["mongo", "azurite", "api", "campaign", "dashboard", "portal"]
FUNCTIONS_SCRIPT_ROOT = "/home/site/wwwroot"


def container_name(service: str) -> str:
    return f"plx-{service}"


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_status(url: str, timeout: float = 5.0) -> int | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return None


@dataclass
class Service:
    key: str
    description: str
    depends_on: list[str]
    startup_timeout: int
    liveness_failures: int
    spec: Callable[[Context], ContainerSpec]
    healthy: Callable[[Context], bool]
    after_start: Callable[[Context], None] | None = None

    @property
    def container(self) -> str:
        return container_name(self.key)


def _labels(service: str) -> dict[str, str]:
    return {"plx.managed": "true", "plx.role": "service", "plx.service": service}


# -- emulators ---------------------------------------------------------------
def _mongo_spec(ctx: Context) -> ContainerSpec:
    emulator = ctx.platform.local["emulators"]["mongodb"]
    return ContainerSpec(
        name=container_name("mongo"),
        image=emulator["image"],
        volumes=[("plx-mongo-data", "/data/db")],
        publish=[f"127.0.0.1:{emulator['port']}:27017"],
        network=ctx.network,
        cpus=2,
        memory="2048M",
        labels=_labels("mongo"),
    )


def _mongo_healthy(ctx: Context) -> bool:
    port = ctx.platform.local["emulators"]["mongodb"]["port"]
    return tcp_open("127.0.0.1", port)


def _azurite_spec(ctx: Context) -> ContainerSpec:
    emulator = ctx.platform.local["emulators"]["azurite"]
    gateway = ctx.gateway()
    ports = emulator["ports"]
    return ContainerSpec(
        name=container_name("azurite"),
        image=emulator["image"],
        command=["azurite"],
        args=[
            "--blobHost", "0.0.0.0", "--queueHost", "0.0.0.0", "--tableHost", "0.0.0.0",
            "--location", "/data", "--skipApiVersionCheck", "--loose",
        ],
        volumes=[("plx-azurite-data", "/data")],
        publish=[f"{gateway}:{ports[name]}:{port}" for name, port in (("blob", 10000), ("queue", 10001), ("table", 10002))],
        network=ctx.network,
        cpus=1,
        memory="1024M",
        labels=_labels("azurite"),
    )


def _azurite_healthy(ctx: Context) -> bool:
    ports = ctx.platform.local["emulators"]["azurite"]["ports"]
    gateway = ctx.gateway()
    return tcp_open(gateway, ports["blob"]) and tcp_open(gateway, ports["queue"])


def azurite_client(ctx: Context) -> AzuriteClient:
    return AzuriteClient(ctx.resolver.azurite_endpoint("queue"))


def _azurite_init(ctx: Context) -> None:
    """Creates the same queues as the production storage account."""
    client = azurite_client(ctx)
    for queue in ctx.platform.queues:
        client.ensure_queue(queue)


# -- Container App (API) -----------------------------------------------------
def _api_app(ctx: Context) -> dict:
    return ctx.platform.container_apps[0]


def _api_spec(ctx: Context) -> ContainerSpec:
    app = _api_app(ctx)
    container = app["containers"][0]
    mount = container["volumeMounts"][0]["mountPath"]
    command, args = container_command(container)
    return ContainerSpec(
        name=container_name("api"),
        image=ctx.local_image(container["image"]),
        command=command,
        args=args,
        env=ctx.resolver.env(container["env"], app["name"], container["name"]),
        binds=[(str(ctx.home.data), mount, False)],
        publish=[f"127.0.0.1:{app['local']['hostPort']}:{app['ingress']['targetPort']}"],
        network=ctx.network,
        cpus=cpu_count(container["cpu"]),
        memory=memory_arg(container["memory"]),
        labels={**_labels("api"), "plx.mongo-address": ctx.mongo_host()},
    )


def _api_healthy(ctx: Context) -> bool:
    app = _api_app(ctx)
    return http_status(f"http://127.0.0.1:{app['local']['hostPort']}{app['local']['healthPath']}") == 200


# -- Function App (campaign) -------------------------------------------------
def _function_app(ctx: Context) -> dict:
    return ctx.platform.function_apps[0]


def _campaign_spec(ctx: Context) -> ContainerSpec:
    function_app = _function_app(ctx)
    local = function_app["local"]
    env = ctx.resolver.env(function_app["settings"], function_app["name"])
    env.update({
        "AzureWebJobsScriptRoot": FUNCTIONS_SCRIPT_ROOT,
        "AzureFunctionsJobHost__Logging__Console__IsEnabled": "true",
    })
    image = ctx.local_image(function_app["name"])
    arch = ctx.image_arch(image)
    return ContainerSpec(
        name=container_name("campaign"),
        image=image,
        env=env,
        network=ctx.network,
        cpus=cpu_count(local["cpu"]),
        memory=memory_arg(local["memory"]),
        arch=arch,
        rosetta=arch == "amd64",
        labels={**_labels("campaign"), "plx.mongo-address": ctx.mongo_host()},
    )


def uses_current_mongo(ctx: Context, service: Service) -> bool:
    info = ctx.container.inspect(service.container)
    labels = ((info.get("configuration") or {}).get("labels") or {}) if info else {}
    return labels.get("plx.mongo-address") == ctx.mongo_host()


def _campaign_healthy(ctx: Context) -> bool:
    address = ctx.container.ip_address(container_name("campaign"))
    return bool(address) and http_status(f"http://{address}:80/") == 200


# -- Static Web Apps ---------------------------------------------------------
STATIC_SITE_PORT = 4280


def _site(ctx: Context, name: str) -> dict:
    return next(site for site in ctx.platform.static_sites if site["name"] == name)


def _site_spec(name: str) -> Callable[[Context], ContainerSpec]:
    def spec(ctx: Context) -> ContainerSpec:
        site = _site(ctx, name)
        return ContainerSpec(
            name=container_name(name),
            image=ctx.local_image(name),
            publish=[f"127.0.0.1:{site['local']['hostPort']}:{STATIC_SITE_PORT}"],
            network=ctx.network,
            cpus=1,
            memory="512M",
            labels=_labels(name),
        )

    return spec


def _site_healthy(name: str) -> Callable[[Context], bool]:
    def healthy(ctx: Context) -> bool:
        return http_status(f"http://127.0.0.1:{_site(ctx, name)['local']['hostPort']}/") == 200

    return healthy


SERVICES: dict[str, Service] = {
    "mongo": Service("mongo", "MongoDB 7.0 (Cosmos DB for MongoDB emulation)", [], 120, 3, _mongo_spec, _mongo_healthy),
    "azurite": Service("azurite", "Azurite (storage queues and blobs)", [], 120, 3, _azurite_spec, _azurite_healthy, _azurite_init),
    # Container Apps startup probe: tcp 8000, 240 x 1s; liveness: GET /health every 10s.
    "api": Service("api", "predixlab-api Container App", ["mongo"], 240, 3, _api_spec, _api_healthy),
    "campaign": Service("campaign", "predixlab-campaign Function App (queue triggers)", ["mongo", "azurite"], 300, 3, _campaign_spec, _campaign_healthy),
    "dashboard": Service("dashboard", "predixlab-dashboard Static Web App", [], 120, 3, _site_spec("dashboard"), _site_healthy("dashboard")),
    "portal": Service("portal", "predixlab-portal Static Web App", [], 120, 3, _site_spec("portal"), _site_healthy("portal")),
}


def ordered(keys: list[str] | None = None) -> list[Service]:
    selected = keys or SERVICE_ORDER
    unknown = [key for key in selected if key not in SERVICES]
    if unknown:
        raise KeyError(f"unknown service(s): {', '.join(unknown)}; known: {', '.join(SERVICE_ORDER)}")
    return [SERVICES[key] for key in SERVICE_ORDER if key in selected]


def with_dependencies(keys: list[str] | None) -> list[str] | None:
    if not keys:
        return keys
    selected: set[str] = set()
    pending = list(keys)
    while pending:
        key = pending.pop()
        if key in selected or key not in SERVICES:
            selected.add(key)
            continue
        selected.add(key)
        pending.extend(SERVICES[key].depends_on)
    return [key for key in SERVICE_ORDER if key in selected] + sorted(selected - set(SERVICE_ORDER))


def ensure_volumes(ctx: Context) -> None:
    for volume in ("plx-mongo-data", "plx-azurite-data"):
        if not ctx.container.volume_exists(volume):
            ctx.container.volume_create(volume)


def missing_image(ctx: Context, service: Service) -> str | None:
    image = service.spec(ctx).image
    return None if ctx.container.image_exists(image) else image


def wait_healthy(ctx: Context, service: Service, timeout: int | None = None, log: Callable[[str], None] = print) -> bool:
    deadline = time.time() + (timeout or service.startup_timeout)
    while time.time() < deadline:
        state = ctx.container.state(service.container)
        if state != "running":
            log(f"{service.key}: container is {state or 'missing'}; recent logs:")
            log(ctx.container.logs_text(service.container, tail=30))
            return False
        if service.healthy(ctx):
            return True
        time.sleep(2)
    log(f"{service.key}: not healthy after {timeout or service.startup_timeout}s")
    return False


def start(ctx: Context, service: Service, log: Callable[[str], None] = print) -> bool:
    image = missing_image(ctx, service)
    if image:
        log(f"{service.key}: image {image} not found; run `plx build {service.key}` (or `plx pull`)")
        return False
    for dependency in service.depends_on:
        dependency_service = SERVICES[dependency]
        if not dependency_service.healthy(ctx):
            log(f"{service.key}: waiting for {dependency}")
            if not wait_healthy(ctx, dependency_service, log=log):
                return False
    ctx.container.remove(service.container)
    ctx.container.run_detached(service.spec(ctx), ctx.home.env_files)
    if not wait_healthy(ctx, service, log=log):
        return False
    if service.after_start:
        service.after_start(ctx)
    return True


def is_running(ctx: Context, service: Service) -> bool:
    return ctx.container.state(service.container) == "running"


def stop(ctx: Context, service: Service) -> None:
    ctx.container.stop(service.container)
    ctx.container.remove(service.container)
