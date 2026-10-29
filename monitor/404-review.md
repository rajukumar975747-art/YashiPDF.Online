# Live 404 review

Evidence source: persisted production crawls of 2026-10-29, runs 7–9. All listed URLs returned HTTP 404 from `www.yashipdf.online`; run 9 was after the local source link repairs and still sees the deployed, unrepaired HTML. A 404 is not evidence that deleting the source or adding a redirect is safe. No 404 was redirected to the home page.

## Missing assets (original not present locally)

| URL | Classification and action |
|---|---|
| `/assets/author-raju.jpg` | Missing author asset. No original image exists in the repository; do not synthesize an identity image. |
| `/blog/PLACEHOLDER_after-single-pdf-file.jpg` | Missing draft/placeholder illustration. Original unavailable; leave for editor. |
| `/blog/PLACEHOLDER_before-multiple-jpg-images.jpg` | Missing draft/placeholder illustration. Original unavailable; leave for editor. |
| `/blog/PLACEHOLDER_step1-yashipdf-homepage.jpg` | Missing draft/placeholder illustration. Original unavailable; leave for editor. |
| `/blog/PLACEHOLDER_step2-upload-jpg-images.jpg` | Missing draft/placeholder illustration. Original unavailable; leave for editor. |
| `/blog/PLACEHOLDER_step3-convert-download-pdf.jpg` | Missing draft/placeholder illustration. Original unavailable; leave for editor. |
| `/favicon.ico` | Missing ICO asset. The repository has a working `favicon.png`, so explicit HTML favicon links can safely use that existing PNG. Automatic browser lookup of `/favicon.ico` remains unavailable unless an original ICO is supplied. |

## Accidentally broken internal links with verified equivalents

The following route corrections are in the autopilot allowlist. Run 8 confirmed the replacement routes returned HTTP 200; autopilot then changed 25 exact href/src attributes across 12 source files, with per-file backups and diffs, and the source SEO regression check passed. One favicon MIME correction has its own backup and diff. Failed validation restored all files during earlier attempts. Run 9 still sees deployed 404s until a separately authorized deployment publishes these local source fixes.

| 404 URL | Verified equivalent | Evidence |
|---|---|---|
| `/blog/convert-tiff-to-gpj-online` | `/blog/convert-tiff-to-jpg-online` | Existing article title and route say “Convert TIFF to JPG”; only `gpj` is transposed. |
| `/blog/crop-image` | `/crop` | Broken links are labeled “Crop Image Tool”; `crop.html` is the existing canonical crop tool. |
| `/blog/jpg-to-pdf-converter` | `/img-pdf` | Broken links are labeled “JPG to PDF Converter”; existing `/img-pdf` tool is the image-to-PDF route. |
| `/crop-image` | `/crop` | Tool anchor text and existing crop tool match. |
| `/compress-image-to-50kb-online-free` | `/compress` | Existing compressor explicitly supports 50KB and custom size targets. |
| `/image-compressor` | `/compress` | Existing canonical image compressor. |
| `/jpg-to-pdf-converter` | `/img-pdf` | Existing image-to-PDF tool. |
| `/png-to-jpg` | `/png-jpg` | Existing PNG-to-JPG converter. |
| `/resize-image-to-200x230-pixels-online` | `/resize` | Existing resize tool supports custom dimensions; the broken link labels it an image resizer. |

## Obsolete or unimplemented routes; no equivalent confirmed

| URL | Classification and action |
|---|---|
| `/pdf-merger` | Route absent; `/pdf-split` only splits PDFs and is not equivalent. No redirect. |
| `/pdf-to-jpg` | Route absent; no PDF-to-image tool exists in the repository. No redirect. |
| `/pdf-to-jpg-converter` | Route absent; no equivalent tool exists in the repository. No redirect. |
| `/tools/` | No separate tools-index route exists; the homepage is not assumed to be equivalent. No redirect. |
| `/video-resizer` | No video-resizing page or asset exists locally. No page generated. |

No evidence established an intentional removal for any route. The unimplemented routes need a product/editor decision before restoration, removal confirmation, or redirection. Placeholder assets and author imagery need their original sources before restoration. The local audit must verify each allowlisted destination as 2xx before changing source links.
