import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { profilesForBrowser, providerProfiles, validateProfileSelection } from "./provider-profiles.server";

describe("merchant provider profiles", () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    process.env.SAHARA_API_KEY = "configured";
    process.env.GROQ_API_KEY = "configured";
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  test("enables only live eligible profiles with server credentials", () => {
    const profiles = providerProfiles();
    expect(profiles.find((profile) => profile.id === "sahara-stream-pcm")?.enabled).toBe(true);
    expect(profiles.find((profile) => profile.id === "groq-whisper-large-v3-batch")?.enabled).toBe(false);
    expect(profiles.find((profile) => profile.id === "gemini-flash")?.enabled).toBe(false);
  });

  test("rejects role confusion and unavailable profiles", () => {
    expect(
      validateProfileSelection({
        asr: "groq-gpt-oss-120b",
        llm: "gemini-flash",
        tts: "sahara-tts-female-pcm",
      }),
    ).toEqual({
      asr: "Choose a recognized profile for this role",
      llm: "Live eligibility remains unresolved after provider 503 responses",
    });
  });

  test("browser profile data contains no credentials or endpoints", () => {
    const serialized = JSON.stringify(profilesForBrowser());
    expect(serialized).not.toContain("configured");
    expect(serialized).not.toContain("apiKey");
    expect(serialized).not.toContain("endpoint");
  });
});
