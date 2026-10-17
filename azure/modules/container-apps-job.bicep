import { appContainer, azureFileVolumes, initContainer, keyVaultSecrets, referencedSecrets } from 'platform-functions.bicep'

param location string

@description('Job definition from config/platform.json (jobs[]).')
param job object

param environmentName string
param environmentId string
param identityId string
param keyVaultUri string
// Only Key Vault secret names, never values.
#disable-next-line secure-secrets-in-params
param secretCatalog object
param imageRefs object
param registryServer string

@description('Deployment parameter values keyed by config/platform.json parameter name.')
@secure()
param parameterValues object

var initContainers = job.?initContainers ?? []
var secretKeys = referencedSecrets(concat(initContainers, job.containers))

resource containerAppsJob 'Microsoft.App/jobs@2025-01-01' = {
  name: job.azureName
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
      triggerType: 'Schedule'
      replicaTimeout: job.replicaTimeout
      replicaRetryLimit: job.replicaRetryLimit
      scheduleTriggerConfig: {
        cronExpression: job.schedule.cronExpression
        parallelism: job.schedule.parallelism
        replicaCompletionCount: job.schedule.replicaCompletionCount
      }
      // Pull with the environment's system-assigned identity (AcrPull on the registry).
      registries: [
        {
          server: registryServer
          identity: 'system-environment'
        }
      ]
      secrets: keyVaultSecrets(secretKeys, secretCatalog, keyVaultUri, identityId)
    }
    template: {
      initContainers: empty(initContainers) ? null : map(initContainers, container => initContainer(container, imageRefs, environmentName, parameterValues))
      containers: map(job.containers, container => appContainer(container, imageRefs, environmentName, parameterValues))
      volumes: azureFileVolumes(job.volumes)
    }
  }
}

output name string = containerAppsJob.name
