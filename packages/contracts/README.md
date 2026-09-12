# @ishop/contracts

Domain, protocol, and trace contracts for iShop (Drake).

Authoritative specification: [CONTRACTS.md](../../docs/CONTRACTS.md).

## Ownership and Boundaries

- This package owns the language-neutral versioned JSON Schemas and fixtures for browser, runtime, and evaluator boundaries.
- It must **not** contain handwritten competing schema authorities.
- Full schemas, TypeScript type generators, and Python schema validators will be implemented in **WP-03**.
