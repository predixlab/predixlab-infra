// PredixLab production infrastructure (resource-group scope).
//
// Workload shape (containers, env, schedules, resources, secrets) is loaded from
// ../config/platform.json, the same file the local Apple container staging
// environment uses, so both environments cannot drift apart silently.
targetScope = 'resourceGroup'

@description('Environment key used to select per-environment values from config/platform.json.')
param environmentName string = 'prod'

@description('Primary region for regional resources.')
param location string = resourceGroup().location

@description('Region of the Static Web Apps (Free SKU is not available in every region).')
param staticSitesLocation string = 'westus2'

@description('Names of shared infrastructure resources.')
param names object

@description('Container Apps environments keyed by the logical names used in platform.json.')
param containerAppsEnvironments object

@description('Live image tags for images whose tagPolicy is "live". Resolved by azure/scripts/deploy.sh from the running workloads so infrastructure deployments never roll back application releases.')
param imageTags object = {}

@description('Values of the deployment parameters declared in config/platform.json ("parameters"), keyed by name. They are specific to an environment or person and never committed; the parameter file reads them from PLX_PARAM_<NAME> environment variables.')
@secure()
param platformParameters object

@description('Resource ID of the Log Analytics workspace backing the campaign Application Insights component.')
param appInsightsWorkspaceResourceId string

@description('GitHub OIDC federated credentials: [{ identity, name, subject }].')
param githubFederatedCredentials array = []

@description('ACS email sender usernames: [{ name, username, displayName }].')
param emailSenders array = []

@description('Create RBAC role assignments. Keep false for the existing production resource group, whose assignments already exist under different names.')
param deployRoleAssignments bool = false

@description('Principal ID of the GitHub deployment service principal for the campaign Function App (role assignment only).')
@secure()
param campaignDeployPrincipalId string = ''

@description('Register the data file share with the Recovery Services vault. Production is already protected.')
param configureFileShareBackup bool = false

@description('Whether the data share is protected by Azure Backup (keeps the AzureBackupProtected share metadata).')
param fileShareBackupProtected bool = true

@secure()
@description('Cosmos DB connection string for the Function App (read from Key Vault by the parameter file).')
param mongoConnectionString string

@secure()
@description('ACS connection string for the Function App (read from Key Vault by the parameter file).')
param acsConnectionString string

@secure()
@description('JWT/unsubscribe signing key for the Function App (read from Key Vault by the parameter file).')
param jwtSecretKey string

@secure()
@description('Current WEBSITE_RUN_FROM_PACKAGE value of the campaign Function App. Owned by the campaign CI; preserved by deploy.sh.')
param campaignRunFromPackage string = ''

var platform = loadJsonContent('../config/platform.json')
var registryServer = platform.registry

// Deterministic so that what-if can resolve Key Vault references before deployment.
var keyVaultUri = 'https://${names.keyVault}${environment().suffixes.keyvaultDns}/'

var imageRefs = toObject(
  items(platform.images),
  image => image.key,
  image => '${registryServer}/${image.value.repository}:${image.value.tagPolicy == 'live' ? (imageTags[?image.key] ?? image.value.defaultTag) : image.value.defaultTag}'
)

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    environments: containerAppsEnvironments
    appInsightsName: names.functionAppInsights
    appInsightsWorkspaceResourceId: appInsightsWorkspaceResourceId
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    name: names.storageAccount
    dataShareName: platform.sharedStorage.shareName
    dataShareQuotaGiB: platform.sharedStorage.quotaGiB
    dataShareBackupProtected: fileShareBackupProtected || configureFileShareBackup
    functionContentShareName: names.functionContentShare
    queues: platform.queues
  }
}

module keyVault 'modules/key-vault.bicep' = {
  name: 'key-vault'
  params: {
    location: location
    name: names.keyVault
  }
}

module cosmos 'modules/cosmos-mongo.bicep' = {
  name: 'cosmos-mongo'
  params: {
    location: location
    name: names.cosmosAccount
    databaseName: platform.databaseName
  }
}

module registry 'modules/container-registry.bicep' = {
  name: 'container-registry'
  params: {
    location: location
    name: names.containerRegistry
  }
}

module identities 'modules/identities.bicep' = {
  name: 'identities'
  params: {
    location: location
    identities: names.identities
    githubFederatedCredentials: githubFederatedCredentials
  }
}

