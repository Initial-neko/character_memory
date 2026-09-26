# ASR P0 Technical Report

> Status: research / acceptance input only. No production code change in this PR.
>
> Baseline audited: `main` at `3bff5afea674a12597581b4e1e59d796d19ba641`.
>
> Goal: make local ASR robust against missing words, premature segmentation, and recognition errors. Latency is a secondary concern; transcript completeness and correctness are P0.

## 1. Executive conclusion

The current ASR problem is architectural as well as model-related.

The current browser call path performs local RMS endpointing and then sends one complete WAV to `POST /v1/asr`. The provider contract is offline/batch-oriented: an audio sample array is transcribed into one final result. There is no shared streaming session, partial transcript protocol, transcript reconciliation layer, or final-pass correction contract.

Therefore, replacing SenseVoice with another offline model alone is not sufficient.

### Recommended local direction

1. **Primary streaming recognizer:** FunASR Paraformer-zh-streaming.
2. **Primary endpoint/VAD:** FunASR FSMN-VAD, with endpoint policy owned by the ASR session rather than duplicated in the browser.
3. **Final accuracy benchmark / optional second pass:** Qwen3-ASR 1.7B and Fun-ASR-Nano.
4. **Current SenseVoice:** retain temporarily as the baseline/fallback until benchmark data proves a replacement.
5. **One shared ASR session API:** both Dictation and Browser Call should feed the same PCM streaming path.

The target is a two-stage pipeline:

```
Mic
  -> 16 kHz PCM
  -> ASR Session
      -> VAD / endpoint state
      -> Streaming ASR
      -> partial transcript
      -> final segment
      -> optional high-accuracy final recheck
      -> reconciled final text
  -> Character Runtime
```

The design intentionally accepts modest additional finalization latency in exchange for transcript completeness.

---

## 2. Current implementation audit

### 2.1 Dictation path

Current browser implementation effectively does:

```
getUserMedia
  -> AudioContext
  -> ScriptProcessor(2048)
  -> Float32Array chunks
  -> concatChunks()
  -> downsample to 16 kHz
  -> build WAV
  -> POST /v1/asr
  -> one final transcript
```

Important consequence: Dictation is not a streaming ASR path. Audio is accumulated and recognized only after recording is finished.

### 2.2 Browser Call path

The call implementation has its own browser-side RMS endpointing:

- threshold: `0.025`
- speech start: two hot frames
- pre-roll: up to six frames
- minimum speech: 250 ms
- silence endpoint: currently obtained from Media Runtime health, defaulting to 900 ms
- hard maximum speech segment: 12 s

The path is:

```
AudioWorklet-like browser processing is NOT used.
AudioContext
  -> ScriptProcessor(2048)
  -> RMS
  -> browser endpoint decision
  -> finishSpeech()
  -> concatenate full segment
  -> downsample to 16 kHz
  -> WAV
  -> POST /v1/asr
  -> final text
  -> send chat message
```

This is functional but fragile for speech completeness.

### 2.3 Current ASR provider contract

The current abstraction is effectively:

```
transcribe(samples, sample_rate) -> TranscriptionResult
```

This is a batch/offline contract.

It does not model:

- session start/stop;
- incremental PCM frames;
- VAD state;
- partial results;
- final results;
- per-segment sequence numbers;
- cancellation;
- reconnect/replay;
- final-pass reconciliation.

That contract should be replaced/extended rather than hiding a streaming implementation behind a batch method.

---

## 3. Current P0 failure risks

### P0-A: premature endpointing

A 900 ms silence threshold can split natural Chinese speech pauses into separate turns. A 12 s hard limit also forcibly fragments long speech.

VAD should answer **"is speech currently present?"** while endpoint policy should answer **"has this utterance actually ended?"**. Those are related but not identical decisions.

### P0-B: no partial transcript

The user receives no ASR evidence while speaking. We cannot distinguish:

- model recognition error;
- endpoint error;
- final-result regression;
- UI text replacement error.

A streaming partial/final protocol is therefore needed for observability as well as UX.

### P0-C: no transcript reconciliation

A sequence such as:

```
partial: 我觉得这个
partial: 我觉得这个角色
partial: 我觉得这个角色现在
final:   我觉得这个角色现在其实还可以
```

must be reconciled by sequence/segment identity. It must not be implemented as arbitrary string replacement.

