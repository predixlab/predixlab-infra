# Contributing to PredixLab Infrastructure

Thank you for contributing. Start with [README](README.md) for the project's
scope, configuration, and operating limitations.

## Before making a change

- Use [GitHub Issues](https://github.com/predixlab/predixlab-infra/issues) for reproducible bugs and focused proposals.
  Discuss substantial interface, schema, or workflow changes before implementing them.
- Report vulnerabilities privately using [SECURITY.md](SECURITY.md), not a public issue.
- Fork the repository and work on a focused branch. Keep unrelated fixes separate.

## Development setup

Requirements: Python 3.9+ for tooling tests; local staging additionally needs the macOS/Apple container prerequisites in README.

From the repository root:

```bash
python3 --version
```

Follow README for optional services and local configuration. Use tracked examples
only as templates; keep real credentials and environment files outside Git.

## Validation

Run the smallest relevant checks while developing, then run the applicable project checks:

```bash
python3 -m unittest discover -s tests -v
bash -n azure/scripts/deploy.sh
sh -n plx
sh -n local/images/static-site/rewrite-origins.sh
```

Azure changes also require `az bicep build --file azure/main.bicep --stdout > /dev/null` and `az bicep build-params --file azure/parameters/prod.bicepparam --stdout > /dev/null` with the existing Bicep tooling. Do not deploy, seed production data, create resources, or change cloud permissions as part of routine validation.

## Pull requests

Validation runs on pushes, pull requests, and manual dispatches. The existing
Python 3.9/3.12 matrix is preserved, and Bicep is installed from its current
stable release. The separate Azure deployment workflow is manual; routine CI
validation does not authorize an infrastructure deployment.

- Describe the problem, intended behavior, and relevant issue.
- Follow existing project conventions; avoid unrelated refactors and dependency changes.
- Add focused regression tests for behavior changes and update public documentation in English.
- Include the commands run and their results; state any validation that could not run.
- Explain compatibility, data migration, configuration, and deployment consequences.
- Include screenshots for visible UI changes, with no customer or personal information.
- Do not include secrets, private endpoints, customer records, personal paths, or session
  profiles in source, fixtures, screenshots, logs, or generated artifacts. Review commit
  metadata and use a privacy-preserving identity if needed.

Be respectful and constructive. Harassment and disclosure of another person's
private information are not acceptable.

## Licensing

Contributions are submitted under the project's [Apache License 2.0](LICENSE).
Preserve copyright notices, third-party licenses, and source attribution. Do not
submit code, datasets, assets, or model files you do not have permission to share.
