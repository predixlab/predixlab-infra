param location string
param name string
param logAnalyticsWorkspaceName string

@description('Resource IDs of user-assigned identities attached to the environment.')
param userAssignedIdentityIds array = []

param storageAccountName string

@description('Azure Files mounts: [{ name, shareName, accessMode }].')
param storages array

@description('Managed certificates: [{ name, subjectName }] validated by CNAME.')
param managedCertificates array = []

@description('Explicit peer-to-peer traffic encryption setting; null leaves it unset.')
param peerTrafficEncryption bool?

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: name
  location: location
  identity: empty(userAssignedIdentityIds)
    ? {
        type: 'SystemAssigned'
      }
    : {
        type: 'SystemAssigned, UserAssigned'
        userAssignedIdentities: toObject(userAssignedIdentityIds, id => id, id => {})
      }
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
    peerAuthentication: {
      mtls: {
        enabled: false
      }
    }
    peerTrafficConfiguration: peerTrafficEncryption == null
      ? null
      : {
          encryption: {
            enabled: peerTrafficEncryption
          }
        }
  }
}

resource environmentStorages 'Microsoft.App/managedEnvironments/storages@2025-01-01' = [
  for storage in storages: {
    parent: managedEnvironment
    name: storage.name
    properties: {
      azureFile: {
        accountName: storageAccount.name
        accountKey: storageAccount.listKeys().keys[0].value
        shareName: storage.shareName
        accessMode: storage.accessMode
      }
    }
  }
]

resource certificates 'Microsoft.App/managedEnvironments/managedCertificates@2025-01-01' = [
  for certificate in managedCertificates: {
    parent: managedEnvironment
    name: certificate.name
    location: location
    properties: {
      subjectName: certificate.subjectName
      domainControlValidation: 'CNAME'
    }
  }
]

output id string = managedEnvironment.id
output name string = managedEnvironment.name
output defaultDomain string = managedEnvironment.properties.defaultDomain
output principalId string = managedEnvironment.identity.principalId
output managedCertificates array = [
  for (certificate, i) in managedCertificates: {
    subjectName: certificate.subjectName
    id: certificates[i].id
  }
]
