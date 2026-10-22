# PredixLab Infrastructure

[Contributing](CONTRIBUTING.md) | [Security](SECURITY.md) | [Apache-2.0](LICENSE)

`predixlab-infra` provides a local staging environment based on Apple's `container` runtime and Azure infrastructure defined with Bicep. Both environments use the workload configuration in [`config/platform.json`](config/platform.json) for application settings, container specifications, and job schedules. Validate changes locally before releasing them to production.

The signal, backtest, API and dashboard code is open source (Apache-2.0).

| Path | Purpose |
| --- | --- |
| [`config/platform.json`](config/platform.json) | Shared workload definitions: images, commands, resources, environment variables, secret references, job schedules, queues, and static sites |
| [`local/`](local/) and [`plx`](plx) | Local staging with Apple `container`, MongoDB, Azurite, and scheduled jobs |
| [`azure/`](azure/) | Bicep modules and deployment parameters for the `predixlab` resource group |

Environment-specific values use `values: { prod, local }`. Deployment-specific values use the `parameters` catalog and are supplied through `PLX_PARAM_*`; `./plx diff` lists these differences.

## Environments

| Production (Azure) | Local (Apple container) |
| --- | --- |
| Container App `predixlab-api` (port 8000, minReplicas 1, `/health` liveness probe) | Container `plx-api` at `http://localhost:8000`, monitored by the local agent |
| Container Apps job `predixlab-signal`: init `predixlab-collector` → `predixlab-signals`, `30 2 * * *` UTC, timeout 1800 s, no retries | Job execution with the same init/main containers, cron (UTC), timeout and retry semantics |
| Container Apps job `predixlab-options`: `30 11 * * *` UTC, timeout 3600 s | Job with the same schedule and timeout |
| Function App `predixlab-campaign` (Python 3.12, queue triggers on `campaign-events` / `campaign-delivery`, same `host.json`) | Official Azure Functions host image (amd64 under Rosetta) with `CAMPAIGN_DELIVERY_BACKEND=console`, so no real email is sent |
| Static Web Apps `predixlab-dashboard` / `predixlab-portal` | Static Web Apps CLI emulator with each repository's `staticwebapp.config.json`, at `localhost:3000` / `localhost:8080` |
| Azure Files share `predixlab-data` mounted at `/data` | Existing `~/.predixlab` raw data directory mounted at `/data` |
| Cosmos DB for MongoDB (serverless, server version 7.0) | `mongo:7.0` container (named volume `plx-mongo-data`), accessible from the host at `127.0.0.1:27017` |
| Storage queues (and blob leases of the Functions host) | Azurite 3.37 (named volume `plx-azurite-data`) with the same queue names |
| Key Vault `predixlab-kv` | `~/.predixlab-staging/secrets.env` (mode 0600, keyed by the same secret catalog names) |
| Azure Communication Services email | Disabled by default; the API uses `EMAIL_BACKEND=console` for verification codes |
| Log Analytics | `./plx logs …`; job logs under `~/.predixlab-staging/logs/jobs/` |

Data collection and options analysis run as part of the hosted service and aren't in the public repositories.

## Schedules

Job schedules use UTC in both environments. Locally, a launchd-managed `plx agent` dispatches each due execution to separate Apple `container` job containers; it does not run the collector, signals, or options application code on the host:

| Job | Cron (UTC) | What it does |
| --- | --- | --- |
| `signal` | `30 2 * * *` | collector `update_daily_data` → signals `daily` (publishes Pivot/AlphaRank and enqueues `campaign.daily_signals`, which the campaign functions deliver) |
| `options` | `30 11 * * *` | `options_monitor run_daily_job` |
| API APScheduler (in process) | every minute 02:00–02:29 | Latest-picks cache check |

After sleep or an agent interruption, runs within the `catchUpMinutes` window (default 180) execute once as `catch-up`; older runs are recorded as `missed`. Executions use `caffeinate` to prevent sleep. Initial installation does not replay earlier schedules.

