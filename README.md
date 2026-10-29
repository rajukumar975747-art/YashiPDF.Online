# YashiPDF.Online

Static HTML website hosted on Vercel. A local-first monitoring dashboard is available under [`monitor/`](monitor/).

## Local website monitor (Windows)

Requirements: Python 3.10 or newer. The monitor uses only the Python standard library; it does not add a paid service, database server, Node dependency, or deployment runtime requirement.

From PowerShell in the project root:

```powershell
Copy-Item .env.example .env
python monitor\monitor.py
```

The `.env` file is an example only; Python does not load it automatically. Set optional configuration in the current PowerShell session as needed (for example `$env:YASHIPDF_MAX_URLS = '400'`). Then open <http://127.0.0.1:8765>. The dashboard binds to loopback by default. If no completed audit exists, its scheduler runs one after startup; otherwise it waits until the interval configured by `YASHIPDF_AUDIT_INTERVAL_MINUTES` (default 1440 minutes). Use **Run full audit now** for a manual run. Use `Ctrl+C` to stop.

One-shot audit (also attempts configured search connectors):

```powershell
$env:YASHIPDF_AUDIT_ONCE = '1'
python monitor\monitor.py
Remove-Item Env:YASHIPDF_AUDIT_ONCE
```

Alternatively: `powershell -ExecutionPolicy Bypass -File monitor\start-monitor.ps1 -AuditOnce`.

The dashboard provides discovered and checked URLs, crawl health, a persistent deduplicated issue registry and alert queue, a human review queue for blog pages, per-URL HTTP/metadata verification, Google URL inspection, verified safe fixes with before/after diffs and rollback backups, audit history/logs, HTTP fetch-time signals, search-performance rows, and JSON/Markdown exports. SQLite history is stored in `monitor\data\monitor.sqlite3`; backups and exact pre-fix copies are under `monitor\data\fix-backups`. These paths are ignored by Git and are not uploaded by Vercel. The crawler is sequential, retries transient connection failures with backoff, uses a minimum request interval, parses sitemap indexes and URL sets, follows same-origin links/assets, honors robots rules, and caps each run at `YASHIPDF_MAX_URLS` (default 250). HTTP fetch timing includes body transfer and is not a Core Web Vital. No headless browser is bundled, so this monitor does not claim to collect browser JavaScript errors or field Core Web Vitals. It does not use search-result scraping.

Routine autopilot runs apply only the explicit exact-route link corrections in `monitor\autopilot.py` when the same audit verified each replacement target as HTTP 2xx. Changed files are backed up, diffed, checked by `scripts\seo-audit.js`, and rolled back automatically if validation fails. URL migrations, redirects, missing assets/pages, sitemap/indexing changes, and article rewrites remain review-only. Alert rows persist in SQLite; new alerts append locally to `monitor\data\alerts.jsonl` by default (set `YASHIPDF_LOCAL_NOTIFY=0` to disable the file outbox). No notification leaves the device.

Connection errors distinguish DNS, proxy, TLS, timeout, and refused-connection failures. HTTP responses are separately recorded. A local resolver/network failure is not reported as production downtime. If robots.txt is unavailable because of a connection failure or server error, the crawler preserves the discovered inventory but stops before fetching site routes.

### Optional Google Search Console

1. In Google Cloud, create an OAuth client, enable the Search Console API, and authorize read-only scope `https://www.googleapis.com/auth/webmasters.readonly` for an account that can read the verified property.
2. Obtain an OAuth refresh token with offline access for that account. Keep all OAuth values private.
3. Set these environment variables in PowerShell before launching the monitor:

   ```powershell
   $env:GOOGLE_CLIENT_ID = '...'
   $env:GOOGLE_CLIENT_SECRET = '...'
   $env:GOOGLE_REFRESH_TOKEN = '...'
   $env:GOOGLE_SEARCH_CONSOLE_SITE_URL = 'sc-domain:yashipdf.online'
   ```

   Use the exact property identifier shown in Search Console; a URL-prefix property can instead be `https://www.yashipdf.online/`.
4. The monitor refreshes a short-lived access token server-side, reads the property sitemap list and requests Search Analytics rows for site, page, query, date, country, device, and query/page dimensions. Current and previous finalized 28-day windows are stored for comparison; the end date is three days before current UTC time to avoid partial reporting. Google may limit rows and quota. Snapshots are local. Search Console position is average position, not an exact live rank.

Google URL Inspection is available per URL in the dashboard. It requires the exact verified property plus credentials authorized for the URL Inspection API. The returned verdict/canonical/index state is an inspection snapshot, not a guarantee of indexing. API authorization, property access, network, and quota errors are recorded as connector status rather than a false connected state.

### Bing Webmaster Tools

The monitor uses Microsoft's documented Bing Webmaster JSON/HTTP read protocol, not SOAP or POX. It authenticates with the verified-account API key or OAuth refresh token, rate-limits calls, and reads account sites, site/query/page traffic, crawl stats, and sitemap feeds when those methods are available to the account. `BING_WEBMASTER_SITE_URL` selects the verified property. Set either `BING_WEBMASTER_API_KEY` or the three `BING_WEBMASTER_CLIENT_ID`, `BING_WEBMASTER_CLIENT_SECRET`, and `BING_WEBMASTER_REFRESH_TOKEN` values. The key/query parameter never appears in dashboard errors. Microsoft notes SOAP and POX retirement while publishing JSON/HTTP endpoint formats in its [protocol documentation](https://learn.microsoft.com/en-us/bingwebmaster/api-protocols); see also [OAuth access](https://learn.microsoft.com/en-us/bingwebmaster/oauth2). Bing report date coverage is whatever the endpoint returns; it is not presented as a controlled date range unless supplied by the API.

