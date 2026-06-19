# Contributing

SAA is an open repository for the Attention Labs SAA SDKs and framework adapters. Contributions of any size are welcome, bug reports, doc fixes, new framework adapters, performance improvements.

## Before you open a PR

- For **security issues**, do NOT open a public PR or issue, follow the private disclosure path in [`SECURITY.md`](./SECURITY.md).
- For **bug fixes**, an issue with reproduction steps is helpful but not required.
- For **new framework adapters** (e.g. a new telephony or voice-agent platform), open an issue first so we can confirm scope. Adapters live under [`examples/`](./examples/) and consume the cloud SDK; they should not invent new event types or wire formats.
- For **doc-only changes**, just open the PR.
- For **SDK source changes** (`packages/saa-js`, `packages/saa-py`), note that these mirror the published `@attenlabs/saa-js` and `attenlabs-saa` packages. Substantive changes are usually proposed upstream first; this monorepo accepts test/fixture additions and adapter-side fixes more readily.

## Local development

Node 20+, Python 3.10+.

```bash
# JS SDK build
cd packages/saa-js
npm install --no-save
npm run build

# Python SDK install
pip install -e packages/saa-py
```

Each example under [`examples/`](./examples/) has its own `README.md` with run instructions.

## What we do not accept

- Bundled audio, video, or model artifacts (`*.wav`, `*.mp3`, `*.mp4`, `*.onnx`, `*.pt`, `*.tflite`, etc.). Telemetry of internal recordings is out of scope for this repository.
- Re-introducing the shapes intentionally not part of this repo (see [`CLAUDE.md`](./CLAUDE.md) for the list). A meaningful new feature is welcome; resurrecting an excluded surface is not.
- Cross-vendor benchmark tables or invented performance numbers. SAA's operating-point numbers are not published in this repo; don't add benchmark tables here.

## Style

- Markdown: GitHub-flavoured. One H1 per page, sentence-case headings, no trailing exclamation marks.
- Code: no comments that simply re-state what the code does. Comments are for non-obvious *why*.
- Commit messages: imperative mood (`add`, `fix`, `tighten`), reference issue / PR numbers where relevant.

## Code of conduct

This repository follows the [Contributor Covenant Code of Conduct](./CODE_OF_CONDUCT.md). Be civil.

## License

Apache-2.0 across the repo. By contributing, you agree that your contributions are licensed under Apache-2.0. Each package's `LICENSE` file is authoritative for that subtree; the root [`LICENSE`](./LICENSE) is the Apache-2.0 text.
