<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero.svg" width="326">
  </a>
</p>

# SAA diagram system

Style tokens for every SVG in the repo. Keep new diagrams consistent with what's already here.

## Canonical sizes

| Tier | Where | viewBox |
|---|---|---|
| **Hero** | top of each `packages/*/README` | 820 × 220-280 |
| **Tile** | top of each `examples/*/README` | 820 × 200-220 |
| **Diagram** | section-level illustration | 820 × 320-460 |

`width="820"` is canonical for **every** SVG. No exceptions.

## Tokens

- Title: `22 / 700 / #0f172a`. Subtitle: `13 / 400 / #475569`. Section label: `10-11 / 700 / #94a3b8 / 0.08em`. Card label: `13 / 700 / #0f172a`. Card subtitle: `11 / #475569`.
- Card fills: neutral `#ffffff` / stroke `#e5e7eb`. Positive (addressed): `#ecfdf5` / stroke `#10b981 sw 1.5`. Dark (cloud): `linear(#0f172a → #1e293b)`. All `rx: 12`.
- Background gradient `#bg` = `linear top→bottom · #fafafa → #f1f5f9`.
- Greens (decision-positive only): primary `#10b981` · mid `#059669` · dark `#065f46` · fill `#ecfdf5`.
- Grays (side speech, dropped): bg `#f3f4f6` · stroke `#d1d5db / #9ca3af` · text `#6b7280` · dashed `4 4`.
- Arrows: neutral `stroke #374151 sw 1.5` · addressed `stroke #059669 sw 2`.
- Adapter brand accents live **only** on the adapter's own pill. The SAA gate keeps canonical green.
- Font: `ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`. Monospaced: `ui-monospace, SFMono-Regular, Menlo, monospace`. Set on the root `<svg>`.

## Accessibility

`role="img"` plus `<title>` and `<desc>` linked via `aria-labelledby`. `<title>` is one declarative sentence; `<desc>` is one or two sentences a screen reader can convey.

## Adding a new diagram

1. Pick the smallest tier that fits.
2. Title is one sentence; subtitle is one sentence of context.
3. Decision-positive elements use canonical green; everything else stays neutral.
4. Adapter brand colours live only on adapter pills.


---

<p align="center">
  <sub>An attention labs project. © 2026.</sub>
</p>