### Free backlink prospect tracker

The existing monitor includes a local SQLite prospect and verification module at `/backlinks`, with CSV, JSON, and Markdown exports at `/backlinks.csv`, `/backlinks.json`, and `/backlinks.md`. The public research seed is `monitor\backlink_opportunities.json`; profiles and editorial drafts are in `monitor\backlink_drafts.md`. The module stores source/check dates, cost/account requirements, tailored copy, manual status transitions, referring pages, HTTP results, anchor text, and detected `rel` class. It counts a backlink as verified only after fetching the public referring page and finding a link to the recorded YashiPDF destination. Public-host DNS checks block local/private destinations, redirects are constrained to the initial HTTPS host, and the verifier does not submit forms or send outreach. Free prospects, pending human actions, verified links, and removed/broken links are separate dashboard counts. No link is treated as a ranking guarantee.

Research status (2026-10-29): two opportunities are explicitly verified as free standard submissions (Ignlab Launch and AlternativeTo); Product Hunt's official launch guide does not state a submission price, so its prospect is retained but excluded from the verified-free count. Nothing has been submitted. See `monitor\backlink_drafts.md` for links, source evidence, excluded candidates, and ready-to-review copy.

No API integration can claim a ranking increase from an empty response. Bing query/page values and Google average position remain separate source-specific measurements.

### Windows autopilot setup

Run this one time from PowerShell to register and start the current-user Task Scheduler task, then wait for and verify the loopback health endpoint:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\Student\Documents\GitHub\YashiPDF.Online\monitor\install-autopilot.ps1"
```

The task starts at user sign-in, runs the monitor directly with `pythonw.exe` when available, restarts after task-process failure (up to five attempts at five-minute intervals), starts when available, and ignores overlapping instances. Running the monitor directly means Task Scheduler owns the process and can stop it without leaving a Python child behind. The installer detects old/duplicate YashiPDF monitor processes, refuses replacement during an active audit, and only stops processes whose command line names this project's `monitor.py`. The monitor scheduler runs a full audit every 24 hours by default, retries failed audits after 15 minutes, and uses a SQLite process lease. On restart it marks abandoned runs failed and frees a dead process lease. Audit history and event logs are in `monitor\data\monitor.sqlite3`; windowless startup/runtime messages are in `%LOCALAPPDATA%\YashiPDFMonitor\logs\monitor-host.log`. The dashboard is loopback-only. The task runs while the interactive user session is available; after a reboot it starts at that user's sign-in. Stop it with `Stop-ScheduledTask -TaskName 'YashiPDF.Online Autopilot Monitor'`.

To remove only the scheduled task while leaving code, databases, reports, and backups intact, run `Stop-ScheduledTask -TaskName 'YashiPDF.Online Autopilot Monitor' -ErrorAction SilentlyContinue` followed by `Unregister-ScheduledTask -TaskName 'YashiPDF.Online Autopilot Monitor' -Confirm:$false`. Run the installer again to recover/re-register the task. The installer waits for a healthy loopback endpoint before reporting success.

### Configuration

See [`.env.example`](.env.example). Supported settings include `YASHIPDF_SITE_URL`, `YASHIPDF_HOST`, `YASHIPDF_PORT`, `YASHIPDF_DATA_DIR`, `YASHIPDF_MAX_URLS`, `YASHIPDF_AUDIT_INTERVAL_MINUTES`, `YASHIPDF_RETRY_INTERVAL_MINUTES`, `YASHIPDF_REQUEST_TIMEOUT`, `YASHIPDF_REQUEST_RETRIES`, `YASHIPDF_CRAWL_DELAY_SECONDS`, `YASHIPDF_API_DELAY_SECONDS`, and `YASHIPDF_LOCAL_NOTIFY`, plus Google and Bing settings above. The monitor does not load `.env` automatically. Credentials should be set in the current user's environment or another private secret store, never committed or pasted into issue reports.

### Troubleshooting and recovery

- **Dashboard won't start:** check Python with `python --version`; check whether port 8765 is already in use and set `$env:YASHIPDF_PORT = '8766'` if needed.
- **Live URLs show HTTP status 0:** the host could not be resolved/reached before timeout. Check DNS, internet connectivity, firewall and `YASHIPDF_REQUEST_TIMEOUT`. The audit distinguishes this from a real HTTP response.
- **Google says not configured:** set all three OAuth values and the exact property identifier. **Google says error:** check the OAuth refresh token, readonly scope, enabled API, verified property permission, quotas, and network. The token itself is never shown in the dashboard.
- **Interrupted audit after restart:** the next start marks persisted `running` jobs failed with a restart reason; completed history remains in SQLite.
- **Restore history:** stop the monitor, copy a backup of `monitor\data` back into place, then restart.
- **Revert source change:** `git diff -- sitemap.xml` shows the sitemap addition made for the `/blog` canonical route. Review the diff before reverting with `git restore -- sitemap.xml`.

## Audit and tests

Run the source metadata checker and the local monitor tests:

```powershell
node scripts\seo-audit.js
python -m unittest discover -s monitor -p test_monitor.py -v
```

The monitor tests use deterministic local fixture responses for repeatability. They do not claim to verify production connectivity, Google/Bing account data, indexing, ranking, Lighthouse, field Core Web Vitals, deployment, or production tool workflows.

## Production site

The website remains static HTML with its existing Vercel rewrites and tool implementation. `monitor/` is a local operations dashboard and is not deployed as part of the public site. Run a Vercel preview separately before deploying any website source changes. No production deployment is performed by this monitor.
