# Annotation Guidelines and Adjudication Rubric

**Document Version:** 1.0.0  
**Application:** iShop (Drake) Evaluation Dataset Creation  
**Governing Specs:** [EVALUATION.md](../../docs/EVALUATION.md), [CONTRACTS.md](../../docs/CONTRACTS.md), [TESTING.md](../../docs/TESTING.md)

---

## 1. Transcription Standards

### Orthography and Unicode Integrity (T-25)
- **Yoruba & Nigerian English Code-Switching:**
  - Preserves tone marks and subdots using Unicode NFC normalization.
  - Vowels: `a, e, ẹ, i, o, ọ, u`. Tones: acute (high: `á, é, ẹ́, í, ó, ọ́, ú`), grave (low: `à, è, ẹ̀, ì, ò, ọ̀, ù`), unmarked (mid).
  - Consonants: `ṣ` (s with dot below), `gb`, `p`, etc.
  - Never replace tone marks or subdots with unaccented English characters in the reference transcript.
- **Nigerian Pidgin (PCM) & English Code-Switching:**
  - Standardized spelling for common markers: `abeg` (please), `wetin` (what), `dey` (be/progressive), `na` (is), `no` (negation), `dem` (plural/they).
  - Preserves contracted forms: `don't`, `i'm`, `make we`.
- **Verbatim Audio Fidelity:**
  - Transcribe exactly what was spoken, including hesitations, false starts, and repeated words.
  - Do not "correct" grammar or normalize slang into formal British/American English.
  - If speech is unintelligible, mark as `[unintelligible]`. If completely silent, leave transcript empty (`""`).

---

## 2. Critical Slot Annotation

Each scenario episode requires gold label extraction for critical intent slots:

| Slot | Allowed Values / Format | Example |
|---|---|---|
| `operation` | `search_catalog`, `get_product`, `show_variant`, `add_variant`, `set_line_quantity`, `remove_line`, `handoff_to_checkout` | `add_variant` |
| `product_id` | Valid fixture product ID (or `null` if discovery) | `prod_shirt` |
| `variant_id` | Exact resolved variant ID (or `null` if ambiguous) | `var_red_s` |
| `quantity_operation` | `increment`, `set`, `remove` | `increment` |
| `quantity` | Positive integer | `2` |
| `options` | Key-value dictionary of selected attributes | `{"color": "red", "size": "s"}` |
| `prohibited_actions` | Actions specifically forbidden by prompt/scenario | `["add_variant:var_blue_m"]` |
| `required_clarification`| Options that must be asked when ambiguous | `["size"]` |

---

## 3. Cart State and Policy Annotation

1. **Initial Cart State (`initial_cart_lines`):**
   - Declares preexisting lines in shopper cart at the start of the episode.
2. **Expected Final Cart State (`expected_cart_lines`):**
   - Canonical multiset of items that must be present in the authoritative cart after successful completion.
3. **Prohibited Intermediate Actions (`prohibited_actions`):**
   - Any mutation that, if executed during the interaction, violates the shopper's instructions (e.g. adding a rejected color before removing it, or adding unauthorized items).
   - An episode where a prohibited mutation is executed fails **Strict Task Success** even if the final cart matches FCEM (T-25).

---

## 4. Dual Reviewer Adjudication Protocol

1. **Independent Dual Annotation:**
   - Two native speakers independently transcribe and label critical slots for each episode.
2. **Dispute Resolution:**
   - Where annotations agree: automatically accepted into the frozen manifest.
   - Where annotators disagree on transcription or intent: a senior adjudicator reviews the original lossless audio and resolves the discrepancy.
   - If audio remains ambiguous after adjudication, the episode is either marked with `required_clarification` or excluded from held-out evaluation according to predefined protocol rules.
3. **Inter-Annotator Agreement Reporting:**
   - Calculate and report Cohen's kappa for critical slot agreement and transcript character agreement prior to freezing.