### P0-D: two different capture semantics

Dictation and Browser Call currently have materially different recording/endpointing behavior. A future ASR implementation should have one shared audio-session contract with different UI policies on top.

### P0-E: accuracy is not currently measurable at the right boundary

The system needs a fixed corpus of real user-style utterances and must separately measure:

- audio captured vs expected duration;
- endpoint start/end;
- partial transcript stability;
- final transcript CER/WER;
- omission rate;
- substitution rate;
- insertion rate;
- finalization latency;
- GPU VRAM peak;
- CPU/RAM;
- repeated-session stability.

"Model accuracy" alone is insufficient.

---

## 4. Candidate local models

### 4.1 Paraformer-zh-streaming — primary streaming candidate

FunASR lists Paraformer-zh-streaming as a dedicated streaming ASR model. The FunASR quickstart explicitly documents real-time microphone/WebSocket streaming, and the project provides streaming/cache-oriented examples.

Model scale is about 220M parameters. This is materially smaller than Qwen3-ASR 1.7B and is therefore attractive for always-on local use.

Why it fits:

- dedicated streaming architecture;
- Chinese-focused;
- low model footprint;
- compatible with a long-lived local Media Runtime;
- can provide the partial/final semantics we currently lack.

Reference:
- https://github.com/modelscope/FunASR
- https://github.com/modelscope/FunASR/tree/main/examples/industrial_data_pretraining/paraformer_streaming

### 4.2 FSMN-VAD — primary VAD candidate

FunASR lists FSMN-VAD as a small VAD model (roughly 0.4M parameters in the current model listing).

Its job should remain VAD, not be treated as the entire endpointing policy.

This lets us move speech-boundary decisions into the ASR session instead of keeping an independent RMS detector in the browser.

Reference:
- https://github.com/modelscope/FunASR

### 4.3 Fun-ASR-Nano — final-pass candidate

Fun-ASR-Nano is an approximately 800M-parameter ASR model in the current FunASR model list. The repository also contains a real-time WebSocket serving example where FSMN-VAD feeds confirmed segments to Fun-ASR-Nano.

This is particularly interesting for a two-pass architecture:

```
streaming recognizer -> low latency partials
                       |
                       v
                 final segment
                       |
                       v
                 Fun-ASR-Nano
                       |
                       v
                 final transcript
```

Reference:
- https://github.com/modelscope/FunASR
- https://github.com/modelscope/FunASR/blob/main/examples/industrial_data_pretraining/fun_asr_nano/serve_vllm.py

### 4.4 Qwen3-ASR 1.7B — accuracy benchmark / optional final pass

Qwen3-ASR 1.7B is much larger (~1.7B parameters) and is a strong candidate for the accuracy side of the benchmark, especially for multilingual or code-switching input.

It should not automatically become the streaming front-end merely because it is larger. The first question is whether its measured omission/substitution rate justifies the additional resource cost.

A current Qwen3-ASR 1.7B ONNX FP16 community deployment reports about 9 GiB free VRAM for three independent ONNX Runtime sessions, while the upstream Fun-ASR/Qwen tooling documents GPU >=8 GB and recommends 16 GB+ for its vLLM deployment. These are deployment-specific figures, not universal model requirements.

Reference:
- https://huggingface.co/docs/transformers/main/model_doc/qwen3_asr
- https://github.com/QwenAudio/Fun-ASR

### 4.5 SenseVoice — baseline/fallback

The current project uses SenseVoice through the existing Sherpa runtime. It is approximately 234M parameters according to the current FunASR model listing.

It should remain available during migration so we can compare:

```
current SenseVoice
vs
Paraformer Streaming
vs
Fun-ASR-Nano
vs
Qwen3-ASR 1.7B
```

without changing the user-visible product contract.

---

## 5. GPU VRAM / system resource assessment

Exact VRAM must be measured on the actual target machine; model parameter count alone is not a valid VRAM estimate.

### Planning envelope

| Component | Scale | Expected role | Resource planning |
|---|---:|---|---|
| FSMN-VAD | ~0.4M | VAD | negligible compared with ASR |
| Paraformer-zh-streaming | ~220M | primary streaming ASR | low-GPU-footprint candidate |
| SenseVoiceSmall | ~234M | current baseline | low-GPU-footprint candidate |
| Fun-ASR-Nano | ~800M | final-pass ASR | medium footprint |
| Qwen3-ASR 1.7B | ~1.7B | accuracy benchmark/final pass | highest footprint of these candidates |

