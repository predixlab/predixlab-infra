param location string

@description('Static site definition from config/platform.json (staticSites[]).')
param site object

// Content is published by each repository's Azure/static-web-apps-deploy workflow
// with the deployment token; this template only owns the resource and domains.
// Creating a brand-new site with repository linking additionally needs a GitHub
// token (repositoryToken), which is intentionally not part of this template.
resource staticSite 'Microsoft.Web/staticSites@2024-04-01' = {
  name: site.azureName
  location: location
  sku: {
    name: site.sku
    tier: site.sku
  }
  properties: {
    repositoryUrl: site.repositoryUrl
    branch: site.branch
    provider: 'GitHub'
    stagingEnvironmentPolicy: 'Enabled'
    allowConfigFileUpdates: true
    enterpriseGradeCdnStatus: 'Disabled'
  }
}

// DNS validation (CNAME for sub-domains, TXT for the apex) is a one-time manual step.
@batchSize(1)
resource customDomains 'Microsoft.Web/staticSites/customDomains@2024-04-01' = [
  for domain in site.customDomains: {
    parent: staticSite
    name: domain
    properties: {}
  }
]

output defaultHostname string = staticSite.properties.defaultHostname
