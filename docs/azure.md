# Azure Deployment

The Bicep templates provision the PredixLab platform in the `predixlab` resource group by default. The target subscription and deployment-specific values are supplied at deployment time. Workload configuration is shared with the [local staging environment](local.md) through [`config/platform.json`](../config/platform.json).

## Resources

| Module | Resources |
| --- | --- |
| `monitoring.bicep` | Two Log Analytics workspaces (one per Container Apps environment, 30-day retention) and workspace-based Application Insights for `predixlab-campaign` |
| `storage.bicep` | `predixlabstorage` (StorageV2, LRS, Hot, TLS 1.2), the file shares `predixlab-data` (data) and the Function App content share, the queues `campaign-events` / `campaign-delivery`, and delete-retention policies |
| `key-vault.bicep` | `predixlab-kv` (RBAC, 90-day soft delete, readable by template deployments) |
| `cosmos-mongo.bicep` | `predixlab-db`: Cosmos DB for MongoDB, serverless, server version 7.0, database `predixlab` (the applications create their own collections and indexes) |
| `container-registry.bicep` | `predixlabacr` (Basic, with admin credentials for the API deployment action) |
| `identities.bicep` | `predixlab-kv-oidc` (the jobs read Key Vault) and `predixlab-container-api-server-environmentpredixlabacrpredixlabOidc` (the API reads Key Vault and deploys through GitHub OIDC with the federated credential `predixlab-api-fed`) |
| `communication.bicep` | Email Service `predixlab-ecs`, the customer-managed domain `predixlab.com`, the senders `donotreply/notification/alert/taskmanager`, and Communication Services `predixlab-acs` |
| `container-apps-environment.bicep` | `predixlab-container-api-server-environment` (storages `predixlab-data` read-only and `predixlab-data-rw` read-write, managed certificate for `api.predixlab.com`) and `predixlab-container-worker-environment` (storage `predixlab-data` read-write, identity `predixlab-kv-oidc`) |
| `container-app.bicep` | `predixlab-api` (external ingress on port 8000, custom domain, probes, 1–10 replicas, Key Vault secret references) |
| `container-apps-job.bicep` | `predixlab-signal` and `predixlab-options` (schedule triggers; images are pulled with the environment's system-assigned identity) |
| `function-app.bicep` | Linux Consumption plan `WestUSLinuxDynamicPlan` (Y1) and `predixlab-campaign` (Python 3.12, system-assigned identity for runtime Key Vault references) |
| `static-web-app.bicep` | `predixlab-dashboard` and `predixlab-portal` (Free, westus2, custom domains) |
| `backup.bicep` | Recovery Services vault and the daily (19:30 UTC, 30-day retention) backup policy for the `predixlab-data` share |
| `role-assignments.bicep` | Optional Key Vault, AcrPull, and deployment role assignments, enabled by `deployRoleAssignments` |

Data collection and options analysis run as part of the hosted service and aren't in the public repositories.

The API, jobs, and Function App settings are defined in [`config/platform.json`](../config/platform.json) and translated into ARM resources by the Bicep modules and [`platform-functions.bicep`](../azure/modules/platform-functions.bicep). Environment entries support `value` (shared), `values.prod` (production-specific), `secret` (Key Vault-backed), and `parameter` (deployment-time input). Container Apps use Key Vault references. Function App `secret` settings remain literal values read during deployment; its Function-App-only `keyVaultReference` setting kind instead renders a runtime reference using the secret catalog.

### Resource ownership

Deployments use incremental mode. Resources outside these templates, including application-managed storage containers and monitoring resources, are not removed. Review ownership before changing the deployment mode or bringing existing resources under template management.

## Deployment

Prerequisites: Azure CLI access to the target subscription and resource group, permission to deploy resources and read the required Key Vault secrets, and production configuration in [`prod.bicepparam`](../azure/parameters/prod.bicepparam).

Copy the [environment file example](../azure/parameters/prod.env.example) to `azure/parameters/prod.env` and set `PLX_PARAM_ALERT_RECIPIENTS` to the production recipients. Optionally set `AZURE_SUBSCRIPTION_ID`; otherwise, the Azure CLI's active subscription is used. The `--subscription` option takes precedence over both.

```bash
cp azure/parameters/prod.env.example azure/parameters/prod.env
az login
azure/scripts/deploy.sh validate
azure/scripts/deploy.sh what-if
azure/scripts/deploy.sh deploy
```

Run these commands from the repository root. `what-if` previews changes without deploying them; `--show-unchanged` includes unchanged and ignored resources. `deploy` requests confirmation after its own preview unless `--yes` is provided.

### Deployment inputs

| Input | Purpose |
| --- | --- |
| `--subscription` or `AZURE_SUBSCRIPTION_ID` | Selects the Azure subscription; defaults to the Azure CLI's active subscription |
| `--resource-group` | Selects the target resource group; defaults to `predixlab` |
| `PLX_PARAM_ALERT_RECIPIENTS` | Required comma-separated recipients for job-status email (`ACS_TO_EMAILS`); the local value is empty |
| `PLX_IMAGE_TAG_API`, `PLX_IMAGE_TAG_SIGNALS` | Optional image-tag overrides; otherwise the currently deployed tags are retained |
| `PLX_CAMPAIGN_RUN_FROM_PACKAGE` | Optional Function App package override; otherwise the deployed package setting is retained |
| `PLX_CAMPAIGN_DEPLOY_PRINCIPAL_ID` | Optional campaign deployment principal for environments with role assignment creation enabled |

The optional `.env` file beside the selected `.bicepparam` file supplies deployment inputs; process environment values take precedence. The file is ignored by Git and parsed as data rather than executed. All deployment parameters declared in [`config/platform.json`](../config/platform.json) must be provided. Keep subscription and principal identifiers, operator addresses, and secret values out of tracked files.

The script retains live image tags for the API and signals workloads and the campaign Function App's `WEBSITE_RUN_FROM_PACKAGE` setting unless overridden. Collector and options use their fixed `latest` image tags. The templates preserve Azure Backup metadata on the data share and the Function App's Application Insights linkage tag.

## Secrets

Key Vault must contain these secrets:

`predixlab-mongodb-connection-string`, `predixlab-storage-account-connection-string`, `predixlab-acs-connection-string`, `jwt-secret-key`, `campaign-unsubscribe-signing-key`, `session-secret-key`, `google-oauth-client-id`, `google-oauth-client-secret`, `microsoft-oauth-client-id`, `microsoft-oauth-client-secret`, `stripe-secret-key`, `stripe-publishable-key`, `stripe-webhook-secret`

The `secrets` catalog in [`config/platform.json`](../config/platform.json) defines their purposes. The Function App uses literal `MONGODB_URL`, `ACS_CONNECTION_STRING`, and `SECRET_KEY` settings resolved from `predixlab-kv` at deployment time with `az.getSecret()`. Its storage connection string uses `listKeys()`. Container Apps and jobs use managed-identity Key Vault references. Deployment-specific parameters are marked secure in Bicep.

The catalog entry `unsubscribe-secret` maps to Key Vault secret `campaign-unsubscribe-signing-key`. Both API and campaign must receive the same value as `UNSUBSCRIBE_SECRET` so the API can verify campaign unsubscribe links; keep it independent of `jwt-secret-key` and `session-secret-key`. The API uses the versionless Key Vault URL `https://predixlab-kv.vault.azure.net/secrets/campaign-unsubscribe-signing-key` with its existing user-assigned `api` identity. Campaign uses exactly `@Microsoft.KeyVault(SecretUri=https://predixlab-kv.vault.azure.net/secrets/campaign-unsubscribe-signing-key/)`, with a trailing slash and `keyVaultReferenceIdentity: SystemAssigned`. This preserves the configuration already set in production without changing other app settings. For a fresh environment, provision this secret and enable `deployRoleAssignments` to grant the Function App's system-assigned principal `Key Vault Secrets User` on the vault after the identity exists. Production keeps that flag false because Ops already created the equivalent assignment under a different name.

Before deploying the updated API, create `session-secret-key` in Key Vault using an independent high-entropy value, such as 32 random bytes encoded as 64 hex characters. The API receives it as required `SESSION_SECRET_KEY`; do not reuse `jwt-secret-key`. Keep the value consistent across replicas. Rotating it invalidates existing session cookies, including those signed with the retired hardcoded key, so complete the rollout across all replicas and restart in-progress OAuth sign-ins. The API has no default or legacy-key fallback.

## Provisioning additional environments

- Create an environment-specific `.bicepparam` file with appropriate resource names, Key Vault references, and settings; select the subscription and resource group at deployment time.
- Enable `deployRoleAssignments` when the required roles do not already exist. Supply `PLX_CAMPAIGN_DEPLOY_PRINCIPAL_ID` in the untracked environment file if the campaign deployment role is needed. Production sets `deployRoleAssignments = false` because equivalent assignments already exist.
- Enable `configureFileShareBackup` to register a new file share for backup protection.
- Configure DNS before adding custom domains or managed certificates (CNAME for subdomains; TXT for the apex).
- A GitHub-linked Static Web App requires a repository token. Alternatively, create it without a repository link and publish with a deployment token.

## Preview considerations

ARM `what-if` can report differences for read-only properties, service defaults, and values resolved during deployment. In particular, Container Apps environment Log Analytics settings may appear as unresolved expressions, and the API's secret array can appear reordered. Review the complete preview before deploying; do not assume reported changes are harmless.

## Operational considerations

- The API configuration does not currently set `PUBLIC_API_URL`; confirm unsubscribe-link URLs before enabling email flows.
- Frontend builds reference production origins in source. Local builds apply `originRewrites` to target staging services.
- The campaign Function App has no HTTP triggers; its triggers consume storage queue messages.
