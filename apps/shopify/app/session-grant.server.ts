import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";

export type SessionGrant = {
  schema_version: "1.0.0";
  grant_id: string;
  shop_id: string;
  permitted_origin: string;
  anonymous_session_id: string;
  config_revision: string;
  issued_at_ms: number;
  expires_at_ms: number;
  signature: string;
};

const grantMessage = (grant: Omit<SessionGrant, "schema_version" | "signature">) =>
  [
    grant.grant_id,
    grant.shop_id,
    grant.permitted_origin,
    grant.anonymous_session_id,
    grant.config_revision,
    grant.issued_at_ms,
    grant.expires_at_ms,
  ].join("|");

export function signGrantPayload(
  grant: Omit<SessionGrant, "schema_version" | "signature">,
  signingSecret: string,
) {
  return createHmac("sha256", signingSecret)
    .update(grantMessage(grant))
    .digest("hex");
}

export function createSessionGrant(input: {
  shop: string;
  origin: string;
  signingSecret: string;
  nowMs?: number;
  ttlMs?: number;
  configRevision?: string;
}): SessionGrant {
  if (input.signingSecret.length < 32) {
    throw new Error("SESSION_SIGNING_SECRET must contain at least 32 characters");
  }

  const issuedAt = input.nowMs ?? Date.now();
  const unsigned = {
    grant_id: `grant_${randomUUID()}`,
    shop_id: input.shop,
    permitted_origin: input.origin,
    anonymous_session_id: `sess_${randomUUID().replaceAll("-", "")}`,
    config_revision: input.configRevision ?? "default-v1",
    issued_at_ms: issuedAt,
    expires_at_ms: issuedAt + (input.ttlMs ?? 5 * 60 * 1000),
  };

  return {
    schema_version: "1.0.0",
    ...unsigned,
    signature: signGrantPayload(unsigned, input.signingSecret),
  };
}

export function signatureMatches(grant: SessionGrant, signingSecret: string) {
  const expected = signGrantPayload(grant, signingSecret);
  return timingSafeEqual(Buffer.from(grant.signature), Buffer.from(expected));
}