FunASR's current quickstart describes GPU 8 GB+ as a practical starting point, while its vLLM guidance recommends 16 GB+ for GPU deployments. This is a deployment-level recommendation rather than a claim that every 220M model needs 8 GB.

For Qwen3-ASR 1.7B, deployment footprint depends strongly on implementation and quantization. One current ONNX FP16 deployment reports about 9 GiB free VRAM for three independent sessions; this should be treated as a concrete implementation reference, not as the universal requirement.

### Important architectural implication

We should **not load all candidate models simultaneously in production**.

A reasonable production progression is:

**Phase A**
```
FSMN-VAD + Paraformer Streaming
```

**Phase B, if benchmark requires a second pass**
```
FSMN-VAD + Paraformer Streaming
                  +
             one final-pass model
```

If the final-pass model creates unacceptable VRAM pressure, the system should use an explicit model lifecycle policy:

```
streaming model resident
        |
final segment
        |
optional final-pass model
        |
result
        |
model can remain resident OR unload
```

The choice should be based on measured latency/VRAM, not intuition.


---

## 5A. Resource evidence matrix (expanded)

The resource numbers below intentionally distinguish **weight/file size**, **observed process memory**, and **recommended/required GPU capacity**. These are not interchangeable. A parameter-count calculation is only a planning estimate.

| Candidate | Params | Published / observed artifact size | Published / observed runtime memory | GPU planning | CPU-only |
|---|---:|---:|---:|---|---|
| **FSMN-VAD** | ~0.4M | very small | negligible relative to ASR model | effectively negligible; measure co-resident overhead | Yes |
| **Paraformer-zh-streaming** | ~220M | Paraformer benchmark reports **880 MB FP32**, **237 MB INT8** for the 220M Paraformer family | Public official benchmark evidence is stronger for CPU/ONNX throughput than isolated inference VRAM; do **not** infer 880 MB = VRAM | Plan for a low-VRAM model; exact CUDA peak must be measured with the chosen runtime | **Yes**, ONNX is a practical deployment path |
| **SenseVoiceSmall** | ~234M | FunASR model listing identifies 234M; official GGUF artifacts provide substantially smaller quantized files | Public deployment reports put the complete lightweight pipeline well below multi-GB scale, but exact CUDA peak is runtime-dependent | Low-VRAM candidate; retain as baseline and measure | **Yes** |
| **Fun-ASR-Nano-2512** | ~830M / 800M-class | Published Q4_K_M artifact: **0.52 GiB**; BF16 artifact about **1.55 GiB** in one public artifact index | A public CUDA inference issue observed **1.99 GB allocated / 2.01 GB reserved** immediately after model load; this is implementation-specific, not a universal peak | Native/vLLM guidance says **GPU >=8 GB**, **16 GB+ recommended**; actual single-stream peak must be measured | Possible in some builds, but not the preferred production path |
| **Qwen3-ASR 1.7B** | ~1.7B active | OpenASR artifacts: **4.70 GB FP16**, **2.51 GB Q8_0**, **1.33 GB Q4_K**; RAM peaks 4.43/4.14/4.04 GB respectively in isolated OpenASR packs | A separate HF deployment reports roughly **4 GB** for BF16 ASR weights plus ~2 GB if the aligner is loaded; another service implementation recommends **>=12 GB VRAM**. These are deployment-specific. | Treat **12 GB as the practical floor for a comfortable CUDA service target**, not as a model-theoretical minimum. 16 GB gives more headroom. | OpenASR provides CPU builds, but for interactive Character Memory use, CPU latency must be benchmarked rather than assumed acceptable |

### Evidence and interpretation

**Paraformer / FunASR:** the official FunASR ONNX benchmark lists the 220M Paraformer family at 880 MB storage in FP32 and 237 MB after INT8 quantization. That benchmark is primarily a CPU/ONNX benchmark, so these numbers must not be mislabeled as peak GPU VRAM. citeturn0search0

The official model zoo currently lists Paraformer-zh-streaming at 220M, SenseVoiceSmall at 234M, Fun-ASR-Nano at 800M, Qwen3-ASR at 1.7B, and FSMN-VAD at 0.4M. citeturn0search1turn0search13

