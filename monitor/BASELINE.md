# YashiPDF.Online monitoring baseline

Baseline captured 2026-10-29. This report separates repository evidence from production evidence.

## Repository and route inventory

- Existing site: static HTML with Vercel clean URLs, redirects, rewrites, a Python PDF API, and no JavaScript package/build manifest.
- Source audit: 94 HTML files; `node scripts/seo-audit.js` reported 0 metadata/canonical errors and 1 warning: duplicate `id="related-articles"` in `blog/jpg-vs-png-which-format-should-you-use.html`.
- Sitemap before change: 93 unique URLs. The canonical page `https://www.yashipdf.online/blog` was absent even though `blog.html` declares it and `vercel.json` rewrites `/blog` to that file.
- Safe fix applied: added the verified `/blog` route to `sitemap.xml`. Parsed sitemap after the change: 94 unique URLs. A source comparison found no canonical page URL missing from the sitemap.
- The 68 directly stored `blog/*.html` files are not the complete source-page count; a nested blog HTML file exists as well. The repository checker counted all 94 HTML files, including tool, utility, and blog pages.

## Connectivity and live crawl

Windows host checks (2026-10-29):

- Windows DNS resolves `www.yashipdf.online` via CNAME `efcb740cf1ce120a.vercel-dns-017.com`, A records `216.198.79.65` and `64.29.17.65`, and resolver-provided AAAA answers (NAT64 form in this environment). The crawler's Python resolver returned a different Vercel A answer set during the sequential run, also with a successful crawl; DNS can return different addresses across resolver paths.
- TCP port 443 succeeds; `curl --head --location` returned HTTPS `200 OK`, `Server: Vercel`, and HSTS. Therefore the site was reachable from this Windows host during this check.
- WinHTTP is configured for direct access; no proxy environment variables were set. The sandboxed process previously could not use Windows DNS, which caused the earlier false-local-connectivity findings. A direct `Resolve-DnsName -Server 1.1.1.1` timed out, so independent public-resolver access is restricted here; it does not override the successful system-resolver/HTTPS result.
- Windows Defender Firewall is enabled for Domain, Private and Public profiles. Profile default actions are `NotConfigured` (rule-based); TCP 443 succeeds, and no blocked outbound path was established by these checks.
- No DNS, proxy or firewall settings were changed. These checks do not establish uptime from all networks or search-engine indexation.

Autopilot validation production crawl (run 9, 2026-10-29) completed sequentially and was persisted to ignored `monitor/data/monitor.sqlite3`:

- Windows system DNS resolved the domain, proxy configuration was absent, and `robots.txt` and the deployed sitemap both returned HTTP 200. This run found 120 URLs (93 in the deployed sitemap plus same-origin discovered links) and checked all 120.
- Responses: 99 HTTP 200, 21 HTTP 404, and 0 connection failures; response-time p50/p95 (including body transfer) were 969/1797 ms. The prior evidence run 8 confirmed all seven replacement destinations (`/blog/convert-tiff-to-jpg-online`, `/crop`, `/img-pdf`, `/compress`, `/png-jpg`, `/resize`, `/favicon.png`) returned HTTP 200.
- Run 9 still sees 21 production 404 URLs because local source repairs have not been deployed. Six original image assets are unavailable; `/favicon.ico` has no original ICO; five unimplemented product routes remain without equivalent targets. Full per-URL classification and evidence are in `monitor/404-review.md`.
- A malformed source href in `blog/lossy-vs-lossless-compression-explained.html` is classified as `malformed_internal_link` and excluded from network requests; it no longer appears as a crawler connection failure.
- Local source files now have 25 exact link corrections across 12 pages, plus one favicon MIME correction. Each change has an exact backup and diff under ignored `monitor/data/fix-backups`; the source checker passed. The production crawler still reads deployed content, so these fixes do not change public behavior until deployed. No production writes, DNS changes or deployment were performed.

## Monitoring app verification

