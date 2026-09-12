# Provider smoke checks

`speech_smoke.py` loads credentials from the untracked root `.env`, sends one permitted WAV to selected ASR profiles, and writes results only to a private ignored path. It never writes credentials or headers.

Example:

```powershell
.\.venv\Scripts\python.exe spikes/providers/speech_smoke.py data/private/sample.wav --output artifacts/private/provider-smoke.json
```

For Sahara, choose the documented language route explicitly, such as `--sahara-language pcm` for Nigerian Pidgin/English or `--sahara-language yo` for Yoruba/English. Use `--provider sahara` to run only one provider. A smoke result establishes wire compatibility only; it is not a benchmark result.