module communication 'modules/communication.bicep' = {
  name: 'communication'
  params: {
    communicationServiceName: names.communicationService
    emailServiceName: names.emailService
    emailDomainName: names.emailDomain
    senders: emailSenders
  }
}

module environments 'modules/container-apps-environment.bicep' = [
  for env in items(containerAppsEnvironments): {
    name: 'aca-env-${env.key}'
    params: {
      location: location
      name: env.value.name
      logAnalyticsWorkspaceName: env.value.logAnalyticsWorkspace
      userAssignedIdentityIds: map(env.value.?userAssignedIdentities ?? [], identity => identityIds[identity])
      storageAccountName: storage.outputs.name
      storages: env.value.storages
      managedCertificates: env.value.?managedCertificates ?? []
      peerTrafficEncryption: env.value.?peerTrafficEncryption
    }
    dependsOn: [
      monitoring
    ]
  }
]

var identityIds = toObject(identities.outputs.identities, identity => identity.key, identity => identity.id)

var environmentIndex = toObject(
  map(range(0, length(items(containerAppsEnvironments))), i => {
    key: items(containerAppsEnvironments)[i].key
    index: i
  }),
  entry => entry.key,
  entry => entry.index
)

module containerApps 'modules/container-app.bicep' = [
  for app in platform.containerApps: {
    name: 'aca-app-${app.name}'
    params: {
      location: location
      app: app
      environmentName: environmentName
      environmentId: environments[environmentIndex[app.environment]].outputs.id
      identityId: identityIds[app.identity]
      keyVaultUri: keyVaultUri
      secretCatalog: platform.secrets
      imageRefs: imageRefs
      registryName: registry.outputs.name
      registryServer: registryServer
      parameterValues: platformParameters
      managedCertificates: environments[environmentIndex[app.environment]].outputs.managedCertificates
    }
  }
]

module jobs 'modules/container-apps-job.bicep' = [
  for job in platform.jobs: {
    name: 'aca-job-${job.name}'
    params: {
      location: location
      job: job
      environmentName: environmentName
      environmentId: environments[environmentIndex[job.environment]].outputs.id
      identityId: identityIds[job.identity]
      keyVaultUri: keyVaultUri
      secretCatalog: platform.secrets
      imageRefs: imageRefs
      registryServer: registryServer
      parameterValues: platformParameters
    }
  }
]

module functionApps 'modules/function-app.bicep' = [
  for fn in platform.functionApps: {
    name: 'function-${fn.name}'
    params: {
      location: location
      functionApp: fn
      environmentName: environmentName
      planName: names.functionPlan
      appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
      appInsightsId: monitoring.outputs.appInsightsId
      storageAccountName: storage.outputs.name
      contentShareName: names.functionContentShare
      runFromPackage: campaignRunFromPackage
      secretValues: {
        'mongodb-connection-string': mongoConnectionString
        'acs-connection-string': acsConnectionString
        'jwt-secret-key': jwtSecretKey
      }
      parameterValues: platformParameters
    }
  }
]

module staticSites 'modules/static-web-app.bicep' = [
  for site in platform.staticSites: {
    name: 'swa-${site.name}'
    params: {
      location: staticSitesLocation
      site: site
    }
  }
]

module backup 'modules/backup.bicep' = {
  name: 'backup'
  params: {
    location: location
    vaultName: names.recoveryVault
    storageAccountName: storage.outputs.name
    fileShareName: platform.sharedStorage.shareName
    configureProtection: configureFileShareBackup
  }
}

module roleAssignments 'modules/role-assignments.bicep' = if (deployRoleAssignments) {
  name: 'role-assignments'
  params: {
    keyVaultName: keyVault.outputs.name
    registryName: registry.outputs.name
    identityPrincipals: identities.outputs.identities
    environmentPrincipalIds: [for (env, i) in items(containerAppsEnvironments): environments[i].outputs.principalId]
    functionAppName: platform.functionApps[0].azureName
    campaignDeployPrincipalId: campaignDeployPrincipalId
  }
  dependsOn: [
    functionApps
  ]
}

output imageRefs object = imageRefs
output apiFqdns array = [for (app, i) in platform.containerApps: containerApps[i].outputs.fqdn]
output environmentDefaultDomains array = [for (env, i) in items(containerAppsEnvironments): environments[i].outputs.defaultDomain]
output staticSiteHostnames array = [for (site, i) in platform.staticSites: staticSites[i].outputs.defaultHostname]
