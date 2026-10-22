// Production (resource group "predixlab").
// Values mirror the live environment as inspected on 2026-09-23; see docs/azure.md.
// Nothing here is tied to a subscription or a person: those values are read from
// environment variables that azure/scripts/deploy.sh sets at deployment time.
using '../main.bicep'

// Target subscription (--subscription, AZURE_SUBSCRIPTION_ID or the Azure CLI's current subscription).
var subscriptionId = readEnvironmentVariable('AZURE_SUBSCRIPTION_ID', '')

param environmentName = 'prod'
param location = 'westus'
param staticSitesLocation = 'westus2'

param names = {
  storageAccount: 'predixlabstorage'
  keyVault: 'predixlab-kv'
  cosmosAccount: 'predixlab-db'
  containerRegistry: 'predixlabacr'
  communicationService: 'predixlab-acs'
  emailService: 'predixlab-ecs'
  emailDomain: 'predixlab.com'
  recoveryVault: 'predixlab-recovery-service-vault'
  functionPlan: 'WestUSLinuxDynamicPlan'
  functionAppInsights: 'predixlab-campaign'
  functionContentShare: 'predixlab-campaignd6376daf5643'
  identities: {
    // API runtime identity (Key Vault references) and GitHub OIDC deploy identity of predixlab-api.
    api: 'predixlab-container-api-server-environmentpredixlabacrpredixlabOidc'
    // Key Vault reference identity shared by the scheduled jobs.
    jobs: 'predixlab-kv-oidc'
  }
}

param containerAppsEnvironments = {
  api: {
    name: 'predixlab-container-api-server-environment'
    logAnalyticsWorkspace: 'predixlab-container-api-server-environment-log-workspace'
    storages: [
      {
        name: 'predixlab-data'
        shareName: 'predixlab-data'
        accessMode: 'ReadOnly'
      }
      {
        name: 'predixlab-data-rw'
        shareName: 'predixlab-data'
        accessMode: 'ReadWrite'
      }
    ]
    managedCertificates: [
      {
        name: 'api.predixlab.com-predixla-251231020733'
        subjectName: 'api.predixlab.com'
      }
    ]
    peerTrafficEncryption: false
  }
  worker: {
    name: 'predixlab-container-worker-environment'
    logAnalyticsWorkspace: 'predixlab-container-worker-environment-log-analytics-workspace'
    userAssignedIdentities: [
      'jobs'
    ]
    storages: [
      {
        name: 'predixlab-data'
        shareName: 'predixlab-data'
        accessMode: 'ReadWrite'
      }
    ]
    peerTrafficEncryption: false
  }
}

// Auto-created workspace in DefaultResourceGroup-WUS that backs the campaign Application Insights component
// (Azure names it after the subscription).
param appInsightsWorkspaceResourceId = '/subscriptions/${subscriptionId}/resourceGroups/DefaultResourceGroup-WUS/providers/Microsoft.OperationalInsights/workspaces/DefaultWorkspace-${subscriptionId}-WUS'

param githubFederatedCredentials = [
  {
    identity: 'api'
    name: 'predixlab-api-fed'
    subject: 'repo:predixlab@312333614/predixlab-api@1322549087:ref:refs/heads/main'
  }
  {
    identity: 'api'
    name: 'predixlab-api-env-production'
    subject: 'repo:predixlab@312333614/predixlab-api@1322549087:environment:production'
  }
]

param emailSenders = [
  {
    name: 'donotreply'
    username: 'donotreply'
    displayName: 'DoNotReply'
  }
  {
    name: 'notification'
    username: 'notification'
    displayName: 'PredixLab'
  }
  {
    name: 'alert'
    username: 'alert'
    displayName: 'PredixLab Alert'
  }
  {
    name: 'taskmanager'
    username: 'taskmanager'
    displayName: 'PredixLab Task Manager'
  }
]

// Images with tagPolicy "live" keep whatever release CI deployed last.
// azure/scripts/deploy.sh exports these from the running workloads.
param imageTags = {
  api: readEnvironmentVariable('PLX_IMAGE_TAG_API', 'latest')
  signals: readEnvironmentVariable('PLX_IMAGE_TAG_SIGNALS', 'latest')
}

// Deployment parameters declared in config/platform.json ("parameters"). deploy.sh refuses to
// run until every PLX_PARAM_<NAME> is set (environment or the untracked parameters/prod.env).
param platformParameters = {
  'alert-recipients': readEnvironmentVariable('PLX_PARAM_ALERT_RECIPIENTS', '')
}

// Role assignments already exist in production under other names; campaignDeployPrincipalId
// is only needed when deployRoleAssignments is true.
param deployRoleAssignments = false
param campaignDeployPrincipalId = readEnvironmentVariable('PLX_CAMPAIGN_DEPLOY_PRINCIPAL_ID', '')
param configureFileShareBackup = false

// Existing secret settings remain literal values read at deployment time.
// UNSUBSCRIBE_SECRET is a runtime Key Vault reference defined in platform.json.
param mongoConnectionString = az.getSecret(subscriptionId, 'predixlab', 'predixlab-kv', 'predixlab-mongodb-connection-string')
param acsConnectionString = az.getSecret(subscriptionId, 'predixlab', 'predixlab-kv', 'predixlab-acs-connection-string')
param jwtSecretKey = az.getSecret(subscriptionId, 'predixlab', 'predixlab-kv', 'jwt-secret-key')

// Owned by the campaign CI (Azure/functions-action); deploy.sh passes the current value through.
param campaignRunFromPackage = readEnvironmentVariable('PLX_CAMPAIGN_RUN_FROM_PACKAGE', '')
