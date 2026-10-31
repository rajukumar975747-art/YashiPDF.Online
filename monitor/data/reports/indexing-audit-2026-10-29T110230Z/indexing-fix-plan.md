# Indexing audit fix and review queue

Source live crawl: run 12, 2026-10-29T10:54:11+00:00 UTC. All actions below require no deploy unless stated.

## Applied locally

1. **Malformed footer anchor** — `blog/lossy-vs-lossless-compression-explained.html` had a truncated `<a href="https` fragment. Restored a Contact anchor to the existing `/contact` page, which returned HTTP 200 in run 12. Backed up at `monitor/data/fix-backups/20261029T110020Z-lossy-vs-lossless.html.bak`; exact diff adjacent. Local parser and full monitor suite pass. State `FIXED_LOCALLY`; production still reflects run 12 until a separately authorized deploy and re-audit.

## Review queue (do not auto-publish)

| Priority | Evidence | Safe next action |
|---|---|---|
| High | Seven missing resource URLs: /assets/author-raju.jpg, /blog/PLACEHOLDER_after-single-pdf-file.jpg, /blog/PLACEHOLDER_before-multiple-jpg-images.jpg, /blog/PLACEHOLDER_step1-yashipdf-homepage.jpg, /blog/PLACEHOLDER_step2-upload-jpg-images.jpg, /blog/PLACEHOLDER_step3-convert-download-pdf.jpg, /favicon.ico | Supply verified original image/ICO files or remove/replace individual references after editorial review. Do not invent author imagery or placeholder illustrations. |
| High | Five missing routes: /pdf-merger, /pdf-to-jpg, /pdf-to-jpg-converter, /tools/, /video-resizer | Confirm whether the product exists or was intentionally removed. Redirect only when an equivalent is confirmed; current repository has no confirmed equivalent. |
| Medium | 13 JSON-LD `@type` findings | Inspect each JSON-LD object against visible page content; add only a truthful, supported type or remove malformed/unsupported markup. |
| Medium | Long descriptions on 8 pages | Editorially shorten without changing factual meaning. No automatic rewrite. |
| Low | `/blog/` and hyphenated best-file-format URL redirect to their canonical destinations | Update internal links to final canonical paths when editing those pages; don't create duplicate pages. Sitemap already lists the canonical routes. |
| Low | `/tools/` redirects to `/tools` then returns 404; two source pages link to it | Product/editor review. There is no verified `/tools` route or anchor target; do not redirect to the homepage by assumption. |
| Low | 71 article review records, word counts 1300–8182 | Review intent fit, factual accuracy, useful examples, citations and contextual tool links. These are heuristics, not confirmed indexing causes. |
| Blocked | GSC URL Inspection inspected 0/94 intended pages; no authorized connection | Complete one-time OAuth and confirm exact property. No Google categorization until authorized data arrives. |

## No-change decisions

- No missing asset was fabricated or restored without an original.
- No unsupported redirects, noindex removal, mass indexing request, or content rewrite was made.
- No ranking or Google-selected canonical data is available; none is claimed.
