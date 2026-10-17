// Pure helpers that translate config/platform.json workload entries into ARM shapes.
//
// Env entry forms (platform.json):
//   { name, value }                      same value in every environment
//   { name, secret }                     secret catalog key, resolved per environment
//   { name, parameter }                  parameter catalog key; the deployment supplies the value
//   { name, values: { <env>: value } }   environment-specific; omitted where absent

@export()
@description('Returns true when the env entry applies to the given environment.')
func appliesTo(entry object, environmentName string) bool =>
  contains(entry, 'secret') || contains(entry, 'value') || contains(entry, 'parameter') || contains(entry.?values ?? {}, environmentName)

@export()
@description('Plain (non-secret) value of an env entry; parameter entries read the deployment parameter values.')
func envValue(entry object, environmentName string, parameterValues object) string =>
  contains(entry, 'parameter') ? parameterValues[entry.parameter] : (entry.?value ?? entry.values[environmentName])

@export()
@description('Converts platform env entries into Container Apps env vars (secrets become secretRef).')
func containerEnv(entries array, environmentName string, parameterValues object) array =>
  map(
    filter(entries, entry => appliesTo(entry, environmentName)),
    entry =>
      contains(entry, 'secret')
        ? {
            name: entry.name
            secretRef: entry.secret
          }
        : {
            name: entry.name
            value: envValue(entry, environmentName, parameterValues)
          }
  )

@export()
@description('Sorted, de-duplicated secret catalog keys referenced by the given containers.')
func referencedSecrets(containers array) array =>
  sort(
    union(flatten(map(containers, container => map(filter(container.env, entry => contains(entry, 'secret')), entry => entry.secret))), []),
    (left, right) => left < right
  )

@export()
@description('Container Apps secret definitions backed by Key Vault references.')
func keyVaultSecrets(secretKeys array, secretCatalog object, keyVaultUri string, identityId string) array =>
  map(secretKeys, key => {
    name: key
    keyVaultUrl: '${keyVaultUri}secrets/${secretCatalog[key].keyVaultSecret}'
    identity: identityId
  })

@export()
func resources(spec object) object =>
  union(
    {
      cpu: json(spec.cpu)
      memory: spec.memory
    },
    contains(spec, 'ephemeralStorage') ? { ephemeralStorage: spec.ephemeralStorage } : {}
  )

@export()
@description('Main container definition for a Container App or Job.')
func appContainer(spec object, imageRefs object, environmentName string, parameterValues object) object => {
  name: spec.name
  image: imageRefs[spec.image]
  command: spec.?command ?? []
  args: spec.?args ?? []
  env: containerEnv(spec.env, environmentName, parameterValues)
  resources: resources(spec)
  volumeMounts: spec.?volumeMounts ?? []
  probes: spec.?probes ?? []
}

@export()
@description('Init container definition (init containers do not support probes).')
func initContainer(spec object, imageRefs object, environmentName string, parameterValues object) object => {
  name: spec.name
  image: imageRefs[spec.image]
  command: spec.?command ?? []
  args: spec.?args ?? []
  env: containerEnv(spec.env, environmentName, parameterValues)
  resources: resources(spec)
  volumeMounts: spec.?volumeMounts ?? []
}

@export()
@description('Azure Files volumes for a Container App or Job.')
func azureFileVolumes(volumes array) array =>
  map(volumes, volume => {
    name: volume.name
    storageName: volume.storageName
    storageType: 'AzureFile'
  })
