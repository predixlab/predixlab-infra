@description('Region for the workspaces and Application Insights component.')
param location string

@description('Container Apps environments; each entry names its Log Analytics workspace.')
param environments object

param appInsightsName string

@description('Existing workspace (may live in another resource group) that backs Application Insights.')
param appInsightsWorkspaceResourceId string

resource workspaces 'Microsoft.OperationalInsights/workspaces@2023-09-01' = [
  for env in items(environments): {
    name: env.value.logAnalyticsWorkspace
    location: location
    properties: {
      sku: {
        name: 'PerGB2018'
      }
      retentionInDays: 30
      workspaceCapping: {
        dailyQuotaGb: -1
      }
      publicNetworkAccessForIngestion: 'Enabled'
      publicNetworkAccessForQuery: 'Enabled'
    }
  }
]

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    IngestionMode: 'LogAnalytics'
    WorkspaceResourceId: appInsightsWorkspaceResourceId
    RetentionInDays: 90
  }
}

output appInsightsConnectionString string = appInsights.properties.ConnectionString
output appInsightsId string = appInsights.id
