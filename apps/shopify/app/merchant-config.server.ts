import { randomUUID } from "node:crypto";

import db from "./db.server";
import { normalizeProfileSelection, validateProfileSelection, type ProviderRole } from "./provider-profiles.server";

export const DEFAULT_SELECTION: Record<ProviderRole, string> = {
  asr: "sahara-stream-pcm",
  llm: "groq-gpt-oss-120b",
  tts: "sahara-tts-female-pcm",
};

export class StaleConfigurationError extends Error {}
export class InvalidConfigurationError extends Error {
  constructor(public errors: Partial<Record<ProviderRole, string>>) {
    super("Provider configuration is invalid");
  }
}

export async function getMerchantConfiguration(shop: string) {
  const existing = await db.merchantConfiguration.findUnique({ where: { shop } });
  if (existing) return existing;
  const revision = randomUUID();
  return db.$transaction(async (transaction) => {
    const created = await transaction.merchantConfiguration.create({
      data: {
        shop,
        revision,
        asrProfileId: DEFAULT_SELECTION.asr,
        llmProfileId: DEFAULT_SELECTION.llm,
        ttsProfileId: DEFAULT_SELECTION.tts,
      },
    });
    await transaction.merchantConfigurationRevision.create({
      data: {
        shop: created.shop,
        revision: created.revision,
        asrProfileId: created.asrProfileId,
        llmProfileId: created.llmProfileId,
        ttsProfileId: created.ttsProfileId,
      },
    });
    return created;
  });
}

export async function saveMerchantConfiguration(input: {
  shop: string;
  expectedRevision: string;
  selection: Record<ProviderRole, string>;
}) {
  const selection = normalizeProfileSelection(input.selection);
  const errors = validateProfileSelection(selection);
  if (Object.keys(errors).length) throw new InvalidConfigurationError(errors);
  const revision = randomUUID();
  return db.$transaction(async (transaction) => {
    const changed = await transaction.merchantConfiguration.updateMany({
      where: { shop: input.shop, revision: input.expectedRevision },
      data: {
        revision,
        asrProfileId: selection.asr,
        llmProfileId: selection.llm,
        ttsProfileId: selection.tts,
      },
    });
    if (changed.count !== 1) throw new StaleConfigurationError("Settings changed in another session");
    await transaction.merchantConfigurationRevision.create({
      data: {
        shop: input.shop,
        revision,
        asrProfileId: selection.asr,
        llmProfileId: selection.llm,
        ttsProfileId: selection.tts,
      },
    });
    return transaction.merchantConfiguration.findUniqueOrThrow({ where: { shop: input.shop } });
  });
}

export async function getConfigurationSnapshot(shop: string) {
  const configuration = await getMerchantConfiguration(shop);
  const selection = normalizeProfileSelection({
    asr: configuration.asrProfileId,
    llm: configuration.llmProfileId,
    tts: configuration.ttsProfileId,
  });
  const errors = validateProfileSelection(selection);
  if (Object.keys(errors).length) throw new InvalidConfigurationError(errors);
  return { revision: configuration.revision, selection };
}
