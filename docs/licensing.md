# Licensing

SpectraCut is dual-licensed:

- **Open source:** [AGPL-3.0-or-later](../LICENSE), free for everyone.
- **Commercial:** a separate agreement for organisations that cannot meet the AGPL's conditions. See [COMMERCIAL.md](../COMMERCIAL.md).

This page is a plain-language guide, not legal advice. The licence text governs.

## Self-hosters

If you run SpectraCut on your own machines or render hosts:

- **Internal use, unmodified or modified:** allowed, with no obligation to publish anything, as long as you do not convey the software to others or let others interact with it over a network.
- **Distributing it** (binaries, containers, images, a bundled appliance): you must pass on the AGPL, keep copyright and SPDX notices, and provide the Corresponding Source of your version, including your modifications.
- Keep the `SPDX-License-Identifier: AGPL-3.0-or-later` headers in the files you ship.

## SaaS and network operators

AGPL section 13 adds one rule to the GPL: if you **modify** SpectraCut and let users interact with it remotely over a network (for example a hosted `spectracut serve`, or a prompt-to-video service built on it), you must offer those users the Corresponding Source of your modified version, for example with a visible link.

Running an unmodified SpectraCut as a service has no extra source obligation beyond the AGPL's ordinary terms, but you must not remove the notices. If you do not want to publish your modifications, buy a commercial licence.

## What the licence does not cover

- **Your videos.** Output you generate is yours. Brand kits and customer assets are not part of the codebase.
- **Third-party tools and models.** They run as separate programs and keep their own licences.
- **Media licences.** Voices, music, sound and imagery carry their own licence, recorded per asset. "Commercial-safe" describes media, not the code licence.
- **Trademarks.** The licence does not grant rights to the SpectraCut name or logos.

## Buying a commercial licence

Commercial licences are sold off-platform. See [COMMERCIAL.md](../COMMERCIAL.md) for what they grant and how to start a request. The terms there are a draft until approved by the maintainers.

## Contributions and the CLA

Every contribution must be covered by the [CLA](../CLA.md), which lets the project offer both licences. A check on each pull request looks for your signature. Sign by commenting this on the pull request:

> I have read the CLA Document and I hereby sign the CLA

Until the maintainers finish the CLA bot setup and trademark clearance (issue #10), the check runs in report-only mode: it warns about unsigned authors but does not block merging. Once enforcement is on, a pull request from an unsigned contributor is blocked.