**Fun-ASR-Nano:** a public CUDA inference report observed 1.99 GB allocated / 2.01 GB reserved immediately after loading the model. This is useful as a real deployment datapoint, but it is not a vendor minimum or universal peak. citeturn1search0 The current Fun-ASR vLLM guide specifies GPU >=8 GB and recommends 16 GB+. citeturn1search9

For artifact-level planning, a public model index reports Fun-ASR-Nano-2512 Q4_K_M at 0.52 GiB and BF16 at about 1.55 GiB before runtime/cache overhead. citeturn1search2

**Qwen3-ASR 1.7B:** the OpenASR distribution currently publishes FP16/Q8/Q4 builds at 4.70/2.51/1.33 GB, with isolated RAM peaks of 4.43/4.14/4.04 GB. Its benchmark is useful for quantization footprint, but it is not a direct GPU-VRAM benchmark for the PyTorch/vLLM runtime we would deploy. citeturn0search10turn0search14 A separate deployment reports >=12 GB VRAM as its supported target, with roughly 4 GB for BF16 ASR weights and another ~2 GB if a timestamp aligner is loaded. citeturn0search15

### Resource-selection rule

We should not select a candidate from parameter count or model-file size alone.

For each candidate that survives the accuracy benchmark, measure on the **actual Character Memory target machine**:

1. cold-start VRAM;
2. warm idle VRAM;
3. peak VRAM during a 60 s continuous utterance;
4. peak VRAM during repeated 10-minute sessions;
5. CPU utilization during streaming;
6. system RAM;
7. model load time;
8. first-partial latency;
9. finalization latency;
10. whether the runtime silently falls back from CUDA to CPU;
11. VRAM after 10/50/100 repeated sessions;
12. concurrent-session behavior at 1, 2, and 4 sessions.

### Important deployment implication

For our expected workload, **one resident streaming model plus one optional final-pass model** is preferable to loading multiple heavyweight ASR models permanently.

A concrete starting resource budget is therefore:

- **Low-VRAM path:** FSMN-VAD + Paraformer-zh-streaming.
- **Mid-VRAM path:** FSMN-VAD + Paraformer streaming + Fun-ASR-Nano final pass.
- **Higher-VRAM path:** FSMN-VAD + Paraformer streaming + Qwen3-ASR 1.7B final pass.
- Do not co-resident-load both Fun-ASR-Nano and Qwen3-ASR merely for redundancy before measurement proves that this is necessary.

The final decision remains empirical because VRAM depends on framework, precision, CUDA kernels, KV/cache policy, context length, and number of concurrent sessions.

---

## 6. Recommended target architecture

### 6.1 Unified ASR session

Introduce a conceptual session contract:

```
start_session(config)
  -> session_id

push_audio(session_id, pcm16)
  -> partial events

end_segment(session_id)
  -> final event

cancel_session(session_id)

close_session(session_id)
```

Events should contain at least:

```
{
  session_id,
  segment_id,
  revision,
  kind: "partial" | "final",
  text,
  start_ms,
  end_ms
}
```

The browser must never infer whether a sentence is final from timing alone.

### 6.2 Endpointing

Move the primary speech-boundary decision into the Media Runtime.

Use:

```
FSMN-VAD
+
endpoint policy
+
small pre-roll
+
small post-roll
+
maximum utterance guard
```

The browser should mainly capture PCM and render state.

### 6.3 Transcript reconciliation

Maintain one segment buffer:

```
segment_id=17

partial revision 1
partial revision 2
partial revision 3
final revision
```

Only the final revision becomes the message sent to Character Runtime.

A final-pass correction must replace the same segment rather than create a duplicate user message.

### 6.4 Two-pass accuracy mode

If benchmarks show that streaming Paraformer still produces unacceptable substitutions/omissions:

```
PCM
 -> FSMN-VAD
 -> Paraformer streaming
 -> partial UI

segment final
 -> high-accuracy ASR
 -> reconcile final
 -> send Character message
```

This deliberately spends additional latency to protect transcript correctness.

---

## 7. Benchmark plan

Do not select the model from published headline accuracy numbers.

Build a fixed local corpus containing at least:

