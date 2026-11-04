# Changelog

All notable changes to SpectraCut are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). SpectraCut is licensed under
AGPL-3.0-or-later.

## [Unreleased]

### Added

- Release pipeline (`.github/workflows/release.yml`). Tagging `vX.Y.Z` builds the
  `spectracut` and `spectracut-host` wheels, the `@spectracut/capture` npm package
  and the `spectracut-api` and `spectracut-host` images on GHCR. Everything is a dry
  run until the repository variable `PUBLISH_ENABLED` is set to `true` (see #9).
- `spectracut --version`.