- Windows Task Scheduler task `YashiPDF.Online Autopilot Monitor` was registered for current-user sign-in with IgnoreNew overlap policy, automatic task-process restart (five attempts/five minutes), StartWhenAvailable, and no execution time limit. Installer exited 0. I stopped and restarted the task; it launched a new process, returned to `Running`, reported Task Scheduler result `267009` (task is running), and `/health` returned HTTP 200. It runs the monitor's persistent SQLite scheduler (24-hour default; 15-minute failed-run retry).
- Dashboard `/health`, `/`, `/report.json`, and `/report.md` returned HTTP 200. The task uses `%LOCALAPPDATA%\\YashiPDFMonitor\\logs\\monitor-host.log`; DB audit/event history and local JSONL alerts persist under `monitor/data`.
- `python -m unittest discover -s monitor -p 'test_monitor.py' -v`: 17/17 passed, including restart recovery, retry backoff, due/duplicate lease, safe-fix backups/diffs and rollback, local alert deduplication, mocked authorized Google/Bing paths, and dashboard controls.
- `python -m py_compile monitor\\monitor.py monitor\\autopilot.py monitor\\test_monitor.py monitor\\diagnose_audit.py`: passed. `node scripts\\seo-audit.js`: 94 HTML pages, 0 errors, 1 duplicate-ID warning. `git diff --check`: passed. `monitor/data/monitor.sqlite3` records the real run 9.
- Scheduler smoke forced a due run and duplicate suppression in unit tests; startup recovery test marked an abandoned run failed and removed its dead-owner lease. Task stop/start was tested; full machine reboot/sign-in was not simulated.
- Google OAuth credentials are absent; connector status is `configured_not_connected`; no search metrics are reported as real.
- Bing credentials are absent; connector status is `configured_not_connected`; no search metrics are reported as real.
- Bing connector uses the official JSON/HTTP interface documented by Microsoft. SOAP and POX are not used. No real Bing account request was possible without credentials.

## Remaining queue and rollback

- Review the duplicate HTML ID noted above. It is a source warning only; its effect was not verified in a browser.
- Review substantive article usefulness and accuracy in the dashboard queue. The monitor intentionally does not rewrite article bodies.
- The changed site files and `/blog` sitemap entry are local only. Production deployment requires an installed/configured Vercel or authorized Git deployment mechanism and a preview/rollback path; this was not exercised. No `package.json` build script or headless browser is present, so there is no project build/browser-JavaScript/Core Web Vitals check. HTTP body-transfer timing is available but is not a Core Web Vital.
- At the initial baseline, branch creation was blocked by the command sandbox's read-only `.git` mount. A later scoped write for the user-requested review branch succeeded; the current branch and commit status are reported in the final acceptance summary.

## Follow-up verification, 2026-10-29

- A fresh dashboard-triggered production audit (run 10) completed at 08:34 UTC: 120/120 URLs checked, 99 HTTP 200, 21 HTTP 404, 0 connection failures, `robots.txt` HTTP 200, and the sitemap contained 93 deployed URLs. Response-time p50/p95 was 1344/4188 ms including body transfer, not Core Web Vitals. The 21 production 404s remain; local source corrections are not deployed.
- `/health`, `/`, `/report.json`, and `/report.md` all returned HTTP 200 after task installation. A second `/run` submission during run 10 returned HTTP 409, confirming dashboard duplicate-run suppression.
- A controlled task stop/start exposed that the former PowerShell wrapper left its child Python monitor behind. The task was corrected to execute `pythonw.exe` directly, with windowless runtime output written to `%LOCALAPPDATA%\\YashiPDFMonitor\\logs\\monitor-host.log`. The installer now refuses to replace a monitor during an active audit and cleans only processes whose command line names this repository's `monitor.py`.
- After correction, Task Scheduler stop produced zero monitor processes and state `Ready`; start returned it to `Running` with exactly one `pythonw.exe` process/listener on `127.0.0.1:8765` and `/health` HTTP 200. No reboot was performed.
- Re-running the installer against the healthy direct-process task preserved the same PID (`34712`) and left the task's recorded run time unchanged, verifying idempotent installation. The installer compares Task Scheduler's ISO-8601 execution limit (`PT0S`) as text to avoid restarting an already-correct task.
- Task configuration was verified: current-user logon trigger, `IgnoreNew`, `StartWhenAvailable`, five restart attempts at five-minute intervals, and no execution time limit. Last Task Scheduler result `267009` means the task is running.
- The Windows UI browser automation stopped because it could not confidently establish the active browser URL. API/HTTP checks verified the dashboard and exports; a visual browser interaction was not completed.
- Full monitor tests after the task lifecycle correction: 18/18 passed. SEO source audit: 94 HTML files, 0 errors, 1 duplicate-ID warning. Python compilation, JavaScript syntax, Vercel JSON and sitemap XML parsing, and `git diff --check` passed.
