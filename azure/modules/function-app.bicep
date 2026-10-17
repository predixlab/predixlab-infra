import { appliesTo, envValue } from 'platform-functions.bicep'

param location string

@description('Function App definition from config/platform.json (functionApps[]).')
param functionApp object

param environmentName string
param planName string
param appInsightsConnectionString string

@description('Resource ID of the Application Insights component (portal linkage tag).')
param appInsightsId string
param storageAccountName string
param contentShareName string

@secure()
@description('WEBSITE_RUN_FROM_PACKAGE currently set by the Function App CI; empty to omit.')
param runFromPackage string = ''

@secure()
@description('Secret values keyed by secret catalog name (the Function App uses literal app settings).')
param secretValues object

@description('Deployment parameter values keyed by config/platform.json parameter name.')
@secure()
param parameterValues object

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};AccountKey=${storageAccount.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'
var resolvedSecrets = union(secretValues, {
  'storage-account-connection-string': storageConnectionString
})

// Settings owned by the hosting platform rather than by the workload definition.
var hostingSettings = [
  {
    name: 'WEBSITE_CONTENTAZUREFILECONNECTIONSTRING'
    value: storageConnectionString
  }
  {
    name: 'WEBSITE_CONTENTSHARE'
    value: contentShareName
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: appInsightsConnectionString
  }
  {
    name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
    value: 'true'
  }
  {
    name: 'WEBSITE_ENABLE_SYNC_UPDATE_SITE'
    value: 'true'
  }
]

var workloadSettings = map(
  filter(functionApp.settings, entry => appliesTo(entry, environmentName)),
  entry => {
    name: entry.name
    value: contains(entry, 'secret') ? resolvedSecrets[entry.secret] : envValue(entry, environmentName, parameterValues)
  }
)

var packageSettings = empty(runFromPackage)
  ? []
  : [
      {
        name: 'WEBSITE_RUN_FROM_PACKAGE'
        value: runFromPackage
      }
    ]

resource plan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: planName
  location: location
  kind: 'functionapp'
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  properties: {
    reserved: true
  }
}

resource site 'Microsoft.Web/sites@2024-04-01' = {
  name: functionApp.azureName
  location: location
  kind: 'functionapp,linux'
  tags: {
    'hidden-link: /app-insights-resource-id': appInsightsId
  }
  properties: {
    serverFarmId: plan.id
    reserved: true
    httpsOnly: false
    clientAffinityEnabled: false
    siteConfig: {
      linuxFxVersion: functionApp.linuxFxVersion
      ftpsState: 'FtpsOnly'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      http20Enabled: false
      use32BitWorkerProcess: false
      functionAppScaleLimit: functionApp.functionAppScaleLimit
      appSettings: concat(hostingSettings, workloadSettings, packageSettings)
    }
  }
}

output name string = site.name
output defaultHostName string = site.properties.defaultHostName
