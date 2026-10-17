"""Command line interface: `./plx <command>` (see `./plx --help`)."""

from __future__ import annotations

import argparse
import json
import os
import platform as host_platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, agent, jobs, launchd, seed, services
from .build import BuildError, Builder
from .context import Context, ContextError
from .paths import repo_path
from .platform import parameter_env_name
from .smoke import Smoke
from . import state as state_store
from .state import State, to_iso


def _print_table(rows: list[list[str]], headers: list[str]) -> None:
    widths = [max(len(str(value)) for value in column) for column in zip(headers, *rows)] if rows else [len(h) for h in headers]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    for row in rows:
        print("  ".join(str(value).ljust(width) for value, width in zip(row, widths)))


def _local_time(moment) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")


# -- setup -------------------------------------------------------------------
def cmd_doctor(ctx: Context, _args) -> int:
    ok = True

    def report(status: bool, message: str, fatal: bool = True) -> None:
        nonlocal ok
        print(f"{'ok  ' if status else ('FAIL' if fatal else 'warn')}  {message}")
        if not status and fatal:
            ok = False

    mac_version = host_platform.mac_ver()[0]
    report(bool(mac_version) and int(mac_version.split(".")[0]) >= 26, f"macOS {mac_version or 'unknown'} (26+ for container networking)")
    report(host_platform.machine() == "arm64", f"Apple silicon ({host_platform.machine()})")
    report(ctx.container.available(), "`container` CLI installed" + (f": {ctx.container.version()}" if ctx.container.available() else " (brew install container)"))
    if ctx.container.available():
        report(ctx.container.system_running(), "container system running (plx init starts it)")
    rosetta = subprocess.run(["/usr/bin/pgrep", "-q", "oahd"]).returncode == 0
    report(rosetta, "Rosetta 2 available (amd64 images: Functions host)", fatal=False)
    for kind, workload in ctx.platform.workloads():
        source = workload.get("source") or {}
        repo = source.get("repo")
        if repo:
            report(repo_path(repo).exists(), f"{kind} {workload['name']}: {repo_path(repo)}")
    for key, image in ctx.platform.images.items():
        path = repo_path(image["source"]["repo"])
        report(path.exists(), f"image {key}: {path}")
    problems = ctx.platform.validate()
    report(not problems, "config/platform.json is consistent" + (": " + "; ".join(problems) if problems else ""))
    report(shutil.which("az") is not None, "Azure CLI (only for seed --from-azure / pull-prod)", fatal=False)
    report(ctx.home.secrets_file.exists(), f"secrets file {ctx.home.secrets_file}", fatal=False)
    return 0 if ok else 1


def cmd_init(ctx: Context, _args) -> int:
    ctx.home.ensure()
    if not ctx.container.system_running():
        print("starting container system ...")
        ctx.container.system_start(install_kernel=True)
    info = ctx.ensure_network()
    print(f"network {ctx.network}: subnet {info['status'].get('ipv4Subnet')}, gateway {info['status'].get('ipv4Gateway')}")
    services.ensure_volumes(ctx)
    generated = ctx.secrets.initialize(ctx.platform)
    print(f"secrets: {ctx.home.secrets_file}" + (f" (generated {', '.join(generated)})" if generated else ""))
    print(f"data share: {ctx.home.data}")
    print("next: ./plx pull && ./plx build && ./plx up")
    return 0


def cmd_pull(ctx: Context, args) -> int:
    """Pulls emulator and base images that are not built from source."""
    references = [
        (ctx.platform.local["emulators"]["mongodb"]["image"], "linux/arm64"),
        (ctx.platform.local["emulators"]["azurite"]["image"], "linux/arm64"),
    ]
    for function_app in ctx.platform.function_apps:
        references.append((function_app["local"]["baseImage"], function_app["local"].get("platform")))
    for reference, image_platform in references:
        if ctx.container.image_exists(reference) and not args.force:
            print(f"present  {reference}")
            continue
        print(f"pulling  {reference} ({image_platform or 'default'})")
        ctx.container.pull(reference, image_platform)
    return 0


