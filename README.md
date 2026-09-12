# iShop (Drake)

iShop is a merchant-installed voice shopping assistant for Shopify storefronts. Shoppers open Drake from the bottom-right corner, describe what they want, and get help finding products and preparing the correct cart. Drake has a female voice. The shopper completes payment.

**Repository stage: execution design prepared; application implementation has not started.** The owner requested review of this execution system before coding. The current work and authorization state live in [STATUS](docs/plans/STATUS.md).

## Start here

1. Coding agents: read [AGENTS.md](AGENTS.md).
2. Reviewers: read [product behavior](docs/PRODUCT.md), [architecture](docs/ARCHITECTURE.md), then the [work sequence](docs/plans/README.md).
3. To resume work: open [STATUS](docs/plans/STATUS.md) and the next eligible work package.
4. For a specific topic: use the [documentation map](docs/INDEX.md).

The repository contains concrete specifications and work packages, not a running app. Commands described as planned must be implemented and verified in WP-00 before use. No package installation, app scaffolding, deployment, provider call or benchmark run was performed during documentation preparation.

## Intended shape

The widget captures speech; interchangeable speech adapters produce transcripts; a configurable LLM interprets the request; application code checks real Shopify evidence and controls actions; the resulting cart is read back before success is announced. Merchants select tested voice/LLM/TTS profiles. Evaluation follows that entire path.

The first deployment is one controlled Shopify Liquid storefront. Native WebMCP is a capability-tested enhancement, with Shopify storefront actions/Ajax as the browser cart fallback. Storefront APIs supply catalog evidence. There is one shopper cart.

## Evidence and scope

[Preserved research](docs/research/README.md) includes the prior reports, code audits and source snapshots. It is evidence, not a second active specification. The [challenge requirements](docs/CHALLENGE.md) record the organiser emails and rubric. The five-provider benchmark and unresolved access questions are recorded in the current specifications.

No project-wide software or dataset licence has been selected. Archived third-party material retains its original terms; it is not application code available for unrestricted copying.
