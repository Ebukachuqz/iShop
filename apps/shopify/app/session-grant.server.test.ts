import { describe, expect, test } from "vitest";

import {
  createSessionGrant,
  signatureMatches,
  signGrantPayload,
  createResumeReference,
  verifyResumeReference,
} from "./session-grant.server";

describe("session grants", () => {
  test("matches the runtime wire contract", () => {
    const secret = "a-development-secret-with-32-characters";
    const grant = createSessionGrant({
      shop: "example.myshopify.com",
      origin: "https://shop.example.com",
      signingSecret: secret,
      nowMs: 1_000,
      ttlMs: 300_000,
      asrProfileId: "sahara-stream-pcm",
      llmProfileId: "groq-gpt-oss-120b",
      ttsProfileId: "sahara-tts-female-pcm",
    });

    expect(grant.shop_id).toBe("example.myshopify.com");
    expect(grant.permitted_origin).toBe("https://shop.example.com");
    expect(grant.expires_at_ms).toBe(301_000);
    expect(signatureMatches(grant, secret)).toBe(true);
  });

  test("rejects weak signing secrets", () => {
    expect(() =>
      createSessionGrant({
        shop: "example.myshopify.com",
        origin: "https://example.myshopify.com",
        signingSecret: "short",
        asrProfileId: "sahara-stream-pcm",
        llmProfileId: "groq-gpt-oss-120b",
        ttsProfileId: "sahara-tts-female-pcm",
      }),
    ).toThrow(/32 characters/);
  });

  test("uses the same HMAC serialization as the Python runtime", () => {
    const signature = signGrantPayload(
      {
        grant_id: "grant_test",
        shop_id: "example.myshopify.com",
        permitted_origin: "https://shop.example.com",
        anonymous_session_id: "sess_1234567890abcdef",
        config_revision: "default-v1",
        asr_profile_id: "sahara-stream-pcm",
        llm_profile_id: "groq-gpt-oss-120b",
        tts_profile_id: "sahara-tts-female-pcm",
        issued_at_ms: 1_000,
        expires_at_ms: 301_000,
      },
      "a-development-secret-with-32-characters",
    );

    expect(signature).toBe(
      "d1bc2a29189b2e6e25e1bfecb4bbb0732cb540a5750d624f101ea093f9c83064",
    );
  });

  test("resumes only from a valid signed reference", () => {
    const secret = "a-development-secret-with-32-characters";
    const grant = createSessionGrant({ shop: "example.myshopify.com", origin: "https://shop.example.com",
      signingSecret: secret, nowMs: Date.now(), asrProfileId: "asr", llmProfileId: "llm", ttsProfileId: "tts" });
    const reference = createResumeReference(grant, secret);
    expect(verifyResumeReference(reference, secret)?.session_id).toBe(grant.anonymous_session_id);
    expect(verifyResumeReference(`${reference}x`, secret)).toBeNull();
  });
});