def cmd_build(ctx: Context, args) -> int:
    builder = Builder(ctx, ref=args.ref, no_cache=args.no_cache)
    targets = args.targets or builder.targets
    failed = []
    for target in targets:
        print(f"\n==> building {target}" + (f" at {args.ref}" if args.ref else " from the working tree"))
        try:
            tag = builder.build(target)
            print(f"==> {target}: {tag}")
        except Exception as error:  # noqa: BLE001 - keep building the remaining targets
            print(f"==> {target}: FAILED: {error}", file=sys.stderr)
            failed.append(target)
    if failed:
        print(f"\nfailed: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


def cmd_registry_login(ctx: Context, _args) -> int:
    registry = ctx.platform.data["registry"]
    token = subprocess.run(
        ["az", "acr", "login", "--name", registry.split(".")[0], "--expose-token", "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(
        ["container", "registry", "login", "--username", "00000000-0000-0000-0000-000000000000", "--password-stdin", registry],
        input=token, text=True, check=True,
    )
    return 0


def _live_image(ctx: Context, key: str) -> str | None:
    """Image currently deployed in production for an image key (tagPolicy "live")."""
    for kind, workload in ctx.platform.workloads():
        if kind not in ("containerApp", "job"):
            continue
        for container in workload["containers"]:
            if container["image"] != key:
                continue
            command = ["az", "containerapp"] + (["job"] if kind == "job" else []) + [
                "show", "-g", seed.PROD_RESOURCE_GROUP, "-n", workload["azureName"],
                "--query", f"properties.template.containers[?name=='{container['name']}'].image | [0]", "-o", "tsv",
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
    return None


def cmd_pull_prod(ctx: Context, args) -> int:
    """Runs exactly the production image locally (tagged as the local image)."""
    image = ctx.platform.images[args.image]
    reference = f"{ctx.platform.data['registry']}/{image['repository']}:{args.tag or image['defaultTag']}"
    if not args.tag and image["tagPolicy"] == "live":
        reference = _live_image(ctx, args.image) or reference
    print(f"pulling {reference}")
    # Production images are built on linux/amd64 CI runners; they run under Rosetta.
    ctx.container.pull(reference, "linux/amd64")
    ctx.container.tag(reference, ctx.local_image(args.image))
    print(f"tagged as {ctx.local_image(args.image)}")
    return 0


# -- services ----------------------------------------------------------------
def _refresh_mongo_dependents(ctx: Context, selected: list[str] | None) -> list[str]:
    state = State(ctx.home.state_file)
    failed = []
    for service in services.ordered():
        if service.key not in ("api", "campaign") or service.key in (selected or services.SERVICE_ORDER):
            continue
        if service.key in state.stopped_services or not services.is_running(ctx, service) or services.uses_current_mongo(ctx, service):
            continue
        print(f"{service.key:10} restarting (MongoDB network address changed)")
        if not services.start(ctx, service):
            failed.append(service.key)
    return failed


def cmd_up(ctx: Context, args) -> int:
    ctx.home.ensure()
    ctx.ensure_network()
    services.ensure_volumes(ctx)
    if not ctx.home.secrets_file.exists():
        ctx.secrets.initialize(ctx.platform)
    selected = services.with_dependencies(args.services)

    def mark_up(state: State) -> None:
        state.desired = "up"
        for service in services.ordered(selected):
            state.set_service_stopped(service.key, False)

    state_store.update(ctx.home.state_file, mark_up)
    failed = []
    for service in services.ordered(selected):
        if services.is_running(ctx, service) and service.healthy(ctx) and not args.recreate and (
            service.key not in ("api", "campaign") or services.uses_current_mongo(ctx, service)
        ):
            print(f"{service.key:10} running")
            continue
        print(f"{service.key:10} starting ({service.description})")
        started = time.time()
        if services.start(ctx, service):
            print(f"{service.key:10} healthy after {time.time() - started:.0f}s")
        else:
            failed.append(service.key)
    if "mongo" in (selected or services.SERVICE_ORDER) and "mongo" not in failed:
        failed.extend(_refresh_mongo_dependents(ctx, selected))
    endpoints = ctx.platform.endpoints()
    print(f"\nAPI {endpoints['api']}  Dashboard {endpoints['dashboard']}  Portal {endpoints['portal']}")
    if failed:
        print(f"failed: {', '.join(failed)} (see `./plx logs <service>`)", file=sys.stderr)
        return 1
    return 0


def cmd_down(ctx: Context, args) -> int:
    def mark_down(state: State) -> None:
        if args.services:
            for service in services.ordered(args.services):
                state.set_service_stopped(service.key, True)
        else:
            state.desired = "down"
            state.data["stoppedServices"] = []

    # The agent re-checks this state under the same lock before any restart.
    state_store.update(ctx.home.state_file, mark_down)
    for service in reversed(services.ordered(args.services)):
        print(f"{service.key:10} stopping")
        services.stop(ctx, service)
    return 0


def cmd_restart(ctx: Context, args) -> int:
    code = 0
    for service in services.ordered(args.services):
        print(f"{service.key:10} restarting")
        if not services.start(ctx, service):
            code = 1
    if code == 0 and "mongo" in (args.services or services.SERVICE_ORDER):
        code = 1 if _refresh_mongo_dependents(ctx, args.services) else 0
    return code


def cmd_status(ctx: Context, _args) -> int:
    rows = []
    for service in services.ordered():
        info = ctx.container.inspect(service.container)
        state = info["status"]["state"] if info else "absent"
        healthy = service.healthy(ctx) if state == "running" else False
        image = (info.get("configuration") or {}).get("image", {}).get("reference", "") if info else ""
        revision = ctx.image_revision(image) if image else ""
        rows.append([service.key, state, "yes" if healthy else "no", ctx.container.ip_address(service.container) or "", revision])
    _print_table(rows, ["SERVICE", "STATE", "HEALTHY", "IP", "REVISION"])
    print()
    cmd_schedule(ctx, None)
    running = jobs.running_executions(ctx)
    if running:
        print()
        _print_table([[e.id, e.data["status"], e.data["trigger"], e.data.get("startedAt") or ""] for e in running], ["EXECUTION", "STATUS", "TRIGGER", "STARTED"])
    print(f"\nscheduler: {launchd.status()}")
    return 0


def cmd_logs(ctx: Context, args) -> int:
    target = args.target
    if target in services.SERVICES:
        return ctx.container.logs(services.container_name(target), follow=args.follow, tail=args.tail)
    job_names = {job["name"] for job in ctx.platform.jobs} | {job["azureName"] for job in ctx.platform.jobs}
    if target in job_names:
        records = jobs.history(ctx, ctx.platform.job(target)["name"], limit=1) or []
        running = [e.data for e in jobs.running_executions(ctx) if e.data["job"] == ctx.platform.job(target)["name"]]
        latest = (running or records or [None])[-1]
        if not latest:
            print(f"no executions of {target} yet")
            return 1
        path = Path(latest["log"])
    else:
        path = Path(jobs.Execution.load(ctx, target).data["log"])
    if not path.exists():
        print(f"log {path} not found")
        return 1
    command = ["tail", "-n", str(args.tail or 200)] + (["-f"] if args.follow else []) + [str(path)]
    return subprocess.call(command)


def cmd_env(ctx: Context, args) -> int:
    for kind, workload in ctx.platform.workloads():
        if workload["name"] != args.workload:
            continue
        if kind == "functionApp":
            groups = [(workload["name"], workload["settings"])]
        elif kind == "staticSite":
            groups = [(workload["name"], workload.get("buildEnv", []))]
        else:
            groups = [(c["name"], c["env"]) for c in workload.get("initContainers", []) + workload["containers"]]
        for container, entries in groups:
            if args.container and container != args.container:
                continue
            print(f"# {workload['name']}/{container}")
            resolved = ctx.resolver.env(entries, workload["name"], container if kind in ("containerApp", "job") else None)
            secret_names = ctx.resolver.secret_names(entries)
            for key, value in resolved.items():
                shown = value if args.show_secrets or key not in secret_names else ("<empty>" if not value else "<secret>")
                print(f"{key}={shown}")
            print()
        return 0
    print(f"unknown workload {args.workload}", file=sys.stderr)
    return 1


def cmd_diff(ctx: Context, _args) -> int:
    """Shows every place where local staging intentionally differs from production."""
    rows = []
    for _, workload in ctx.platform.workloads():
        containers = workload.get("initContainers", []) + workload.get("containers", [])
        groups = [(c["name"], c.get("env", [])) for c in containers] or [(workload["name"], workload.get("settings", []) + workload.get("buildEnv", []))]
        for container, entries in groups:
            for entry in entries:
                parameter = entry.get("parameter")
                if parameter is not None:
                    local_value = ctx.platform.parameter_value(parameter)
                    rows.append([
                        f"{workload['name']}/{container}",
                        entry["name"],
                        f"${parameter_env_name(parameter)}",
                        local_value or "<empty>",
                    ])
                    continue
                values = entry.get("values")
                if values is None:
                    continue
                rows.append([f"{workload['name']}/{container}", entry["name"], values.get("prod", "<unset>"), values.get("local", "<unset>")])
    _print_table(rows, ["WORKLOAD", "VARIABLE", "PROD", "LOCAL"])
    print("\nsecrets: production reads Key Vault predixlab-kv; local resolves them from the emulators or", ctx.home.secrets_file)
    return 0


# -- jobs --------------------------------------------------------------------
def cmd_schedule(ctx: Context, _args) -> int:
    rows = []
    for row in agent.describe_schedule(ctx):
        rows.append([
            row["job"], row["azureName"], row["cron"] + " UTC", "yes" if row["enabled"] else "no",
            to_iso(row["nextUtc"]), _local_time(row["nextUtc"]),
            to_iso(row["lastFired"]) if row["lastFired"] else "-",
        ])
    _print_table(rows, ["JOB", "AZURE NAME", "SCHEDULE", "ENABLED", "NEXT (UTC)", "NEXT (LOCAL)", "LAST FIRED"])
    return 0


def cmd_job_list(ctx: Context, args) -> int:
    return cmd_schedule(ctx, args)


def cmd_job_run(ctx: Context, args) -> int:
    job = ctx.platform.job(args.job)
    if jobs.collects_market_data(job) and not args.skip_init and jobs.us_market_open() and not args.force:
        print(
            f"{job['name']}: the US market is open. The collector would store partial bars for the live session\n"
            "(production runs at 02:30 UTC, after the close). Use --skip-init, or --force to run anyway.",
            file=sys.stderr,
        )
        return 2
    execution = jobs.Execution.create(ctx, job["name"], "manual", skip_init=args.skip_init)
    print(f"execution {execution.id}  log {execution.log_path}")
    if args.detach:
        jobs.start_detached(ctx, execution)
        print(f"running in the background; follow with ./plx logs {execution.id} -f")
        return 0
    tail = subprocess.Popen(["tail", "-n", "+1", "-F", str(execution.log_path)], stderr=subprocess.DEVNULL)
    try:
        code = jobs.execute(ctx, execution.id)
    finally:
        time.sleep(0.5)
        tail.terminate()
    final = jobs.Execution.load(ctx, execution.id).data
    print(f"\n{final['id']}: {final['status']}{' - ' + final['reason'] if final.get('reason') else ''}")
    return code


def cmd_job_execute(ctx: Context, args) -> int:
    return jobs.execute(ctx, args.execution)


def cmd_job_exec(ctx: Context, args) -> int:
    job = ctx.platform.job(args.job)
    containers = job.get("initContainers", []) + job["containers"]
    container = next((c for c in containers if c["name"] == args.container), None) if args.container else job["containers"][0]
    if container is None:
        print(f"unknown container {args.container}; choose from {', '.join(c['name'] for c in containers)}", file=sys.stderr)
        return 1
    command = list(args.command)
    spec = jobs.container_spec(ctx, job, container, f"plx-exec-{job['name']}-{os.getpid()}", {"plx.role": "exec", "plx.job": job["name"]})
    if command:
        spec.command, spec.args = command[:1], command[1:]
    return ctx.container.run_interactive(spec, ctx.home.env_files, tty=sys.stdin.isatty())


def cmd_job_history(ctx: Context, args) -> int:
    job = ctx.platform.job(args.job)["name"] if args.job else None
    rows = []
    for record in jobs.history(ctx, job, args.limit):
        rows.append([record["id"], record["status"], record["trigger"], record.get("scheduledFor") or "-", record.get("startedAt") or "-", record.get("finishedAt") or "-", record.get("reason") or ""])
    _print_table(rows, ["EXECUTION", "STATUS", "TRIGGER", "SCHEDULED", "STARTED", "FINISHED", "REASON"])
    return 0


def cmd_job_stop(ctx: Context, args) -> int:
    return 0 if jobs.stop_execution(ctx, args.execution) else 1


# -- agent & scheduler -------------------------------------------------------
def cmd_agent(ctx: Context, args) -> int:
    agent.Agent(ctx).run(once=args.once)
    return 0


def cmd_scheduler(ctx: Context, args) -> int:
    if args.action == "install":
        launchd.install(ctx)
        print(f"installed {launchd.PLIST}; {launchd.status()}")
    elif args.action == "uninstall":
        launchd.uninstall()
        print("uninstalled")
    else:
        print(launchd.status())
    return 0


# -- data --------------------------------------------------------------------
def cmd_seed(ctx: Context, args) -> int:
    ctx.home.ensure()
    if args.what == "data":
        if args.from_azure:
            seed.seed_from_azure(ctx, args.dirs or seed.DATA_DIRECTORIES, args.options_days)
        else:
            if args.source is None:
                raise seed.SeedError("specify --from <directory> or --from-azure to seed raw data")
            copied = seed.seed_from_directory(ctx, Path(args.source), args.options_days, dry_run=args.dry_run)
            print(f"{'would copy' if args.dry_run else 'copied'}: {', '.join(copied)}")
        seed.print_description(ctx)
        return 0
    collections = args.collections or list(seed.DEFAULT_COLLECTIONS)
    if args.include_large:
        collections += seed.LARGE_COLLECTIONS
    if not args.yes:
        print("This reads production Cosmos DB (consumes RUs) and replaces these local collections:")
        print("  " + ", ".join(collections))
        print("Re-run with --yes to continue.")
        return 1
    seed.seed_database_from_azure(ctx, collections)
    return 0


def cmd_queue(ctx: Context, args) -> int:
    client = services.azurite_client(ctx)
    if args.action == "ls":
        _print_table([[queue, str(client.queue_length(queue))] for queue in client.list_queues()], ["QUEUE", "MESSAGES"])
    elif args.action == "peek":
        from .azurite import format_messages

        print(format_messages(client.peek_messages(args.queue)))
    elif args.action == "send":
        text = Path(args.message[1:]).read_text(encoding="utf-8") if args.message.startswith("@") else args.message
        json.loads(text)
        client.send_message(args.queue, text)
        print(f"sent to {args.queue}")
    elif args.action == "clear":
        client.clear_messages(args.queue)
        print(f"cleared {args.queue}")
    return 0


def cmd_mongo(ctx: Context, args) -> int:
    database = ctx.platform.data["databaseName"]
    command = ["mongosh", "--quiet", database, *args.args]
    return ctx.container.exec(services.container_name("mongo"), command, interactive=sys.stdin.isatty())


def cmd_smoke(ctx: Context, _args) -> int:
    return Smoke(ctx).run()


def cmd_destroy(ctx: Context, args) -> int:
    if not args.yes:
        print("Removes all plx containers, the network and the MongoDB/Azurite volumes. The mounted raw data directory is retained. Re-run with --yes.")
        return 1
    if args.purge_data and ctx.home.data.resolve().is_relative_to(ctx.home.root.resolve()):
        raise ContextError("the raw data directory is inside PLX_HOME; move it before purging staging state")
    launchd.uninstall()
    for container in ctx.container.list():
        labels = (container.get("configuration") or {}).get("labels") or {}
        if labels.get("plx.managed") == "true":
            ctx.container.remove(container["configuration"]["id"])
    for volume in ("plx-mongo-data", "plx-azurite-data"):
        ctx.container.volume_delete(volume)
    ctx.container.network_delete(ctx.network)
    state_store.update(ctx.home.state_file, lambda state: setattr(state, "desired", "down"))
    if args.purge_data:
        shutil.rmtree(ctx.home.root, ignore_errors=True)
        print(f"removed staging state {ctx.home.root}; kept raw data {ctx.home.data}")
    else:
        print(f"kept staging state {ctx.home.root} and raw data {ctx.home.data} (use --purge-data to remove staging state)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plx", description="PredixLab local staging environment on Apple container (mirrors Azure production).")
    parser.add_argument("--version", action="version", version=f"plx {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.set_defaults(handler=handler)
        return command

    add("doctor", cmd_doctor, "Check prerequisites and configuration")
    add("init", cmd_init, "Start the container system, create network, volumes, secrets and data share")
    pull = add("pull", cmd_pull, "Pull emulator and base images (MongoDB, Azurite, Functions host)")
    pull.add_argument("--force", action="store_true")
    build = add("build", cmd_build, "Build local images from the sibling repositories")
    build.add_argument("targets", nargs="*", help="api collector signals options campaign dashboard portal (default: all)")
    build.add_argument("--ref", help="build a git ref (e.g. origin/main) instead of the working tree")
    build.add_argument("--no-cache", action="store_true")
    add("registry-login", cmd_registry_login, "Log in to the production ACR with the Azure CLI (read-only pulls)")
    pull_prod = add("pull-prod", cmd_pull_prod, "Pull a production image from ACR and use it locally")
    pull_prod.add_argument("image", help="image key from config/platform.json, e.g. collector")
    pull_prod.add_argument("--tag")

    up = add("up", cmd_up, "Start services (MongoDB, Azurite, API, campaign, dashboard, portal)")
    up.add_argument("services", nargs="*")
    up.add_argument("--recreate", action="store_true", help="recreate running services (pick up new images/config)")
    down = add("down", cmd_down, "Stop services (data is kept)")
    down.add_argument("services", nargs="*")
    restart = add("restart", cmd_restart, "Recreate services")
    restart.add_argument("services", nargs="+")
    add("status", cmd_status, "Show services, schedules and running executions")
    logs = add("logs", cmd_logs, "Show logs of a service, a job (latest execution) or an execution")
    logs.add_argument("target")
    logs.add_argument("-f", "--follow", action="store_true")
    logs.add_argument("-n", "--tail", type=int)
    env = add("env", cmd_env, "Print the resolved local environment of a workload")
    env.add_argument("workload")
    env.add_argument("--container")
    env.add_argument("--show-secrets", action="store_true")
    add("diff", cmd_diff, "List intentional differences between local staging and production")

    add("schedule", cmd_schedule, "Show job schedules (UTC, like Azure Container Apps jobs)")
    job = add("job", None, "Run and inspect jobs")
    job_sub = job.add_subparsers(dest="job_command", required=True)
    job_list = job_sub.add_parser("list", help="List jobs and next runs")
    job_list.set_defaults(handler=cmd_job_list)
    job_run = job_sub.add_parser("run", help="Start an execution now (like az containerapp job start)")
    job_run.add_argument("job")
    job_run.add_argument("--skip-init", action="store_true", help="skip init containers (local-only convenience)")
    job_run.add_argument("--detach", action="store_true")
    job_run.add_argument("--force", action="store_true", help="run a data collection during US market hours")
    job_run.set_defaults(handler=cmd_job_run)
    job_exec = job_sub.add_parser("exec", help="Run a one-off command in a job container context (same image, env, mounts)")
    job_exec.add_argument("job")
    job_exec.add_argument("--container")
    job_exec.add_argument("command", nargs="*", help="command after `--`, e.g. -- python -m predixlab_signals info")
    job_exec.set_defaults(handler=cmd_job_exec)
    job_history = job_sub.add_parser("history", help="Show past executions")
    job_history.add_argument("job", nargs="?")
    job_history.add_argument("-n", "--limit", type=int, default=20)
    job_history.set_defaults(handler=cmd_job_history)
    job_stop = job_sub.add_parser("stop", help="Stop a running execution")
    job_stop.add_argument("execution")
    job_stop.set_defaults(handler=cmd_job_stop)
    job_execute = job_sub.add_parser("_execute", help=argparse.SUPPRESS)
    job_execute.add_argument("execution")
    job_execute.set_defaults(handler=cmd_job_execute)

    run_agent = add("agent", cmd_agent, "Run the scheduler/reconciler in the foreground")
    run_agent.add_argument("--once", action="store_true")
    scheduler = add("scheduler", cmd_scheduler, "Manage the launchd agent that runs `plx agent`")
    scheduler.add_argument("action", choices=["install", "uninstall", "status"])

    seed_cmd = add("seed", cmd_seed, "Seed the data share or MongoDB from a local copy or production (read-only)")
    seed_cmd.add_argument("what", choices=["data", "db"])
    seed_cmd.add_argument("--from", dest="source", help="another local data tree (data)")
    seed_cmd.add_argument("--from-azure", action="store_true", help="download from the production file share (data)")
    seed_cmd.add_argument("--dirs", nargs="*", help="directories to download with --from-azure")
    seed_cmd.add_argument("--options-days", type=int, default=2, help="latest options_data sessions to copy")
    seed_cmd.add_argument("--dry-run", action="store_true")
    seed_cmd.add_argument("--collections", nargs="*", help="collections to copy (db)")
    seed_cmd.add_argument("--include-large", action="store_true", help="also copy options_gex/options_structure/picks/predictions (db)")
    seed_cmd.add_argument("--yes", action="store_true")

    queue = add("queue", cmd_queue, "Inspect Azurite queues")
    queue.add_argument("action", choices=["ls", "peek", "send", "clear"])
    queue.add_argument("queue", nargs="?")
    queue.add_argument("message", nargs="?", help="JSON text or @file")
    mongo = add("mongo", cmd_mongo, "Open mongosh in the local MongoDB")
    mongo.add_argument("args", nargs=argparse.REMAINDER)
    add("smoke", cmd_smoke, "Run pre-release smoke checks against the running stack")
    destroy = add("destroy", cmd_destroy, "Remove containers, network and volumes")
    destroy.add_argument("--yes", action="store_true")
    destroy.add_argument("--purge-data", action="store_true", help="also delete PLX_HOME")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["mongo"]:
        # Everything after `mongo` belongs to mongosh (e.g. --eval).
        args = parser.parse_args(["mongo"])
        args.args = argv[1:]
    elif "--" in argv:
        # `plx job exec <job> [--container X] -- <command...>`
        split = argv.index("--")
        args = parser.parse_args(argv[:split])
        if hasattr(args, "command") and args.command is not None and args.handler is cmd_job_exec:
            args.command = argv[split + 1:]
    else:
        args = parser.parse_args(argv)
    if args.handler is None:
        parser.parse_args([args.command, "--help"])
        return 2
    ctx = Context()
    try:
        return int(args.handler(ctx, args) or 0)
    except (ContextError, KeyError, BuildError, seed.SeedError) as error:
        print(f"plx: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
