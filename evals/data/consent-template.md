# Participant Consent Template and Data Rights Agreement

**Study / Project:** iShop (Drake) Multilingual Shopping Assistant Research  
**Date Established:** 12 September 2026  
**Governing Policies:** Safety S-12, Testing T-23, Evaluation Specifications

---

## 1. Scope and Separate Permissions (Safety S-12)

Participant consent must strictly decouple three distinct permissions. Granting permission for local research does not grant permission for third-party commercial provider transmission or public release.

1. **Permission A: Local Recording & Internal Research**
   - Audio and scenario interaction recorded locally on isolated encrypted storage.
   - Used solely for evaluating offline speech recognition and shopping dialogue correctness.
   - Retention limit: maximum 30 days from collection date, followed by verifiable deletion.

2. **Permission B: Third-Party Provider Processing (ASR & LLM)**
   - Transmission of anonymized/pseudonymous audio or transcript text to evaluated external cloud providers:
     - Sahara (Intron Health)
     - Mansa (African Languages Lab)
     - OpenAI (gpt-live-transcribe)
     - ElevenLabs (scribe_v2_realtime)
     - Google Cloud (Speech-to-Text V2 / Chirp 3)
     - Google Gemini / Groq (transcript intent interpretation)
   - Subject to vendor retention and training terms (Q-06). Unpaid developer tiers that permit human review or model training are excluded from receiving unconsented audio.

3. **Permission C: Public Open Research Dataset Release**
   - Release of pseudonymous audio and verified transcripts as part of an open-access research benchmark dataset (e.g. under CC BY-NC-SA 4.0).
   - Requires explicit separate opting in. Participants may withhold public release rights while granting local research rights.

---

## 2. Participant Privacy Protections

- **Pseudonymization:** All audio recordings, transcripts, and metadata are tagged with opaque identifiers (`spk_xxx`, `ep_xxx`). No personal names, phone numbers, payment details, or contact information are stored with the data.
- **Strict No-Payment Guarantee (Safety S-01):** Scenarios simulate browsing and cart additions only. No real payment, credit card information, or delivery address will ever be collected or requested.
- **Right of Revocation & Deletion:** Participants may request deletion of their recordings and data at any time prior to dataset publication by referencing their private participant token.

---

## 3. Consent Record Structure

Each consented session must log a non-personal metadata entry in `data/private/consent_log.json` (untracked in Git) with:
```json
{
  "consent_id": "cst_sha256_hash",
  "speaker_id": "spk_001",
  "date_utc": "2026-09-12T10:00:00Z",
  "permission_local_research": true,
  "permission_third_party_providers": true,
  "permission_public_release": false,
  "allowed_processors": ["local", "simulator", "sahara", "mansa", "gemini"],
  "retention_expiry_utc": "2026-10-12T10:00:00Z"
}
```
Episodes without `permission_third_party_providers: true` are blocked by the evaluation runner from uploading to remote providers (T-23).
