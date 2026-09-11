# Local ASR (P0.16)

Character Memory treats speech recognition as **draft input only**:

`click mic -> record -> click stop -> local ASR -> textarea -> user reviews -> existing send path`

ASR must never call the chat submit function directly. This keeps recognition mistakes from becoming accepted user events, memories, intents, or relationship changes before the user has reviewed the text.

## Provider contract

`SpeechRecognitionProvider.transcribe(...)` is the only backend boundary the Web route depends on. The first implementation is `FunASRProvider`; a future Whisper/SenseVoice/Paraformer/remote provider should implement the same contract rather than changing chat routes.

## First provider: Fun-ASR-Nano on CUDA

Default configuration:

```yaml
asr_provider: funasr
asr_model: FunAudioLLM/Fun-ASR-Nano-2512
asr_device: cuda:0
asr_hub: ms
asr_language: 中文
asr_hotwords: []
asr_max_bytes: 25165824
```

The model is loaded lazily on the first transcription and then reused.

### Install

Install a CUDA-enabled PyTorch + torchaudio build that matches the NVIDIA driver first. Verify:

```bash
python -c "import torch; print(torch.cuda.is_available())"
```

It must print `True` for `asr_device: cuda:0`.

Then install the optional ASR dependency:

```bash
pip install -e ".[asr,api]"
```

FunASR's standard `AutoModel(..., device="cuda")` path is used for the desktop-first implementation. We intentionally do not require vLLM in P0.16; the same provider contract can later receive a vLLM-backed implementation if measured latency justifies the extra deployment complexity.

## Accuracy and speed terminology

**CER (Character Error Rate)** is `(substitutions + deletions + insertions) / reference characters`. Lower is better. A CER of 8% roughly means eight character-level edit errors per one hundred reference characters; it does not mean exactly eight wrong visible characters in every sentence.

**RTF (Real-Time Factor)** is inference time divided by audio duration. Lower is faster. `RTFx = 1 / RTF`; 20x means 20 seconds of audio can be processed in about one second under that benchmark setup.

The current FunASR historical benchmark (184 files, about 192 minutes of Chinese audio, H100) reports approximately:

- SenseVoice-Small GPU: 169.6x, CER 7.81%
- Paraformer-Large GPU: 119.6x, CER 10.18%
- Fun-ASR-Nano PyTorch GPU: 17-21x, CER 8.06%
- Fun-ASR-Nano vLLM batch: 340x, CER 8.20%
- Whisper-large-v3-turbo GPU: 46.1x, CER 21.71%

These are cross-model reference numbers on specific hardware/data, not latency guarantees for a desktop GPU. Measure Character Memory's own recordings before choosing a permanent default.

Official FunASR vLLM guidance currently lists GPU VRAM >= 8 GB and recommends 16 GB+. Standard PyTorch peak memory varies with model/runtime/dtype, so use `nvidia-smi` on the actual machine instead of treating one number as a guarantee.

## Hotwords

The Web route automatically adds current Character names as hotwords, then configured `asr_hotwords`. This is important because a character-name error is more damaging to this product than an ordinary punctuation error.

No LLM silently rewrites the transcript after ASR. The user always sees and can edit the exact ASR draft before sending.
