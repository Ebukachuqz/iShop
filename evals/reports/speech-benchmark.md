# iShop speech recognition and downstream shopping benchmark

**Run date:** 15 September 2026  
**Systems:** Sahara by Intron, Groq Whisper Large v3, AssemblyAI Universal-2, and ElevenLabs Scribe v2

## Summary

This pilot evaluates how accurately four speech-recognition systems transcribe English, Nigerian Pidgin, Yoruba, and code-switched speech, and whether their transcripts preserve enough meaning for iShop shopping actions.

Sahara achieved the lowest Nigerian Pidgin WER on the AfriSwitch sample and preserved the intended downstream task in all 10 iShop Pidgin cases. ElevenLabs achieved the lowest WER and CER on the iShop Pidgin commands and performed best on English commands, with 5.0% WER and all 10 English tasks recoverable. All four systems performed poorly on the small Yoruba sample, so these results do not support a robust Yoruba claim.

## Models compared

| Model | Configuration | Observed strengths | Observed weaknesses |
| --- | --- | --- | --- |
| Sahara | Intron batch speech recognition with a language hint | Best Nigerian Pidgin WER on AfriSwitch; preserved all tested Pidgin shopping tasks | Two English timeouts; weaker English accuracy than ElevenLabs |
| Groq | Whisper Large v3, batch transcription | Low median latency; complete AfriSwitch coverage | Produced unrelated non-Latin text on some short English clips and one empty output |
| AssemblyAI | Universal-2, batch transcription | Complete coverage across both corpora; stable responses | Highest median latency and high Yoruba error; moderate command accuracy |
| ElevenLabs | Scribe v2, batch transcription | Best English command accuracy and fastest command latency; strong Pidgin results | AfriSwitch Pidgin WER trailed Sahara slightly; Yoruba remained high-error |

## Data

### AfriSwitch sample

