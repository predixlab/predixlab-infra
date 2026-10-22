# Local Staging

The local staging environment runs the platform workloads with Apple's `container` runtime. It shares application settings and UTC job schedules with the [Azure deployment](azure.md), while MongoDB, storage, and email delivery use local equivalents.

## Requirements

See the [quick start](../README.md#quick-start) for host and sibling-repository requirements. `PLX_WORKSPACE` overrides the default sibling workspace. Without `predixlab-collector` and `predixlab-options` in that workspace, `./plx doctor` reports `FAIL` for `image collector` and `image options` and exits 1 even if all other checks pass. `./plx build collector options` fails with missing-repository errors and exits 1. A bare `./plx build` attempts every target, continues after failures, and exits 1 with collector and options reported as failed.

Use `./plx build api signals campaign dashboard portal` for the public targets. Each builds from its own repository without depending on the collector/options repositories, and the api, campaign, dashboard and portal services do not use those images.

Building and running staging with only the public repositories has not yet been tested end to end.

The full `signal` job (with collection) needs the collector image, and the `options` job needs the options image. Build these from the collector and options repositories, which aren't public. `./plx job run signal --skip-init` skips the collector init container and runs only the signals container; input data must already be present in `~/.predixlab` (or `PLX_DATA_DIR`), with models under that directory's `models/us/`.

## Components

```text
launchd  com.predixlab.staging.agent  ->  plx agent (every 15 s)
                                          |-- monitors mongo, azurite, api, campaign, dashboard, portal
                                          `-- starts UTC-scheduled jobs in independent processes

network predixlab-staging (default 192.168.207.0/24, gateway 192.168.207.1)
  plx-mongo     published on 127.0.0.1:27017
  plx-azurite   published on gateway:10000-10002
  plx-api       published on 127.0.0.1:8000
  plx-campaign  (no ports; queue triggers)
  plx-dashboard published on 127.0.0.1:3000
  plx-portal    published on 127.0.0.1:8080
  plx-job-*     containers of a running job execution, removed when it finishes
```

MongoDB publishes one host port at `127.0.0.1:27017`. Local clients use `mongodb://127.0.0.1:27017/predixlab`; workload containers connect directly to MongoDB on the container network. The container address is resolved when workloads start. The agent recreates dependent services if it changes after a MongoDB restart. Azurite publishes its ports on the network gateway for container access. To change the subnet, set `PLX_SUBNET` or `network.subnet` in `config.json` and recreate the network with `./plx destroy --yes`.

## Raw data and state

The existing `~/.predixlab` raw data directory is mounted at `/data` in the API, collector, signals, and options containers. Set `PLX_DATA_DIR` to override this location. No data migration or seeding is required when the directory already contains the needed files. The host agent only dispatches due jobs; it launches each job step with Apple `container`. Collector and signals run as separate containers in the `signal` job: collector completes as the init container before signals starts. The `options` job runs in its own container.

Local orchestration state is stored separately in `~/.predixlab-staging` (`PLX_HOME`):

| Path | Content |
| --- | --- |
| `secrets.env` | Local secrets (mode 0600), keyed by secret catalog name; independent `jwt-secret-key`, `unsubscribe-secret`, and `session-secret-key` values are generated |
| `config.json` | Optional local overrides (network, catch-up window, disabled jobs, environment variables, build resources) |
| `state.json` | Desired state (`up`/`down`), individually stopped services and the last fire time of each job; guarded by `state.json.lock` |
| `logs/agent.log` | Agent log (rotated) |
| `logs/jobs/<job>/<execution>.log` | Log of each execution; every line is prefixed with the UTC time and the container name |
| `history/executions.jsonl` | Execution history (status, trigger, scheduled time, exit code of each container) |
| `build/` | Build contexts staged from the git file list (what CI checks out) |

MongoDB and Azurite data live in the named volumes `plx-mongo-data` and `plx-azurite-data`.
`./plx destroy --yes --purge-data` removes staging state and emulator volumes, but does not remove the raw data directory.

For an existing staging environment, rerun `./plx init` before starting the updated API. It adds a missing `session-secret-key` without replacing existing keys; the API receives it as `SESSION_SECRET_KEY`. This key is stable across restarts. Rotating it invalidates existing session cookies and requires in-progress OAuth sign-ins to restart.

`./plx init` also adds a missing `unsubscribe-secret` without replacing existing keys. It corresponds to production's `campaign-unsubscribe-signing-key` and is shared by API and campaign as `UNSUBSCRIBE_SECRET`, independent of the JWT and session keys. Function App `keyVaultReference` settings resolve to the local catalog value rather than an Azure reference string. `./plx env api` and `./plx env campaign` mask it unless `--show-secrets` is explicitly requested.

## Job execution semantics

Without the collector image, the scheduled `signal` job fails at its init step (scheduled runs always run init containers); run it manually with `./plx job run signal --skip-init` instead. Likewise, without the options image the `options` job fails, both scheduled and manual, because the options image is its only container and `--skip-init` does not apply; to stop scheduled attempts, set `"jobs": { "options": { "enabled": false } }` in `config.json`.

- One execution per job may run at a time, matching production parallelism. Additional triggers are skipped and recorded.
- Init containers run sequentially and must succeed before the main container starts. `replicaTimeout` applies to the entire execution; `replicaRetryLimit` reruns the full replica, including init containers.
- `./plx job run <job>` follows execution logs in the foreground; Ctrl-C stops the execution. Use `--detach` for background execution.
- Manual `signal` runs that include the collector are blocked during US regular trading hours to avoid ingesting incomplete market sessions. Use `--skip-init` to run signals without collection or `--force` to override the guard.
- `./plx down` without arguments pauses the whole environment: the agent stops restarting services and records due jobs as skipped. `./plx down <service>` stops only that service; other services and schedules continue.
- `./plx job exec <job> [--container NAME] -- <command>` runs a one-off command with the job's image, environment, mounts and network (not recorded in history), for example `-- python -m predixlab_signals daily --dry-run`.
- After an unexpected restart, the agent marks interrupted executions and removes leftover containers.
- Execution records and logs are retained for 30 days, matching the production Log Analytics retention period.

## Builds

`./plx build` prepares a build context for each target. By default, it includes tracked and unignored working-tree files, including uncommitted changes. With `--ref`, it uses `git archive <ref>` instead. Each application uses its own Dockerfile; the resulting image is tagged `predixlab-local/<target>:latest` and includes revision and working-tree status labels visible in `./plx status`.

- `campaign`: uses the official Functions host image (amd64 under Rosetta) and installs dependencies into `.python_packages/lib/site-packages`, matching the CI package layout. `.funcignore` filters the build context.
- `dashboard` / `portal`: run `npm ci && npm run build`, rewrite configured production origins to local endpoints, and serve the output through the Static Web Apps CLI emulator with `staticwebapp.config.json`.

## Seeding data

```bash
./plx seed data --from-azure --options-days 2  # optional: populate missing raw data
./plx seed data --from ./local-data --options-days 2  # optional: copy from another directory
./plx seed db --from-azure --yes  # optional: market-data collections
```

Do not seed from `~/.predixlab` when it is the mounted directory. Azure data seeding reads the production share; database seeding copies market-data collections and refuses personal-data collections. If the collector image is available, an unseeded data directory is backfilled from 2015-01-01 on the first `signal` run; otherwise, provide the input data yourself before running `signal --skip-init`. The signal job requires models under `~/.predixlab/models/us/` (or the corresponding path under `PLX_DATA_DIR`).

## Adding or changing configuration

1. Update [`config/platform.json`](../config/platform.json) for environment variables, secret references, schedules, and resource settings. Use `values` for environment-specific values and `parameter` plus a `parameters` catalog entry for deployment-time inputs.
2. Run `python3 -m unittest discover -s tests` and `./plx doctor`.
3. Verify affected services with `./plx up --recreate` or jobs with `./plx job run <job>`.
4. Review the Azure preview with `azure/scripts/deploy.sh what-if`, then deploy with `azure/scripts/deploy.sh deploy`.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `StreamClosed` while pulling from `mcr.microsoft.com` or `*.azurecr.io` | `plx` uses `--max-concurrent-downloads 1` for these registries; retry the pull if the connection closes |
| The API does not start | `./plx logs api`; the API exits when MongoDB is unreachable, and the agent restarts it with backoff |
| Campaign does not consume messages | Inspect `./plx logs campaign` and check for `*-poison` queues with `./plx queue ls`; `./plx queue clear <queue>` removes test messages |
| The Dashboard still calls the production API | Rebuild with `./plx build dashboard`; `./plx smoke` checks the bundle for production origins |
| Sign-in codes | With `EMAIL_BACKEND=console`, find codes in `./plx logs api` |
| A job did not run on time | `./plx scheduler status` and `~/.predixlab-staging/logs/agent.log`; if the Mac slept longer than `catchUpMinutes`, the run is logged as missed |
| Port conflict | Stop local servers using ports 27017/3000/8000/8080; MongoDB publishes directly on `127.0.0.1:27017` |