## Quick start

Requirements: Apple silicon with macOS 26, Apple `container` (`brew install container`), Rosetta 2, and the sibling repositories `predixlab-api`, `predixlab-signals`, `predixlab-campaign`, `predixlab-dashboard`, and `predixlab-portal`.

Local staging of the `signal` job's collection step and the `options` job also requires access to the `predixlab-collector` and `predixlab-options` sibling repositories, which aren't in the public repositories.

The Dashboard's remote is
[`predixlab-webapp`](https://github.com/predixlab/predixlab-webapp); clone it
into a sibling directory named `predixlab-dashboard`. Unit tests require only
Python 3.9+ and do not need Azure or the container runtime. Registry pulls,
production seeding, and cloud deployments require separately authorized access;
cloning this repository does not grant it.

```bash
git clone https://github.com/predixlab/predixlab-infra.git
cd predixlab-infra
./plx doctor
./plx init
./plx pull
./plx build
./plx up
./plx smoke
```

Install the optional background scheduler with `./plx scheduler install` only
after reviewing configuration, local secrets, job schedules, and available data.
Do not enable scheduled collection or publication merely to validate documentation.

Existing raw data in `~/.predixlab` is available directly to the API and job containers at `/data`; no data copy is required. Optionally use `./plx seed data --from-azure --options-days 2` to populate missing files or `./plx seed db --from-azure --yes` for market-data collections. Azure seeding requires access to the production data source. See [Local staging](docs/local.md) for state, build, and scheduling details.

- Dashboard: <http://localhost:3000> (email sign-in codes appear in `./plx logs api`)
- Portal: <http://localhost:8080>
- API: <http://localhost:8000/health>
- MongoDB: `mongodb://127.0.0.1:27017/predixlab` or `./plx mongo`

## Test locally, then release

1. Develop and run the unit tests in the application repository.
2. Build the affected target with `./plx build <target>` for working-tree changes or `./plx build <target> --ref origin/main` to match a CI revision.
3. Recreate affected services with `./plx up --recreate <service>` or run jobs with `./plx job run <job>`. During US market hours, use `--skip-init` to avoid collector ingestion or `--force` to permit it.
4. Run `./plx smoke`, check the Dashboard and Portal, and inspect `./plx job history` and `./plx queue peek campaign-delivery`.
5. Merge verified application changes to `main` for CI deployment. Collector and options use `:latest` in production; new images take effect at the next scheduled execution.
6. For infrastructure changes, update the shared configuration, verify locally, then run `azure/scripts/deploy.sh what-if` before `azure/scripts/deploy.sh deploy`.

## Intentional differences from production

| Difference | Reason |
| --- | --- |
| Endpoints: api/dashboard/portal → `localhost:8000/3000/8080`; CORS, OAuth callbacks and referral links follow | `values.local`; see `./plx diff` |
| Secrets: MongoDB and Storage point to local emulators; independent JWT, unsubscribe, and session keys are generated locally; ACS, OAuth, and Stripe are empty by default | Isolates staging from production services; use Stripe test-mode keys only |
| `EMAIL_BACKEND=console` (API) and `CAMPAIGN_DELIVERY_BACKEND=console` (campaign) | Never email real users |
| Stripe price IDs are empty locally | Configure test prices in `config.json` to exercise billing flows |
| Frontend production origins are rewritten during local builds (`originRewrites`) | Directs Dashboard and Portal requests to local endpoints |
| Portal is built without `VITE_GA_MEASUREMENT_ID` | Excludes staging traffic from production analytics |
| The API starts `/app/.venv/bin/uvicorn` directly as `appuser` | Accommodates differences in non-root process capabilities between Apple container and Container Apps |
| CPUs are rounded up (0.5 → 1); images are arm64 (campaign is amd64) | Apple container only accepts whole CPUs; `./plx pull-prod <image>` runs the exact production amd64 image instead |
| The SWA emulator redirects paths without a trailing slash with 301 (production returns 200) | Static Web Apps CLI emulator behavior |

The collector and signals run in separate containers within the `signal` job. Collector runs first as the init container; signals starts after collection succeeds. Both mount the same raw data directory at `/data`.

The `unsubscribe-secret` catalog entry maps to Key Vault secret `campaign-unsubscribe-signing-key`. API and campaign must share it as `UNSUBSCRIBE_SECRET` to sign and verify unsubscribe links. Locally, both use the same independently generated key; rerun `./plx init` to add it to an existing staging environment without replacing other keys. See [Azure secrets](docs/azure.md#secrets) for the runtime identities and reference formats.

## Common commands

```text
./plx status                         services, next runs (UTC/local), running executions, agent state
./plx logs <service|job|execution> [-f] [-n N]
./plx job run <job> [--skip-init] [--detach] [--force]
./plx job exec signal --container predixlab-signals -- python -m predixlab_signals daily --dry-run
./plx job history [job]  |  ./plx job stop <execution>
./plx env <workload> [--container NAME]   resolved local environment (secrets masked)
./plx diff                           local vs production differences
./plx queue ls|peek|send|clear <queue>
./plx seed db --from-azure --yes     read-only copy of market-data collections (no personal data)
./plx down [service…] | ./plx destroy --yes [--purge-data]
```

Local state and optional overrides use `~/.predixlab-staging`; raw data remains in `~/.predixlab` even after `./plx destroy --yes --purge-data`. Set `PLX_DATA_DIR` to use a different raw data directory. Local overrides go in `~/.predixlab-staging/config.json`:

```json
{
  "catchUpMinutes": 180,
  "jobs": { "options": { "enabled": false } },
  "env": {
    "api": { "DEBUG": "True", "STRIPE_MONTHLY_PRICE_ID": "price_test_..." },
    "signal/predixlab-signals": { "PIVOT_MAX_PER_SIDE": "10" }
  },
  "build": { "cpus": 4, "memory": "8G" }
}
```

## Azure deployment

```bash
cp azure/parameters/prod.env.example azure/parameters/prod.env
az login
azure/scripts/deploy.sh what-if
azure/scripts/deploy.sh deploy
```

Set `PLX_PARAM_ALERT_RECIPIENTS` in the ignored `prod.env` file and select the subscription through `--subscription`, `AZURE_SUBSCRIPTION_ID`, or the Azure CLI account context. The deployment script preserves live API and signals image tags and the campaign function package. See [Azure deployment](docs/azure.md) for parameters, prerequisites, and validation.

## Repository layout

```text
config/platform.json             workload definition (shared by Bicep and plx)
azure/main.bicep                 resource-group orchestration
azure/modules/*.bicep            storage, Key Vault, Cosmos DB, ACR, identities, ACS, Container Apps environments/apps/jobs, Function App, Static Web Apps, backup, RBAC
azure/parameters/prod.bicepparam production parameters (secrets are read from Key Vault with az.getSecret at deployment time)
azure/parameters/prod.env.example example for deployment-time values
azure/scripts/deploy.sh          validate / what-if / deploy
local/plx/                       local orchestrator (Python standard library only)
local/images/                    local-only images: Functions host, static sites (build + SWA emulator)
plx                              CLI entry point
tests/                           unit tests: python3 -m unittest discover -s tests
docs/                            Azure and local environment details
```

## Contributing and support

See [CONTRIBUTING.md](CONTRIBUTING.md) for unit tests, shell checks, and Bicep
validation. Use [GitHub Issues](https://github.com/predixlab/predixlab-infra/issues)
for bugs and proposals, and [SECURITY.md](SECURITY.md) for private security reports.
Never attach real environment files, cloud account identifiers, or secret values.

## License

Copyright 2026 PredixLab contributors.

Project source is licensed under the [Apache License 2.0](LICENSE). Cloud services,
container images, datasets, and other third-party materials retain their own
terms. Example deployment names do not grant access to PredixLab infrastructure.
