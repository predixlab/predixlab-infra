param location string

@description('User-assigned identities keyed by logical name, e.g. { api: "<name>", jobs: "<name>" }.')
param identities object

@description('GitHub OIDC federated credentials: [{ identity, name, subject }].')
param githubFederatedCredentials array

resource userAssigned 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [
  for identity in items(identities): {
    name: identity.value
    location: location
  }
]

@batchSize(1)
resource federated 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = [
  for credential in githubFederatedCredentials: {
    name: '${identities[credential.identity]}/${credential.name}'
    properties: {
      issuer: 'https://token.actions.githubusercontent.com'
      subject: credential.subject
      audiences: [
        'api://AzureADTokenExchange'
      ]
    }
    dependsOn: [
      userAssigned
    ]
  }
]

output identities array = [
  for (identity, i) in items(identities): {
    key: identity.key
    id: userAssigned[i].id
    principalId: userAssigned[i].properties.principalId
    clientId: userAssigned[i].properties.clientId
  }
]
