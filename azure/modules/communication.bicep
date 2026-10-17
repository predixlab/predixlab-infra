param communicationServiceName string
param emailServiceName string

@description('Customer-managed sending domain; DNS records live with the domain registrar.')
param emailDomainName string

@description('Sender usernames: [{ name, username, displayName }].')
param senders array

resource emailService 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: emailServiceName
  location: 'global'
  properties: {
    dataLocation: 'United States'
  }
}

resource domain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: emailService
  name: emailDomainName
  location: 'global'
  properties: {
    domainManagement: 'CustomerManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource senderUsernames 'Microsoft.Communication/emailServices/domains/senderUsernames@2023-04-01' = [
  for sender in senders: {
    parent: domain
    name: sender.name
    properties: {
      username: sender.username
      displayName: sender.displayName
    }
  }
]

resource communicationService 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: communicationServiceName
  location: 'global'
  properties: {
    dataLocation: 'United States'
    linkedDomains: [
      domain.id
    ]
  }
}

output communicationServiceName string = communicationService.name
