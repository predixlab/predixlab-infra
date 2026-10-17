// Role assignments required by the platform. Only deployed when
// deployRoleAssignments = true (fresh environments): the production resource
// group already holds equivalent assignments under different names.
param keyVaultName string
param registryName string

@description('Principal IDs of the user-assigned identities: [{ key, principalId }].')
param identityPrincipals array

@description('Principal IDs of the Container Apps environments (system-assigned identities).')
param environmentPrincipalIds array

param functionAppName string

@description('GitHub deployment service principal for the Function App; empty to skip.')
@secure()
param campaignDeployPrincipalId string = ''

var roles = {
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  keyVaultCertificateUser: 'db79e9a7-68ee-4b58-9aeb-b90e7c24fcba'
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'
  websiteContributor: 'de139f84-1756-47ae-9be6-808fbbe84772'
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' existing = {
  name: functionAppName
}

// Workload identities read Key Vault secrets (Container Apps Key Vault references).
resource secretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for identity in identityPrincipals: {
    name: guid(vault.id, identity.principalId, roles.keyVaultSecretsUser)
    scope: vault
    properties: {
      principalId: identity.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    }
  }
]

resource certificateUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for identity in identityPrincipals: {
    name: guid(vault.id, identity.principalId, roles.keyVaultCertificateUser)
    scope: vault
    properties: {
      principalId: identity.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultCertificateUser)
    }
  }
]

// Environments pull job images with their system-assigned identity.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in environmentPrincipalIds: {
    name: guid(registry.id, principalId, roles.acrPull)
    scope: registry
    properties: {
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    }
  }
]

// The API identity is also the GitHub OIDC deployment identity of predixlab-api.
resource apiDeployer 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for identity in filter(identityPrincipals, identity => identity.key == 'api'): {
    name: guid(resourceGroup().id, identity.principalId, roles.contributor)
    properties: {
      principalId: identity.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
    }
  }
]

resource campaignDeployer 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(campaignDeployPrincipalId)) {
  name: guid(functionApp.id, campaignDeployPrincipalId, roles.websiteContributor)
  scope: functionApp
  properties: {
    principalId: campaignDeployPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.websiteContributor)
  }
}
