# Public readiness

ulanzi is a Linux D200 project at version 0.1.0. Source availability does not
imply qualification across Linux distributions, D200 units or firmware versions.

## Licensing and distribution

The project uses [Apache-2.0](../LICENSE). [NOTICE](../NOTICE) records project
authorship, and [THIRD_PARTY_LICENSES.txt](../THIRD_PARTY_LICENSES.txt) preserves
MIT copyright and permission notices for the upstream protocol references.
All three files are explicitly included in Python distribution metadata.
The optional Git-pinned framing reference is separate from runtime dependencies.

## Documentation and support

- [README](../README.md): install, permissions, configuration, service setup,
  recovery and removal.
- [Agent setup](agents.md): supported events, targeting and hook templates.
- [Hardware observations](qualification/README.md): measured behavior and
  incomplete checks, including the two-device recovery limitation.
- [Contributing](../CONTRIBUTING.md): hardware-free tests and package checks.
- [Confidential reports](../SECURITY.md): reporting policy and execution scope.

## Publication hygiene

The public source excludes raw hardware receipts and unrelated working notes.
Examples use placeholders; device identities and user configuration belong in
local files. Preserve legitimate author and third-party license attribution.
Ignore rules reduce accidental staging but do not remove already committed data.

Before publishing a revision, scan the complete history being published and the
built archives for credentials and private machine details. Inspect all remote
branches/tags, issues/comments, releases/assets, Actions logs/artifacts/caches,
wiki/Pages and repository settings. A clean working tree does not sanitize older
commits. Keep any original private evidence separate from public Git ancestry.

## Remaining hardware limits

Unit tests use fake transports and isolated local sockets. They cannot prove
physical rendering, input, unplug recovery or native agent hook delivery. The
recorded two-device cable test did not pass; no clean multi-device acceptance is
claimed. Hook fixtures are not proof of every native client event sequence.
Fresh-system installation and physical checks should be repeated on each
supported environment before making broader support claims.
