# YashiPDF indexing audit summary

Generated UTC: 2026-10-29T110230Z  
Live audit run: 12 (2026-10-29T10:51:47+00:00 to 2026-10-29T10:54:11+00:00, complete)  
Origin: https://www.yashipdf.online/  

## Coverage

- **Unique URLs discovered and checked:** 111 / 111 (the crawler run completed).
- **Canonical HTML pages intended for indexing:** 94 sitemap URLs; all 94 returned HTTP 200 in run 12; all matched repository HTML routes exactly. There were 96 successful HTML response variants resolving to 94 unique canonical destinations.
- **GSC:** 0 URLs inspected; coverage 0/94. Connector is `configured_not_connected`. Indexed/non-indexed totals are unavailable and are not inferred.
- **Bing:** `configured_not_connected`. No connected search metrics. Metrics table rows: 0.

## HTTP and indexability

- Final HTTP status distribution: 200: 99, 404: 12. No connection failures were recorded. `robots.txt` returned HTTP 200; sitemap discovery recorded 94 URLs.
- All 94 repository sitemap URLs were present in run 12 and returned HTTP 200. The live sitemap endpoint returned HTTP 200 and reported 94 URLs. Its exact live URL list was not retained for run 12; run 11 had a direct exact-set comparison (94/94). The repository has 94 HTML files; their Vercel clean routes equal the local sitemap URL set. Canonical mismatch count on successful HTML candidates: 0.
- The crawl recorded no meta/X-Robots noindex findings and no robots exclusion findings. This is only the monitor's technical observation, not Google's index decision.
- 3 redirect chains were seen: `/blog/` to `/blog`, the hyphenated best-file-format alias to its percent-encoded canonical, and `/tools/` to `/tools` (which ends at 404). Two successful HTML aliases resolve to already-listed canonical pages. The monitor's two sitemap-coverage and duplicate-metadata flags for those aliases are crawler-variant artifacts; canonical sitemap coverage itself is complete.
- **404s:** 12 total (7 missing assets, 5 missing routes). No original files or truly equivalent routes were available for automatic restoration/redirect. The exact list is in `indexing-issues.json` and the URL inventory.
- **Broken internal references:** 19 references (8 link references and 11 resource references). There were no orphan risks detected among crawled successful HTML pages.
- **Structured data:** 13 JSON-LD objects on pages were reported without `@type`; review actual markup before editing. This is not a Google rich-result or indexing determination.
- **Metadata:** long-description warnings are editorial suggestions; duplicate title/description reports are explained by the two redirect aliases, not two separate canonical pages.
- Article content review queue: 71 pages, word-count range 1300–8182. These are evidence-based editorial checks (accuracy, usefulness, sources and contextual links), not definitive thin-content judgments.
- Fetch p50/p95: 984.0/1734.0 ms including body transfer, **not Core Web Vitals**. Browser runtime errors and field performance are unavailable.

## Fix and verification

- Repaired one deterministic malformed footer anchor in `blog/lossy-vs-lossless-compression-explained.html`; `/contact` was already a live HTTP 200 route in the audit. A backup and unified diff are under ignored `monitor/data/fix-backups`. **State: FIXED_LOCALLY**, not preview or production verified.
- Tests: monitor **18 passed, 0 failed**; Python compile, JS syntax, SEO source audit (94 HTML, zero errors/warnings), sitemap XML/uniqueness and exact route comparison, local parser regression, and `git diff --check` all passed.
- No production deployment or indexing submission was performed.

## Google setup needed

Create/authorize an OAuth client for the Search Console API and URL Inspection API; set `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN`, and `GOOGLE_SEARCH_CONSOLE_SITE_URL` to the exact verified URL-prefix or Domain property. The existing README documents the local setup. After authorization, use the monitor's connector and inspect priority URLs within API quota. Until then every Google index state remains `NOT_VERIFIED_GSC_AUTH_REQUIRED`.
