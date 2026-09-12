import { describe, expect, test } from "vitest";

import {
  createSessionGrant,
  signatureMatches,
  signGrantPayload,
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
        issued_at_ms: 1_000,
        expires_at_ms: 301_000,
      },
      "a-development-secret-with-32-characters",
    );

    expect(signature).toBe(
      "3a40eb739c4613c22a1776ceb6a869ad5ce334fb8df5cdabf61dcdfbfcf4b490",
    );
  });
});
