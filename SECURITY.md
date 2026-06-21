# Security policy

## Supported versions

| Package | Version | Status |
|---|---|---|
| `@attenlabs/saa-js` | `0.6.x` | Supported |
| `attenlabs-saa` (PyPI) | `0.6.x` | Supported |
| `saa-livekit-client` (PyPI) | `0.3.x` | Supported |
| `saa-pipecat-client` (PyPI) | `0.3.x` | Supported |
| Older versions | (prior) | End-of-life; please upgrade |

## Reporting a vulnerability

If you discover a security issue in the SAA SDKs, the cloud service, or any framework adapter in this repository:

1. **Do not file a public GitHub issue.**
2. Email **security@attentionlabs.ai** with:
   - A clear description of the vulnerability and its impact.
   - Reproduction steps or a proof-of-concept, if available.
   - The affected SDK / version / endpoint.
3. You should receive an acknowledgement within **two business days**.
4. Coordinated disclosure: we will work with you on a fix timeline before any public disclosure.

## Scope

**In scope.**

- The published packages (`@attenlabs/saa-js`, `attenlabs-saa`, `saa-livekit-client`).
- The examples under [`examples/`](./examples/) when used as documented.
- The cloud service at `server.attentionlabs.ai` (please mark cloud-service reports clearly).

**Out of scope.**

- Customer-controlled deployments. Token-mint hygiene, API-key storage, network policy, and downstream STT / LLM / TTS providers are the customer's responsibility.
- Vulnerabilities that require physical access to the user's device, browser plugins, or admin-level OS access.
- Findings that depend on an explicitly insecure configuration (e.g. embedding `SAA_API_KEY` in browser source).

## Token rotation

SAA tokens (the `SAA_API_KEY` env var, or the `token` field passed to `AttentionClient`) are bearer credentials, treat them like any other API key.

- **Server-issued, short-lived for browser surfaces.** Production browser deployments should mint a short-lived token server-side per session (see [`examples/livekit/web/token_server.py`](./examples/livekit/web/token_server.py) for the token-broker shape). Long-lived tokens should never ship in untrusted bundles.
- **Rotate on suspected compromise.** Re-issue via [attentionlabs.ai](https://attentionlabs.ai); the previous token continues to work until its TTL expires unless explicitly revoked.
- **Per-token kill switch.** The cloud service can invalidate a token immediately on request to security@attentionlabs.ai.
- **Never paste a token into a public repo, pastebin, or screenshot.** Treat them the way you'd treat an OpenAI key.

## What we ask

- Please give us a reasonable window (typically 90 days) to address the issue before public disclosure.
- Please do not exfiltrate, retain, or share user data encountered during research.
- Please do not test against the cloud service in a way that disrupts service for other users.

We do not currently run a paid bug bounty programme. Public acknowledgement (in the release notes) is offered with the reporter's permission.
