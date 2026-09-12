# Dataset Card: iShop (Drake) Multilingual Shopping Benchmark

## Dataset Summary

The iShop evaluation benchmark evaluates voice shopping assistants on code-switched Nigerian Pidgin (PCM)–English and Yoruba–English audio against real Shopify Storefront commerce fixtures. It tests whether automatic speech recognition errors, language switching, and conversational corrections propagate into erroneous cart mutations or whether the system's deterministic policy layer safely intercepts and clarifies them.

---

## 1. Data Tracks & Structure

| Track Name | Source / Nature | Target Scale | Licensing / Status |
|---|---|---|---|
| **Original iShop Shopping Speech** | Natural scenario-driven speech from consented native speakers | 12 speakers / 120 episodes (Pilot target); 30 dev, 90 test | Consented under three-tier agreement (local, provider, release). Private storage. |
| **AfriSwitch External Sample** | Stratified subset from Intron Health's AfriSwitch corpus | 400 clips (200 Yoruba, 200 Pidgin), stratified by duration | CC BY-NC-SA 4.0; gated access. Preserves upstream clip IDs. |
| **Synthetic Harness Fixtures** | Non-personal synthetic regression cases for offline testing | 6 multi-condition synthetic episodes | Internal test fixtures; zero external rights constraints. |

---

## 2. Natural vs. Scripted vs. Synthetic Disclosures

- **Natural Speech:** The primary benchmark consists of native speakers responding to scenario goals naturally in their own words. Speakers are given goals (e.g. *"You want to purchase a red cotton shirt in size small, but make sure you don't buy the blue one"*) rather than artificial pre-scripted sentences.
- **No Synthetic Substitution:** Synthetic text-to-speech audio is strictly prohibited from masquerading as natural speaker evidence. Synthetic fixtures are labelled as offline harness regression checks only.

---

## 3. Splits & Speaker Disjointness

- **Split Protocol:** Development and held-out test splits are strictly speaker-disjoint for original iShop recordings:
  - Development Split: 3 speakers (approx. 30 episodes).
  - Held-out Test Split: 9 speakers (approx. 90 episodes).
- **AfriSwitch Attribution Limitation:** The external AfriSwitch corpus does not provide verified speaker identifiers. Consequently, AfriSwitch evaluations utilize clip-level resampling and transparently disclose the absence of speaker-level clustering in all reports (**T-26**).

---

## 4. Consent, Retention & Privacy Safeguards

- **Consent Separation (Safety S-12 / T-23):**
  - Permission A: Local research and offline evaluation (max 30 days retention).
  - Permission B: Processing by the selected external cloud providers (Sahara, Groq, AssemblyAI, ElevenLabs and Google Gemini).
  - Permission C: Public dataset release under open-access research license.
- **Data Exclusion:**
  - Raw audio recordings, identity registers, and personal phone/email information are strictly excluded from Git tracking via `.gitignore` and audited by `scripts/check-secrets.py`.
  - Stored references utilize keyed HMAC hashes and opaque identifiers (`spk_xxx`, `ep_xxx`).

---

## 5. Known Limitations & Biases

1. **Pilot Window Uncertainty:** The 12-speaker pilot provides directional confidence intervals; statistical conclusions must remain modest until scaled.
2. **Language Assumptions:** Nigerian Pidgin and Yoruba represent initial proof languages; additional Nigerian and African languages remain future work.
3. **No Payment Data:** Scenarios terminate at the checkout handoff boundary (Safety S-01, S-02); no payment cards or customer addresses are ever recorded.
