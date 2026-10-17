param location string
param name string

// Secret values are managed outside Bicep (see azure/scripts/secrets.sh); workloads
// consume them through Key Vault references with managed identities.
resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
    enabledForDeployment: true
    enabledForDiskEncryption: true
    enabledForTemplateDeployment: true
    publicNetworkAccess: 'Enabled'
    accessPolicies: []
  }
}

output name string = vault.name
output id string = vault.id
output vaultUri string = vault.properties.vaultUri
