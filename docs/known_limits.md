# Known limits

Operational caveats before you ship SAA. The framing here matches the technical report ([arXiv:2604.08412](https://arxiv.org/abs/2604.08412)) and the deployment posture in this repo. Numeric operating points (F1, latency, footprint) live in the paper, not here.

## Scope

- **Not state-of-the-art across all DDSD settings.** The paper targets a specific operating point. Out-of-distribution audio (heavy domain shift, very low SNR, languages outside the eval set) will degrade.
- **No public cross-vendor leaderboard.** SAA's reported numbers are not directly comparable to TTM mAP or other DDSD-style benchmarks. Treat the paper's figures as setting-specific, not as a ranking.

## Audio-only mode

- **Audio-only is supported but at a lower operating point.** With `enable_video=False` the cloud falls back to the audio-only path; accuracy is materially below the audio+video operating point — see the paper for the exact gap. Audio-only is the right mode for telephony, AI pendants, and single-mic kiosks; for desktop / browser / conferencing the multimodal path is recommended.

## Deployment

- **Cloud, not on-device.** The open SDKs stream to `wss://server.attentionlabs.ai/ws`. End-to-end latency is dominated by your network to the SAA cloud. On-device deployment is a separate enterprise licence; the open SDKs alone do not run inference locally.
- **Tokens are bearer credentials.** Treat `ATTENLABS_TOKEN` like an API key. For browser deployments, mint short-lived JWTs server-side per session — never paste a long-lived token into an untrusted bundle. See [`SECURITY.md`](../SECURITY.md#token-rotation) for the rotation recipe.

## What SAA does not replace

- **VAD, wake words, end-of-turn detection — SAA is none of these.** It sits between your VAD and STT and answers a different question: *"was this speech meant for the device?"* The other layers continue to do their jobs around it.
- **STT, LLM, TTS quality.** SAA filters audio at the addressee layer; everything downstream is your existing stack. Errors in transcription, generation, or synthesis are not SAA-side.

## See also

- [arXiv:2604.08412](https://arxiv.org/abs/2604.08412) — the technical report (architecture, evaluation, operating points).
- [`SECURITY.md`](../SECURITY.md) — token rotation, vulnerability disclosure.
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — repository-side scope rules.
