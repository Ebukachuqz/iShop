# Evaluation Scenario Matrix

**Document Version:** 1.0.0  
**Application:** iShop (Drake) Evaluation Dataset Scenarios  
**Governing Specs:** [EVALUATION.md](../../docs/EVALUATION.md), [TESTING.md](../../docs/TESTING.md)

---

## Scenario Distribution (Target: 120 Episodes)

| Scenario ID | Category | Description | Primary Test IDs | Expected Behavior |
|---|---|---|---|---|
| **SC-01** | Exact Selection | Direct command with all required options specified in code-switched speech | T-01, T-25 | Add single variant directly with verified receipt |
| **SC-02** | Incomplete Options | Product requested without required option (e.g. missing Size) | T-03, S-04 | Return CLARIFY; prompt for missing option; no silent default |
| **SC-03** | Quantity Increment | "Add two more" to preexisting item in cart | T-05 | Add quantity to existing line quantity (1 + 2 = 3) |
| **SC-04** | Quantity Set | "Make it two" to preexisting item in cart | T-05 | Set line quantity to exact target value (= 2) |
| **SC-05** | Negation Scope | "I want red shirt, don't add blue one" | T-06, T-25 | Add red variant; strictly prohibit adding blue variant |
| **SC-06** | Self-Correction | "Add medium... wait, make it large instead" | T-04, T-11 | Execute only final corrected operation (Large) |
| **SC-07** | Inventory Shortage | Requesting quantity greater than available stock or sold-out item | T-07, S-04 | Truthful rejection or shortage clarification; no false in-stock |
| **SC-08** | Unrelated Preservation | Adding new variant to cart with preexisting items | S-08, T-10 | Add new item while preserving initial lines exactly |
| **SC-09** | Distinct Variant Lines | Multiple lines sharing variant ID with different monogram/properties | T-10 | Mutate target line by key without altering sibling line |
| **SC-10** | Checkout Handoff | Explicit command to proceed to checkout | T-20, S-02 | Terminate assistant session; redirect to trusted checkout link |
| **SC-11** | Silence / Noise | Audio stream with background street noise or silence | T-24 | Empty hypothesis or graceful non-action; zero hallucinations |
| **SC-12** | Budget Scope | "Find items under ₦15,000" | T-02, T-25 | Discovery predicate matching verified NGN prices |
