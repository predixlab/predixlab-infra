# Security policy

## Supported code

Security fixes currently target the latest `main` branch. No maintained historical
release series, response-time guarantee, or bounty program is promised by this policy.

## Report a vulnerability privately

Do not disclose exploit details, credentials, personal data, or customer records in
public issues, pull requests, or logs.

1. Open this repository's [Security tab](https://github.com/predixlab/predixlab-infra/security).
2. If **Report a vulnerability** is available, use it to submit a private report.
3. If private reporting is unavailable, open a minimal
   [issue](https://github.com/predixlab/predixlab-infra/issues) requesting a private reporting channel. Include no
   vulnerability details or sensitive attachments; wait for a private channel
   before sharing them.

Include the affected revision, component, impact, prerequisites, and minimal
reproduction using synthetic data. Redact secrets and identifiers. Do not test
against hosted PredixLab services or third-party systems without explicit permission.

## Credentials and publication

- Supply secrets through ignored local configuration or a deployment secret manager.
- Never commit live credentials, browser profiles, cookies, private keys, personal
  identifiers, database exports, or production logs.
- Use independent, high-entropy signing keys and least-privilege service identities.
- Rotate exposed credentials at their provider and invalidate affected sessions.
  Deleting a file or rewriting Git history does not revoke a secret or remove every
  cached copy, fork, build artifact, or platform-retained object.
- Before publishing, review tracked files, Git author/committer metadata, repository
  history, examples, attachments, release artifacts, and CI logs. An automated scan
  is useful but is not a guarantee that no sensitive information remains.

## Operational scope

The source license does not grant access to hosted services, customer data, cloud
resources, or third-party accounts. Use isolated development resources and synthetic
data. Report an issue responsibly rather than accessing or modifying another
person's information.

See [README](README.md) for component-specific configuration and limitations and
[CONTRIBUTING.md](CONTRIBUTING.md) for safe development checks.
