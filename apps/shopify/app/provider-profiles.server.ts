export type ProviderRole = "asr" | "llm" | "tts";

export type ProviderProfile = {
  id: string;
  role: ProviderRole;
  label: string;
  mode: string;
  enabled: boolean;
  disabledReason?: string;
};

const credentialPresent = (name: string) => Boolean(process.env[name]?.trim());

export function providerProfiles(): ProviderProfile[] {
  return [
    {
      id: "sahara-stream-pcm",
      role: "asr",
      label: "Sahara realtime",
      mode: "Streaming PCM, code-switching",
      enabled: credentialPresent("SAHARA_API_KEY"),
      disabledReason: "Sahara server credential is unavailable",
    },
    {
      id: "elevenlabs-scribe-v2-realtime",
      role: "asr",
      label: "ElevenLabs Scribe v2 realtime",
      mode: "Realtime WebSocket transcription",
      enabled: credentialPresent("ELEVENLABS_API_KEY"),
      disabledReason: "ElevenLabs server credential is unavailable",
    },
    {
      id: "assemblyai-v3-realtime",
      role: "asr",
      label: "AssemblyAI v3 realtime",
      mode: "Realtime streaming transcription",
      enabled: credentialPresent("ASSEMBLYAI_API_KEY"),
      disabledReason: "AssemblyAI server credential is unavailable",
    },
    {
      id: "gemini-3.5-transcribe-live",
      role: "asr",
      label: "Gemini 3.5 Transcribe live",
      mode: "Live streaming transcription",
      enabled: credentialPresent("GEMINI_API_KEY"),
      disabledReason: "Gemini server credential is unavailable",
    },
    {
      id: "groq-whisper-large-v3-batch",
      role: "asr",
      label: "Groq Whisper Large v3",
      mode: "Benchmark file transcription only",
      enabled: false,
      disabledReason: "Batch-only profile cannot power live conversation",
    },
    {
      id: "assemblyai-universal-2-batch",
      role: "asr",
      label: "AssemblyAI Universal-2",
      mode: "Benchmark file transcription only",
      enabled: false,
      disabledReason: "Batch-only profile cannot power live conversation",
    },
    {
      id: "gemini-3.5-transcribe-batch",
      role: "asr",
      label: "Gemini 3.5 Transcribe",
      mode: "Benchmark file transcription only",
      enabled: false,
      disabledReason: "Batch-only profile cannot power live conversation",
    },
    {
      id: "elevenlabs-scribe-v2-batch",
      role: "asr",
      label: "ElevenLabs Scribe v2",
      mode: "Benchmark file transcription only",
      enabled: false,
      disabledReason: "Batch-only profile cannot power live conversation",
    },
    {
      id: "groq-gpt-oss-120b",
      role: "llm",
      label: "Groq GPT OSS 120B",
      mode: "Structured shopping intent",
      enabled: credentialPresent("GROQ_API_KEY"),
      disabledReason: "Groq server credential is unavailable",
    },
    {
      id: "gemini-flash",
      role: "llm",
      label: "Gemini Flash",
      mode: "Structured shopping intent",
      enabled: false,
      disabledReason: "Live eligibility remains unresolved after provider 503 responses",
    },
    {
      id: "sahara-tts-female-pidgin",
      role: "tts",
      label: "Sahara female Pidgin-English voice",
      mode: "Sahara streaming Pidgin voice",
      enabled: credentialPresent("SAHARA_API_KEY"),
      disabledReason: "Sahara server credential is unavailable",
    },
    {
      id: "sahara-tts-female-pcm",
      role: "tts",
      label: "Drake female voice (legacy)",
      mode: "Sahara female voice (migrates to Pidgin)",
      enabled: credentialPresent("SAHARA_API_KEY"),
      disabledReason: "Sahara server credential is unavailable",
    },
    {
      id: "elevenlabs-tts-female-stream",
      role: "tts",
      label: "ElevenLabs female voice (HTTP stream)",
      mode: "HTTP progressive audio streaming",
      enabled: credentialPresent("ELEVENLABS_API_KEY"),
      disabledReason: "ElevenLabs server credential is unavailable",
    },
    {
      id: "elevenlabs-tts-female-ws",
      role: "tts",
      label: "ElevenLabs female voice (WebSocket)",
      mode: "Bidirectional stream-input synthesis",
      enabled: credentialPresent("ELEVENLABS_API_KEY"),
      disabledReason: "ElevenLabs server credential is unavailable",
    },
    {
      id: "gemini-tts-female-stream",
      role: "tts",
      label: "Gemini female voice (streaming)",
      mode: "Interactions audio stream",
      enabled: credentialPresent("GEMINI_API_KEY"),
      disabledReason: "Gemini server credential is unavailable",
    },
    {
      id: "groq-orpheus-tts-female",
      role: "tts",
      label: "Groq Orpheus female English voice",
      mode: "Buffered speech synthesis",
      enabled: credentialPresent("GROQ_API_KEY"),
      disabledReason: "Groq server credential is unavailable",
    },
  ];
}

export function profilesForBrowser() {
  return providerProfiles().map(({ id, role, label, mode, enabled, disabledReason }) => ({
    id,
    role,
    label,
    mode,
    enabled,
    disabledReason: enabled ? undefined : disabledReason,
  }));
}

export function validateProfileSelection(selection: Record<ProviderRole, string>) {
  const profiles = providerProfiles();
  const normalizedSelection = { ...selection };
  // Transparent migration for legacy profile ID
  if (normalizedSelection.tts === "sahara-tts-female-pcm") {
    normalizedSelection.tts = "sahara-tts-female-pidgin";
  }
  const errors: Partial<Record<ProviderRole, string>> = {};
  for (const role of ["asr", "llm", "tts"] as const) {
    const profile = profiles.find((candidate) => candidate.id === selection[role] || candidate.id === normalizedSelection[role]);
    if (!profile || profile.role !== role) {
      errors[role] = "Choose a recognized profile for this role";
    } else if (!profile.enabled) {
      errors[role] = profile.disabledReason ?? "This profile is unavailable";
    }
  }
  return errors;
}