1. normal conversational Mandarin;
2. 15–30 second continuous speech;
3. 30–60 second continuous speech;
4. deliberate 0.5–1.5 s pauses;
5. fast speech;
6. low-volume speech;
7. common background noise;
8. Chinese + English code switching;
9. project terms;
10. Character names;
11. numbers and configuration values;
12. long sentences with subordinate clauses.

For each recording store a human-verified reference transcript.

### Primary metrics

**Omission rate**
```
deleted reference content / reference content
```

This is the most important metric.

**Character Error Rate (CER)**

For Chinese:

```
CER = (S + D + I) / N
```

where D (deletions) must be reported separately.

**Substitution rate**

Important for names and technical terms.

**Insertion rate**

Important because aggressive decoding/correction can hallucinate words.

**Endpoint loss**

Reference speech that falls outside the recognized segment boundaries.

**Finalization latency**

Time from detected end-of-speech to final transcript.

**Streaming stability**

Number of partial revisions and whether final text unexpectedly removes already stable content.

**Resource metrics**

- GPU VRAM baseline;
- GPU VRAM peak;
- CPU utilization;
- system RAM;
- model load time;
- warm-session latency;
- cold-start latency.

### Acceptance principle

Do not use one aggregate score.

The model must pass minimum thresholds for:

```
omission
+
substitution
+
endpoint loss
+
finalization latency
+
VRAM
```

A model with a slightly better average CER but significantly more deletions should not automatically replace the current baseline.

---

## 8. Migration order

### Step 1 — contract

Define one Media Runtime ASR session abstraction without changing the Character Runtime semantics.

### Step 2 — streaming prototype

Implement:

```
FSMN-VAD
+
Paraformer-zh-streaming
+
partial/final WebSocket events
```

### Step 3 — browser integration

Replace browser-owned endpointing with the shared ASR session. Keep the existing UI state machine where possible.

### Step 4 — final-pass benchmark

Run the same recordings through:

- current SenseVoice;
- Paraformer Streaming final;
- Fun-ASR-Nano;
- Qwen3-ASR 1.7B.

### Step 5 — choose production mode

Possible outcomes:

**A.**
```
Paraformer streaming is accurate enough
-> keep one model
```

**B.**
```
Paraformer is excellent for streaming but misses some words
-> Paraformer + final-pass model
```

**C.**
```
Qwen3-ASR materially reduces omission/substitution
and fits available VRAM
-> use Qwen3-ASR as final-pass model
```

No model is selected solely by parameter count.

---

## 9. P0 acceptance criteria

ASR should not be marked complete until all of the following are demonstrated on the actual target machine:

- [ ] 30–60 s continuous Chinese speech does not lose material content.
- [ ] Natural pauses do not arbitrarily split one utterance.
- [ ] Long utterances do not silently truncate at the current 12 s boundary.
- [ ] Partial results are visible and revision-safe.
- [ ] Final result cannot regress by replacing a complete final segment with an older partial.
- [ ] Dictation and Browser Call use the same ASR session semantics.
- [ ] Microphone capture errors are distinguishable from ASR recognition errors.
- [ ] ASR runtime reports model readiness and actual device (CUDA/CPU).
- [ ] GPU VRAM peak is measured during warm and repeated sessions.
- [ ] No silent CPU fallback when GPU execution is configured as required.
- [ ] Reference corpus reports CER plus deletion/substitution/insertion separately.
- [ ] Character names and project-specific terms are explicitly benchmarked.
- [ ] Final transcript sent to Character Runtime is exactly the reconciled final segment.
- [ ] Existing SenseVoice remains available until the replacement has passed the benchmark.

---

## 10. Decision

**Recommended route:**

```
                Browser
                   |
              PCM 16 kHz
                   |
             Media Runtime
                   |
             FSMN-VAD
                   |
       Paraformer-zh-streaming
                   |
            partial / final
                   |
          Transcript Buffer
                   |
          +--------+--------+
          |                 |
      acceptable        needs review
          |                 |
          |          Fun-ASR-Nano /
          |          Qwen3-ASR 1.7B
          |                 |
          +--------+--------+
                   |
             FINAL TEXT
                   |
             Character Runtime
```

This route minimizes resource usage while directly addressing the actual P0 failure modes: missing words, premature endpointing, and final-text regression.

The next implementation PR should be created only after the benchmark corpus and actual target-machine VRAM measurements are available. This report intentionally does not make a production model-selection claim before those measurements.
