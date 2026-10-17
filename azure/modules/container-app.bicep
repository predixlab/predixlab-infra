import { appContainer, azureFileVolumes, keyVaultSecrets, referencedSecrets } from 'platform-functions.bicep'

param location string

@description('Container app definition from config/platform.json (containerApps[]).')
param app object

param environmentName string
param environmentId string
param identityId string
param keyVaultUri string
// Only Key Vault secret names, never values.
#disable-next-line secure-secrets-in-params
param secretCatalog object
param imageRefs object
param registryName string

@description('Deployment parameter values keyed by config/platform.json parameter name.')
@secure()
param parameterValues object

@description('Registry login server, e.g. predixlabacr.azurecr.io.')
param registryServer string

@description('Managed certificates of the environment: [{ subjectName, id }].')
param managedCertificates array = []

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

var useAdminCredentials = (app.?registryAuth ?? 'environmentIdentity') == 'adminCredentials'

// Secret name used by azure/container-apps-deploy-action for registry credentials.
var registryPasswordSecretName = '${replace(registryServer, '.', '')}-${registryName}'

var secretKeys = sort(union(referencedSecrets(app.containers), app.?additionalSecrets ?? []), (left, right) => left < right)

var customDomains = [
  for domain in app.ingress.?customDomains ?? []: {
    name: domain
    bindingType: 'SniEnabled'
    certificateId: first(filter(managedCertificates, certificate => certificate.subjectName == domain)).id
  }
]

resource containerApp 'Microsoft.App/containerApps@2025-01-01' = {
  name: app.azureName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: app.activeRevisionsMode
      maxInactiveRevisions: app.maxInactiveRevisions
      ingress: {
        external: app.ingress.external
        targetPort: app.ingress.targetPort
        transport: app.ingress.transport
        allowInsecure: app.ingress.allowInsecure
        stickySessions: {
          affinity: 'none'
        }
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
        customDomains: customDomains
      }
      registries: useAdminCredentials
        ? [
            {
              // The admin user name always equals the registry name.
              server: registryServer
              username: registryName
              passwordSecretRef: registryPasswordSecretName
            }
          ]
        : [
            {
              server: registryServer
              identity: 'system-environment'
            }
          ]
      secrets: concat(
        keyVaultSecrets(secretKeys, secretCatalog, keyVaultUri, identityId),
        useAdminCredentials
          ? [
              {
                name: registryPasswordSecretName
                value: registry.listCredentials().passwords[0].value
              }
            ]
          : []
      )
    }
    template: {
      containers: map(app.containers, container => appContainer(container, imageRefs, environmentName, parameterValues))
      scale: {
        minReplicas: app.scale.minReplicas
        maxReplicas: app.scale.maxReplicas
        cooldownPeriod: app.scale.cooldownPeriod
        pollingInterval: app.scale.pollingInterval
      }
      volumes: azureFileVolumes(app.volumes)
    }
  }
}

output fqdn string = containerApp.properties.configuration.ingress.fqdn