- Source: [AfriSwitch](https://huggingface.co/datasets/intronhealth/AfriSwitch)
- 20 gated test clips: 10 Nigerian Pidgin-English and 10 Yoruba-English
- 164.44 seconds total (2.74 minutes)
- Clips were selected deterministically with seed 42
- Each clip was 3-15 seconds long and had at least one annotated language switch point
- The source dataset is licensed CC BY-NC-SA 4.0 and was used only for evaluation

### iShop shopping commands

- 20 consented recordings from one test speaker
- 10 English and 10 Nigerian Pidgin commands
- 96.23 seconds total (1.60 minutes)
- Tasks covered product search, budget filtering, product navigation, cart additions and reductions, cart inspection, comparison, and checkout handoff

The pilot contains only 40 unique clips and one speaker in the iShop command set. Its findings are directional and should not be treated as population-level performance estimates.

## Preprocessing and protocol

All audio was converted once to mono, 16 kHz, signed PCM16 WAV. Channel averaging and deterministic linear resampling were applied consistently. Every model received the same normalized files, reference transcripts, evaluation order, and applicable language hints.

Failures and empty outputs were retained. A failed or empty transcription received WER and character error rate (CER) of 100% and no downstream credit. Latency is the median for completed requests because failed requests have no completed response time.

## Metrics

- **WER:** word substitutions, deletions, and insertions divided by the reference word count. This measures whether the transcript preserves complete words used in product names, actions, quantities, and constraints.
- **CER:** character-level edit error. This complements WER for spelling variation, morphology, and orthography.
- **Coverage:** completed transcriptions divided by attempted transcriptions. This prevents a system from appearing accurate after failed requests are ignored.
- **Median latency:** time to receive a completed provider response. Median is less distorted by isolated slow requests than the mean.
- **Action/tool recoverability:** whether a frozen local semantic rubric could recover the expected shopping action from the transcript.
- **Slot accuracy:** whether critical values such as product, quantity, budget, and compound follow-up survived transcription.
- **Task success:** whether the transcript preserved the expected action and all required critical slots.

The downstream evaluation used a frozen offline semantic rubric over the ASR hypotheses. It did not call an external language model or execute actions against a live Shopify store. It measures transcript recoverability rather than full production task completion.

## Speech-recognition results

Lower WER, CER, and latency are better. Higher coverage is better. Each row contains 10 attempted utterances.

### AfriSwitch

| Model | Language | Coverage | WER | CER | Median latency |
| --- | --- | ---: | ---: | ---: | ---: |
| Sahara | Nigerian Pidgin | 100% | **33.6%** | **20.0%** | 2,858 ms |
| Groq Whisper Large v3 | Nigerian Pidgin | 100% | 48.1% | 34.7% | 2,045 ms |
| AssemblyAI Universal-2 | Nigerian Pidgin | 100% | 42.5% | 25.3% | 4,898 ms |
| ElevenLabs Scribe v2 | Nigerian Pidgin | 100% | 41.5% | 24.9% | **1,705 ms** |
| Sahara | Yoruba | 100% | **83.8%** | 55.8% | 2,816 ms |
| Groq Whisper Large v3 | Yoruba | 100% | 93.5% | 62.3% | **2,315 ms** |
| AssemblyAI Universal-2 | Yoruba | 100% | 91.3% | 72.5% | 5,269 ms |
| ElevenLabs Scribe v2 | Yoruba | 100% | 87.7% | **55.5%** | 3,328 ms |

### iShop shopping commands

| Model | Language | Coverage | WER | CER | Median latency |
| --- | --- | ---: | ---: | ---: | ---: |
| Sahara | English | 80% | 33.9% | 25.6% | 2,634 ms |
| Groq Whisper Large v3 | English | 90% | 44.3% | 50.3% | 1,828 ms |
| AssemblyAI Universal-2 | English | 100% | 27.7% | 19.5% | 4,829 ms |
| ElevenLabs Scribe v2 | English | 100% | **5.0%** | **5.0%** | **1,435 ms** |
| Sahara | Nigerian Pidgin | 100% | 16.5% | 11.2% | 2,671 ms |
| Groq Whisper Large v3 | Nigerian Pidgin | 100% | 32.8% | 21.8% | **1,696 ms** |
| AssemblyAI Universal-2 | Nigerian Pidgin | 100% | 35.1% | 23.5% | 4,518 ms |
| ElevenLabs Scribe v2 | Nigerian Pidgin | 100% | **11.5%** | **4.2%** | 1,709 ms |

## Downstream shopping-task results

Higher values are better. Each row contains 10 command episodes.

| Model | Language | Action/tool recoverability | Slot accuracy | Task success |
| --- | --- | ---: | ---: | ---: |
| Sahara | English | 60% | 90.9% | 60% |
| Groq Whisper Large v3 | English | 50% | 45.5% | 50% |
| AssemblyAI Universal-2 | English | 70% | 100% | 70% |
| ElevenLabs Scribe v2 | English | **100%** | **100%** | **100%** |
| Sahara | Nigerian Pidgin | **100%** | **100%** | **100%** |
| Groq Whisper Large v3 | Nigerian Pidgin | 70% | 81.8% | 70% |
| AssemblyAI Universal-2 | Nigerian Pidgin | 80% | 90.9% | 80% |
| ElevenLabs Scribe v2 | Nigerian Pidgin | 90% | 90.9% | 80% |

## Qualitative findings

### Nigerian Pidgin

Sahara consistently retained shopping intent and key entities. Common near-homophone errors included “cart” becoming “cat” or “car,” but the requested action usually remained recoverable. ElevenLabs produced the lowest character error on the iShop Pidgin commands and generally preserved entities, although compound or slot-sensitive mistakes reduced task success. AssemblyAI remained usable but lost more function words and a critical slot. Groq was fast but less stable on these short recordings.

### English

ElevenLabs was the strongest English system in this pilot. AssemblyAI completed every request but lexical errors reduced downstream success. Sahara accurately transcribed several requests, although two provider timeouts reduced coverage and task success. Groq sometimes produced unrelated scripts or empty output on short recordings, causing failures that WER alone does not fully describe.

### Yoruba

All systems had high WER on the 10-clip Yoruba-English sample. Sahara had the lowest WER, while ElevenLabs had the lowest CER by a small margin. The gap between WER and CER suggests partial phonetic or orthographic overlap that did not preserve complete reference words. A larger, speaker-diverse Yoruba evaluation and language-specific normalization are required before enabling or advertising robust Yoruba support.

## Product implications

- Prefer Sahara for Nigerian Pidgin, with explicit timeout handling and an enabled fallback provider.
- Prefer ElevenLabs for English-heavy traffic based on this pilot.
- Keep provider selection configurable because accuracy and latency vary substantially by language.
- Preserve transcript correction and clarification before consequential cart actions.
- Continue to require verified cart read-back and human-controlled checkout.
- Do not claim robust Yoruba support based on the current evidence.

## Limitations

The experiment used 10 utterances per model-language-corpus cell, a single speaker for the iShop commands, one request per clip and provider, heterogeneous provider endpoints, and no confidence calibration. The downstream score is an offline semantic proxy rather than live Shopify execution. A production evaluation should include more speakers, accents, devices, background-noise conditions, repeated trials, realtime streaming measurements, and end-to-end storefront outcomes.

## Reproducibility and privacy

The evaluation implementation, scoring code, annotation guidance, dataset card, and synthetic fixtures are available under `evals/`. Private manifests preserve hashes for normalized recordings, while raw audio, raw provider responses, and per-recording private material remain outside Git. No credentials or personal identifiers are included in this report.

## References

- [AfriSwitch dataset](https://huggingface.co/datasets/intronhealth/AfriSwitch)
- [Intron streaming speech recognition](https://docs.voice.intron.io/docs/stt/streaming)
- [Groq speech-to-text documentation](https://console.groq.com/docs/speech-to-text)
- [AssemblyAI evaluation guidance](https://www.assemblyai.com/docs/evaluations)
- [ElevenLabs Scribe v2 documentation](https://elevenlabs.io/docs/overview/capabilities/speech-to-text)
