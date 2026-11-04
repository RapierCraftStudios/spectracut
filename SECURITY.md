# Security Policy

## Supported versions

SpectraCut is pre-release. Security fixes land on `main` and ship in the next release.

## Reporting a vulnerability

Please do not open a public issue. Report privately through GitHub's [private vulnerability reporting](https://github.com/RapierCraftStudios/SpectraCut/security/advisories/new) for this repository.

Include what you found, how to reproduce it and the impact you expect. We aim to acknowledge reports within 3 working days and to agree a disclosure date with you once a fix is ready.

## Scope

Of particular interest:

- render hosts executing anything other than declarative recipes (for example network-supplied Python);
- secrets leaking into specs, recipes, logs or renders;
- tenant isolation in the hosted service.
