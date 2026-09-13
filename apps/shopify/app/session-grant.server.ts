import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";

export type SessionGrant = {
  schema_version: "1.1.0";
  grant_id: string;
  shop_id: string;
  permitted_origin: string;
  anonymous_session_id: string;
  config_revision: string;
  asr_profile_id: string;
  llm_profile_id: string;
  tts_profile_id: string;
  issued_at_ms: number;
  expires_at_ms: number;
  signature: string;
};

type ResumePayload = {
  session_id: string;
  shop_id: string;
  permitted_origin: string;
  config_revision: string;
  expires_at_ms: number;
};

const grantMessage = (grant: Omit<SessionGrant, "schema_version" | "signature">) =>
  [
    grant.grant_id,
    grant.shop_id,
    grant.permitted_origin,
    grant.anonymous_session_id,
    grant.config_revision,
    grant.asr_profile_id,
    grant.llm_profile_id,
    grant.tts_profile_id,
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
  asrProfileId: string;
  llmProfileId: string;
  ttsProfileId: string;
  anonymousSessionId?: string;
}): SessionGrant {
  if (input.signingSecret.length < 32) {
    throw new Error("SESSION_SIGNING_SECRET must contain at least 32 characters");
  }

  const issuedAt = input.nowMs ?? Date.now();
  const unsigned = {
    grant_id: `grant_${randomUUID()}`,
    shop_id: input.shop,
    permitted_origin: input.origin,
    anonymous_session_id: input.anonymousSessionId ?? `sess_${randomUUID().replaceAll("-", "")}`,
    config_revision: input.configRevision ?? "default-v1",
    asr_profile_id: input.asrProfileId,
    llm_profile_id: input.llmProfileId,
    tts_profile_id: input.ttsProfileId,
    issued_at_ms: issuedAt,
    expires_at_ms: issuedAt + (input.ttlMs ?? 5 * 60 * 1000),
  };

  return {
    schema_version: "1.1.0",
    ...unsigned,
    signature: signGrantPayload(unsigned, input.signingSecret),
  };
}

export function createResumeReference(grant: SessionGrant, signingSecret: string, ttlMs = 30 * 60 * 1000) {
  const payload: ResumePayload = {
    session_id: grant.anonymous_session_id,
    shop_id: grant.shop_id,
    permitted_origin: grant.permitted_origin,
    config_revision: grant.config_revision,
    expires_at_ms: Math.min(Date.now() + ttlMs, grant.issued_at_ms + ttlMs),
  };
  const encoded = Buffer.from(JSON.stringify(payload)).toString("base64url");
  const signature = createHmac("sha256", signingSecret).update(encoded).digest("base64url");
  return `${encoded}.${signature}`;
}

export function verifyResumeReference(reference: string, signingSecret: string, nowMs = Date.now()): ResumePayload | null {
  const [encoded, supplied, extra] = reference.split(".");
  if (!encoded || !supplied || extra) return null;
  const expected = createHmac("sha256", signingSecret).update(encoded).digest("base64url");
  const suppliedBytes = Buffer.from(supplied);
  const expectedBytes = Buffer.from(expected);
  if (suppliedBytes.length !== expectedBytes.length || !timingSafeEqual(suppliedBytes, expectedBytes)) return null;
  try {
    const payload = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8")) as ResumePayload;
    if (!payload.session_id.startsWith("sess_") || payload.expires_at_ms < nowMs) return null;
    return payload;
  } catch {
    return null;
  }
}

export function signatureMatches(grant: SessionGrant, signingSecret: string) {
  const expected = signGrantPayload(grant, signingSecret);
  return timingSafeEqual(Buffer.from(grant.signature), Buffer.from(expected));
}
