import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const database = vi.hoisted(() => ({
  merchantConfiguration: {
    findUnique: vi.fn(),
    create: vi.fn(),
    updateMany: vi.fn(),
    findUniqueOrThrow: vi.fn(),
  },
  merchantConfigurationRevision: { create: vi.fn() },
  $transaction: vi.fn(),
}));

vi.mock("./db.server", () => ({ default: database }));

import {
  InvalidConfigurationError,
  saveMerchantConfiguration,
  StaleConfigurationError,
} from "./merchant-config.server";

describe("merchant configuration persistence", () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    vi.clearAllMocks();
    process.env.SAHARA_API_KEY = "configured";
    process.env.GROQ_API_KEY = "configured";
    database.$transaction.mockImplementation(async (operation) => operation(database));
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  test("binds updates to the authenticated shop and expected revision", async () => {
    database.merchantConfiguration.updateMany.mockResolvedValue({ count: 1 });
    database.merchantConfiguration.findUniqueOrThrow.mockResolvedValue({ shop: "shop-a" });
    await saveMerchantConfiguration({
      shop: "shop-a.myshopify.com",
      expectedRevision: "revision-1",
      selection: {
        asr: "sahara-stream-pcm",
        llm: "groq-gpt-oss-120b",
        tts: "sahara-tts-female-pcm",
      },
    });
    expect(database.merchantConfiguration.updateMany).toHaveBeenCalledWith(
      expect.objectContaining({
        where: { shop: "shop-a.myshopify.com", revision: "revision-1" },
      }),
    );
    expect(database.merchantConfigurationRevision.create).toHaveBeenCalledOnce();
  });

  test("rejects a stale update without writing a revision", async () => {
    database.merchantConfiguration.updateMany.mockResolvedValue({ count: 0 });
    await expect(
      saveMerchantConfiguration({
        shop: "shop-a.myshopify.com",
        expectedRevision: "stale",
        selection: {
          asr: "sahara-stream-pcm",
          llm: "groq-gpt-oss-120b",
          tts: "sahara-tts-female-pcm",
        },
      }),
    ).rejects.toBeInstanceOf(StaleConfigurationError);
    expect(database.merchantConfigurationRevision.create).not.toHaveBeenCalled();
  });

  test("rejects unavailable profiles before persistence", async () => {
    await expect(
      saveMerchantConfiguration({
        shop: "shop-a.myshopify.com",
        expectedRevision: "revision-1",
        selection: {
          asr: "groq-whisper-large-v3-batch",
          llm: "gemini-flash",
          tts: "sahara-tts-female-pcm",
        },
      }),
    ).rejects.toBeInstanceOf(InvalidConfigurationError);
    expect(database.$transaction).not.toHaveBeenCalled();
  });
});
