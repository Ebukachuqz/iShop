import { afterEach, beforeEach, describe, expect, test } from "vitest";

import { profilesForBrowser, providerProfiles, validateProfileSelection } from "./provider-profiles.server";

describe("merchant provider profiles", () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    process.env.SAHARA_API_KEY = "configured";
    process.env.GROQ_API_KEY = "configured";
    process.env.ISHOP_ENABLE_SAHARA_PIDGIN_TTS = "true";
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  test("requires explicit verification in addition to optional provider credentials", () => {
    const profiles = providerProfiles();
    expect(profiles.find((profile) => profile.id === "sahara-stream-pcm")?.enabled).toBe(true);
    expect(profiles.find((profile) => profile.id === "sahara-tts-female-pidgin")?.enabled).toBe(true);
    expect(profiles.find((profile) => profile.id === "elevenlabs-scribe-v2-realtime")?.enabled).toBe(false);
    expect(profiles.find((profile) => profile.id === "groq-whisper-large-v3-batch")?.enabled).toBe(false);
    expect(profiles.find((profile) => profile.id === "gemini-flash")?.enabled).toBe(false);
  });

  test("enables an optional profile only after its capability gate is set", () => {
    process.env.ELEVENLABS_API_KEY = "configured";
    expect(providerProfiles().find((profile) => profile.id === "elevenlabs-scribe-v2-realtime")?.enabled).toBe(false);
    process.env.ISHOP_ENABLE_ELEVENLABS_REALTIME_STT = "true";
    expect(providerProfiles().find((profile) => profile.id === "elevenlabs-scribe-v2-realtime")?.enabled).toBe(true);
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
