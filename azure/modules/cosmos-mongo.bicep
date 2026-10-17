param location string
param name string
param databaseName string

// Serverless Cosmos DB for MongoDB (RU). Collections and indexes are owned by the
// applications (the API creates its indexes at startup), so only the database is
// declared here.
resource account 'Microsoft.DocumentDB/databaseAccounts@2024-11-15' = {
  name: name
  location: location
  kind: 'MongoDB'
  tags: {
    defaultExperience: 'Azure Cosmos DB for MongoDB API'
    'hidden-cosmos-mmspecial': ''
    'hidden-workload-type': 'Development/Testing'
  }
  properties: {
    databaseAccountOfferType: 'Standard'
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    capabilities: [
      {
        name: 'EnableMongo'
      }
      {
        name: 'EnableServerless'
      }
    ]
    apiProperties: {
      serverVersion: '7.0'
    }
    capacity: {
      totalThroughputLimit: 4000
    }
    backupPolicy: {
      type: 'Periodic'
      periodicModeProperties: {
        backupIntervalInMinutes: 240
        backupRetentionIntervalInHours: 8
        backupStorageRedundancy: 'Geo'
      }
    }
    enableAutomaticFailover: true
    enableMultipleWriteLocations: false
    enableAnalyticalStorage: false
    enableFreeTier: false
    disableLocalAuth: false
    isVirtualNetworkFilterEnabled: false
    publicNetworkAccess: 'Enabled'
    minimalTlsVersion: 'Tls12'
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/mongodbDatabases@2024-11-15' = {
  parent: account
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

output name string = account.name
