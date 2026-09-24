# Security Policy

## Supported versions

Only the latest release on [PyPI](https://pypi.org/project/gpt-oss-azure-opencode-shim/) receives fixes.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub:
**Security → Report a vulnerability** on this repository
(<https://github.com/Gabrielm3/gpt-oss-azure-opencode-shim/security/advisories/new>).
Do not open a public issue.

Include the version, your configuration (without the API key), and steps to
reproduce. The maintainer aims to reply within 7 days.

## Scope

The shim holds an Azure API key and adds it to every request it forwards, so
these are in scope:

- Any way for a non-local client to reach the shim or make it forward a request
  (Host checks, browser requests, DNS rebinding).
- Leaking the API key or the upstream host through responses, logs, metrics,
  traces or OpenTelemetry spans.
- Recorded traces or outcome logs containing prompt content.

Binding the shim to a non-loopback address (`SHIM_HOST`) removes the local-only
guarantee; the shim warns about it at startup, and that setup is out of scope.

## Release integrity

Releases are built in GitHub Actions and published with PyPI trusted
publishing. Every file on PyPI carries a PEP 740 attestation linking it to the
workflow run that built it, and the same files are attached to the GitHub release.
