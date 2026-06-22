<p align="center">
  <a href="../../README.md">
    <img alt="SAA: Selective Auditory Attention" src="../../assets/saa-hero.png" width="326">
  </a>
</p>

# Social cards

Open Graph + Twitter card sources for the SAA repo, npm page, PyPI
page, docs site, and any blog post linking to attentionlabs.ai.

`og-card.svg`. 1200 × 630 (max 5 MB rasterised), 92/8 framing +
wordmark. SVG is the source of truth.

## Rasterise

```sh
rsvg-convert og-card.svg -w 1200 -h 630 -o og-card.png
```

Emits a lossless `og-card.png`. For smaller variants suitable for hosts with strict size limits, run `magick og-card.png -quality 85 og-card.jpg` (or `cwebp og-card.png -o og-card.webp`). Requires `rsvg-convert` (librsvg).

## Where used

- **GitHub repo social preview**: upload `og-card.png` via
  *Settings → Social preview*. GitHub doesn't read meta tags from
  `README.md`.
- **Docs site**: reference `og-card.png` from `og:image` /
  `twitter:image`.
- **npm + PyPI**: link unfurls pull from the linked GitHub repo's
  social preview.


---

<p align="center">
  <sub>An attention labs project. © 2026 Socero Inc.</sub>
</p>
