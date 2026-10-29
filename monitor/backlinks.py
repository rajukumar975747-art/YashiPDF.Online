"""Local-first prospect tracking and conservative public backlink verification."""
from __future__ import annotations

import csv
import html
import io
import ipaddress
import json
import re
import socket
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

STATUSES = ("prospect", "draft_prepared", "submitted", "published_unverified", "verified", "rejected", "removed")
MANUAL_STATUSES = tuple(status for status in STATUSES if status != "verified")
TRANSITIONS = {
    "prospect": {"draft_prepared", "rejected"},
    "draft_prepared": {"submitted", "rejected", "prospect"},
    "submitted": {"published_unverified", "rejected"},
    "published_unverified": {"verified", "rejected", "removed"},
    "verified": {"published_unverified", "removed"},
    "rejected": {"prospect"},
    "removed": {"published_unverified", "rejected"},
}
YASHIPDF_HOSTS = {"yashipdf.online", "www.yashipdf.online"}
SEED_FILE = Path(__file__).with_name("backlink_opportunities.json")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(value: str, *, target: bool = False) -> tuple[str, str]:
    """Validate an HTTPS URL and return (display URL, dedupe key).

    This validates URL syntax only. Network verification separately rejects
    hosts that resolve to non-public addresses.
    """
    if not isinstance(value, str) or not value or len(value) > 2048 or any(ord(ch) < 32 for ch in value):
        raise ValueError("Enter a valid HTTPS URL (maximum 2048 characters).")
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("The URL is malformed.") from exc
    if parsed.scheme.lower() != "https" or not hostname or parsed.username or parsed.password:
        raise ValueError("Only absolute HTTPS URLs without embedded credentials are accepted.")
    if port not in (None, 443):
        raise ValueError("Non-standard URL ports are not accepted.")
    host = hostname.rstrip(".").lower()
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal", ".test")):
        raise ValueError("Local or reserved hostnames are not accepted.")
    try:
        address = ipaddress.ip_address(host)
        if not address.is_global:
            raise ValueError("Non-public IP addresses are not accepted.")
    except ValueError as exc:
        if "not accepted" in str(exc):
            raise
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError as idna_error:
            raise ValueError("The hostname is invalid.") from idna_error
        if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host):
            raise ValueError("The hostname is invalid.")
    if target and host not in YASHIPDF_HOSTS:
        raise ValueError("The destination must be on yashipdf.online.")
    path = parsed.path or "/"
    normalized_path = path.rstrip("/") or "/"
    display = urllib.parse.urlunsplit(("https", host, path, parsed.query, ""))
    key = urllib.parse.urlunsplit(("https", host, normalized_path, "", ""))
    return display, key


def _safe_public_host(host: str, resolver=socket.getaddrinfo) -> None:
    if not host:
        raise ValueError("The referring page has no hostname.")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        try:
            results = resolver(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"Referring host DNS lookup failed: {type(exc).__name__}.") from exc
        addresses = []
        for result in results:
            try:
                addresses.append(ipaddress.ip_address(result[4][0].split("%", 1)[0]))
            except (ValueError, IndexError, TypeError):
                continue
    if not addresses:
        raise ValueError("Referring host did not resolve to a usable address.")
    if any(not address.is_global for address in addresses):
        raise ValueError("Referring host resolves to a non-public address; verification was blocked.")


def initialize(db) -> int:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS backlink_prospects(
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        website TEXT NOT NULL,
        submission_url TEXT NOT NULL,
        submission_key TEXT NOT NULL UNIQUE,
        source_url TEXT NOT NULL,
        why_relevant TEXT NOT NULL,
        free_submission INTEGER NOT NULL,
        registration_required INTEGER NOT NULL,
        link_type TEXT NOT NULL,
        instructions TEXT NOT NULL,
        target_url TEXT NOT NULL,
        category TEXT NOT NULL,
        description TEXT NOT NULL,
        tags TEXT NOT NULL,
        submission_draft TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'prospect',
        checked_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        submitted_at TEXT,
        referring_page TEXT,
        verified_at TEXT,
        last_http_status INTEGER,
        last_result TEXT,
        anchor_text TEXT,
        link_rel TEXT,
        first_seen TEXT,
        last_verified TEXT,
        removal_reason TEXT
    );
    CREATE TABLE IF NOT EXISTS backlink_verifications(
        id INTEGER PRIMARY KEY,
        prospect_id INTEGER NOT NULL REFERENCES backlink_prospects(id),
        checked_at TEXT NOT NULL,
        referring_page TEXT NOT NULL,
        destination_url TEXT NOT NULL,
        http_status INTEGER,
        final_url TEXT,
        result TEXT NOT NULL,
        reason TEXT NOT NULL,
        anchor_text TEXT,
        link_rel TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_backlink_status ON backlink_prospects(status);
    CREATE INDEX IF NOT EXISTS idx_backlink_verification_prospect ON backlink_verifications(prospect_id, checked_at);
    """)
    if SEED_FILE.exists():
        seed = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        for item in seed:
            try:
                add_prospect(db, item, seed=True)
            except (ValueError, KeyError, sqlite_error_types()):
                # A bad or duplicate seed must not stop the main site monitor.
                continue
    db.commit()
    return db.execute("SELECT COUNT(*) FROM backlink_prospects").fetchone()[0]


def sqlite_error_types():
    # Lazy lookup avoids coupling the module to a specific sqlite connection.
    import sqlite3
    return sqlite3.IntegrityError


def add_prospect(db, data: dict, *, seed: bool = False) -> tuple[int, bool]:
    name = str(data.get("name", "")).strip()[:160]
    if not name:
        raise ValueError("Prospect name is required.")
    website, _ = normalize_url(str(data.get("website", "")))
    submission_url, submission_key = normalize_url(str(data.get("submission_url", "")))
    source_url, _ = normalize_url(str(data.get("source_url", submission_url)))
    target_url, _ = normalize_url(str(data.get("target_url", "https://www.yashipdf.online/")), target=True)
    free_value = data.get("free_submission", False)
    free_submission = int(free_value is True or str(free_value).lower() in ("1", "true", "yes", "free"))
    registered_value = data.get("registration_required", False)
    registration_required = int(registered_value is True or str(registered_value).lower() in ("1", "true", "yes"))
    checked = str(data.get("checked_at") or now()[:10])[:32]
    status = "prospect" if seed else "prospect"
    created = now()
    before = db.total_changes
    db.execute("""INSERT OR IGNORE INTO backlink_prospects
        (name,website,submission_url,submission_key,source_url,why_relevant,free_submission,registration_required,
         link_type,instructions,target_url,category,description,tags,submission_draft,status,checked_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (name,website,submission_url,submission_key,source_url,str(data.get("why_relevant", ""))[:2000],free_submission,
         registration_required,str(data.get("link_type", "Unknown until verified"))[:500],str(data.get("instructions", ""))[:3000],
         target_url,str(data.get("category", ""))[:160],str(data.get("description", ""))[:1200],str(data.get("tags", ""))[:500],
         str(data.get("submission_draft", ""))[:4000],status,checked,created,created))
    inserted = db.total_changes > before
    db.commit()
    row = db.execute("SELECT id FROM backlink_prospects WHERE submission_key=?", (submission_key,)).fetchone()
    return int(row[0]), inserted


def get_prospect(db, prospect_id: int):
    return db.execute("SELECT * FROM backlink_prospects WHERE id=?", (int(prospect_id),)).fetchone()


def list_prospects(db):
    return db.execute("SELECT * FROM backlink_prospects ORDER BY free_submission DESC, name COLLATE NOCASE").fetchall()


def transition_status(db, prospect_id: int, status: str, *, referring_page: str = "", removal_reason: str = "") -> None:
    status = str(status or "")
    if status == "verified":
        raise ValueError("A backlink can only become verified after public-page link verification succeeds.")
    if status not in STATUSES:
        raise ValueError("Unknown prospect status.")
    row = get_prospect(db, prospect_id)
    if not row:
        raise ValueError("Prospect not found.")
    if status != row["status"] and status not in TRANSITIONS.get(row["status"], set()):
        raise ValueError(f"Invalid status transition: {row['status']} → {status}.")
    referring = row["referring_page"]
    submitted = row["submitted_at"]
    if status == "submitted":
        submitted = now()
    if status == "published_unverified":
        referring, _ = normalize_url(referring_page)
    reason = str(removal_reason or "")[:1200] if status == "removed" else row["removal_reason"]
    db.execute("UPDATE backlink_prospects SET status=?,referring_page=?,submitted_at=?,removal_reason=?,updated_at=? WHERE id=?",
               (status, referring, submitted, reason, now(), int(prospect_id)))
    db.commit()


class _BacklinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            a = dict(attrs)
            self.current = {"href": a.get("href", ""), "rel": a.get("rel", ""), "text": ""}

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current is not None:
            self.anchors.append(self.current)
            self.current = None


def classify_rel(rel: str) -> str:
    values = {x.lower() for x in re.split(r"\s+", str(rel or "").strip()) if x}
    classes=[x for x in ("sponsored","ugc","nofollow") if x in values]
    return " ".join(classes) if classes else "follow"


def _destination_matches(href: str, base_url: str, expected: str) -> bool:
    try:
        candidate, _ = normalize_url(urllib.parse.urljoin(base_url, href), target=True)
        dest, _ = normalize_url(expected, target=True)
    except ValueError:
        return False
    a, b = urllib.parse.urlsplit(candidate), urllib.parse.urlsplit(dest)
    path_a = urllib.parse.unquote(a.path).rstrip("/") or "/"
    path_b = urllib.parse.unquote(b.path).rstrip("/") or "/"
    return a.hostname in YASHIPDF_HOSTS and b.hostname in YASHIPDF_HOSTS and path_a == path_b and (not b.query or a.query == b.query)


def verify_backlink(db, prospect_id: int, fetcher, resolver=socket.getaddrinfo) -> dict:
    row = get_prospect(db, prospect_id)
    if not row:
        raise ValueError("Prospect not found.")
    recent=db.execute("SELECT checked_at FROM backlink_verifications WHERE prospect_id=? ORDER BY id DESC LIMIT 1",(int(prospect_id),)).fetchone()
    if recent:
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(recent[0])).total_seconds()
            if age < 60: raise ValueError("Backlink verification is rate-limited to one request per prospect per minute.")
        except ValueError as exc:
            if "rate-limited" in str(exc): raise
    referrer, _ = normalize_url(row["referring_page"] or "")
    target, _ = normalize_url(row["target_url"], target=True)
    host = urllib.parse.urlsplit(referrer).hostname
    checked = now()
    status = 0
    final = referrer
    anchor_text = ""
    link_rel = ""
    reason = ""
    result = "not_verified"
    try:
        _safe_public_host(host, resolver=resolver)
        status, final, body, ctype, error, redirects, headers = fetcher(referrer)
        if status != 200:
            reason = error or f"Referring page returned HTTP {status}."
        elif ctype not in ("text/html", "application/xhtml+xml"):
            reason = f"Referring page returned {ctype or 'unknown content type'}, not HTML."
        else:
            parser = _BacklinkParser()
            parser.feed(body.decode("utf-8", "replace"))
            matched = next((a for a in parser.anchors if _destination_matches(a["href"], final, target)), None)
            if matched:
                anchor_text = " ".join(matched["text"].split())[:500]
                link_rel = classify_rel(matched["rel"])
                reason = "Public referring page returned HTTP 200 and contains a link to the recorded YashiPDF destination."
                result = "verified"
            else:
                reason = "HTTP 200 page was fetched, but no link to the recorded YashiPDF destination was found."
        if status != 200 and not reason:
            reason = f"Referring page returned HTTP {status}."
    except (ValueError, OSError, UnicodeError) as exc:
        reason = str(exc)[:1200]
    except Exception as exc:
        reason = f"Verification failed ({type(exc).__name__}): {str(exc)[:700]}"

    previous = row["status"]
    if result == "verified":
        new_status = "verified"
    elif previous == "verified":
        new_status = "removed"
    else:
        new_status = "published_unverified"
    if new_status != previous and new_status not in TRANSITIONS.get(previous, set()):
        # Failed verification of a pre-submission prospect never promotes it to published.
        new_status = previous
    db.execute("""INSERT INTO backlink_verifications
        (prospect_id,checked_at,referring_page,destination_url,http_status,final_url,result,reason,anchor_text,link_rel)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (int(prospect_id),checked,referrer,target,status or None,final,result,reason,anchor_text or None,link_rel or None))
    first_seen = checked if result == "verified" and not row["first_seen"] else row["first_seen"]
    verified_at = checked if result == "verified" else row["verified_at"]
    removal_reason = reason if new_status == "removed" else row["removal_reason"]
    db.execute("""UPDATE backlink_prospects SET status=?,last_http_status=?,last_result=?,anchor_text=?,link_rel=?,
        first_seen=?,last_verified=?,verified_at=?,removal_reason=?,updated_at=? WHERE id=?""",
        (new_status,status or None,result,anchor_text or None,link_rel or None,first_seen,checked if result=="verified" else row["last_verified"],verified_at,removal_reason,checked,int(prospect_id)))
    db.commit()
    return {"status":new_status,"result":result,"http_status":status or None,"final_url":final,"anchor_text":anchor_text or None,"link_rel":link_rel or None,"reason":reason,"checked_at":checked,"redirects":redirects if 'redirects' in locals() else []}


def stats(db) -> dict:
    total = db.execute("SELECT COUNT(*) FROM backlink_prospects").fetchone()[0]
    free = db.execute("SELECT COUNT(*) FROM backlink_prospects WHERE free_submission=1").fetchone()[0]
    ready = db.execute("SELECT COUNT(*) FROM backlink_prospects WHERE status='draft_prepared'").fetchone()[0]
    submitted = db.execute("SELECT COUNT(*) FROM backlink_prospects WHERE status IN ('submitted','published_unverified','verified')").fetchone()[0]
    verified = db.execute("SELECT COUNT(*) FROM backlink_prospects WHERE status='verified'").fetchone()[0]
    removed = db.execute("SELECT COUNT(*) FROM backlink_prospects WHERE status='removed'").fetchone()[0]
    pending = db.execute("SELECT COUNT(*) FROM backlink_prospects WHERE status IN ('prospect','draft_prepared','submitted','published_unverified')").fetchone()[0]
    return {"researched":total,"free":free,"ready":ready,"submitted":submitted,"verified":verified,"removed_or_broken":removed,"pending_manual":pending}


def export_json(db) -> str:
    prospects=[]
    for p in list_prospects(db):
        d=dict(p)
        d["verifications"]=[dict(v) for v in db.execute("SELECT * FROM backlink_verifications WHERE prospect_id=? ORDER BY checked_at",(p["id"],))]
        prospects.append(d)
    return json.dumps({"generated_at":now(),"stats":stats(db),"prospects":prospects},ensure_ascii=False,indent=2)


def export_csv(db) -> str:
    stream=io.StringIO(newline="")
    columns=["id","name","website","submission_url","why_relevant","free_submission","registration_required","link_type","instructions","target_url","category","description","tags","status","checked_at","submitted_at","referring_page","last_http_status","last_result","anchor_text","link_rel","first_seen","last_verified","removal_reason"]
    writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader()
    for row in list_prospects(db): writer.writerow({k:row[k] for k in columns})
    return stream.getvalue()


def report_markdown(db) -> str:
    s=stats(db)
    lines=["# YashiPDF free backlink prospect report",f"\nGenerated: {now()} (UTC)","\nNo external submissions or outreach were sent.",f"\nProspects: {s['researched']} · free: {s['free']} · ready: {s['ready']} · submitted: {s['submitted']} · verified: {s['verified']} · removed/broken: {s['removed_or_broken']} · manual tasks: {s['pending_manual']}","\n| Prospect | Free | Status | Submission / evidence | Target | Next action |","|---|---:|---|---|---|---|"]
    for p in list_prospects(db):
        action={"prospect":"Review draft and submit manually if appropriate.","draft_prepared":"Submit manually; account/CAPTCHA/terms remain human actions.","submitted":"Wait for publisher review.","published_unverified":"Verify the public referring page.","verified":"Recheck on a conservative schedule.","rejected":"Review reason before any resubmission.","removed":"Investigate removal; never recreate automatically."}.get(p["status"],"Review")
        lines.append(f"| {p['name']} | {'Yes' if p['free_submission'] else 'Unknown'} | {p['status']} | [{p['submission_url']}]({p['submission_url']}) · checked {p['checked_at']} | {p['target_url']} | {action} |")
        lines.append(f"\n**Fit:** {p['why_relevant']}  \n**Draft:** {p['description']}  \n**Link type:** {p['link_type']}  \n**Instructions:** {p['instructions']}\n")
    lines.append("\nA backlink is counted as verified only after an HTTP 200 public referring page is fetched and an actual link to the recorded destination is found. Ranking impact is not inferred.")
    return "\n".join(lines)


def write_daily_summary(db, data_dir: Path, run_id=None, audit_result=None) -> Path:
    """Write one idempotent local pending-action summary per UTC calendar day."""
    directory=Path(data_dir)/"reports"; directory.mkdir(parents=True,exist_ok=True)
    day=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    destination=directory/f"backlink-daily-{day}.md"
    s=stats(db)
    lines=[f"# Backlink follow-up — {day} UTC", "", "No email or external submission was sent.", "",
           f"Audit run: {run_id if run_id is not None else 'not associated'} · audit result: {(audit_result or {}).get('execution','not supplied')}",
           f"Prospects: {s['researched']} · verified free: {s['free']} · drafts ready: {s['ready']} · submitted: {s['submitted']} · verified backlinks: {s['verified']} · removed/broken: {s['removed_or_broken']} · human actions pending: {s['pending_manual']}", "", "## Pending actions", ""]
    pending=[p for p in list_prospects(db) if p["status"] in ("prospect","draft_prepared","submitted","published_unverified")]
    if not pending: lines.append("No pending prospect actions.")
    for p in pending:
        action={"prospect":"Review the source and prepare the draft.","draft_prepared":"Submit manually if the current terms remain acceptable.","submitted":"Check the publisher's review status.","published_unverified":"Verify the public page and link attributes."}[p["status"]]
        lines.append(f"- **{p['name']}** — `{p['status']}`; {action} Submission: {p['submission_url']}.")
    temporary=destination.with_suffix(".md.tmp")
    temporary.write_text("\n".join(lines)+"\n",encoding="utf-8")
    temporary.replace(destination)
    return destination


def dashboard_page(db, notice: str = "") -> str:
    s=stats(db); rows=list_prospects(db)
    esc=lambda v: html.escape(str(v or ""),quote=True)
    cards="".join(f"<article class='card'><h3>{esc(p['name'])}</h3><p>{esc(p['why_relevant'])}</p><p><b>{'Verified free' if p['free_submission'] else 'Cost not confirmed free'}</b> · {'account required' if p['registration_required'] else 'no account required'} · status: <b>{esc(p['status'])}</b></p><p><a href='{esc(p['submission_url'])}' target='_blank' rel='noopener noreferrer'>Official submission instructions</a> · <a href='{esc(p['target_url'])}' target='_blank' rel='noopener noreferrer'>Suggested YashiPDF destination</a></p><details><summary>Review submission draft and instructions</summary><p>{esc(p['description'])}</p><pre>{esc(p['submission_draft'])}</pre><p>{esc(p['instructions'])}</p><p>Link type: {esc(p['link_type'])} · Source checked: {esc(p['checked_at'])}</p></details><form method='post' action='/backlinks/status'><input type='hidden' name='id' value='{p['id']}'><label>Update status <select name='status'>{''.join(f'<option value="{st}" {"selected" if st==p["status"] else ""}>{st}</option>' for st in MANUAL_STATUSES)}</select></label><label>Public referring page <input name='referring_page' type='url' placeholder='https://directory.example/listing'></label><label>Removal reason <input name='removal_reason'></label><button class='secondary'>Save status</button></form>{f'<p>Last result: {esc(p["last_result"])} · HTTP {esc(p["last_http_status"])} · {esc(p["link_rel"])} · {esc(p["anchor_text"])}<br>{esc(p["removal_reason"] or "")}</p>' if p['last_result'] else ''}{'<form method="post" action="/backlinks/verify"><input type="hidden" name="id" value="'+str(p['id'])+'"><button>Verify public backlink</button></form>' if p['referring_page'] else ''}</article>" for p in rows)
    add_form="""<section class='card'><h2>Add a researched prospect</h2><form method='post' action='/backlinks/add'><label>Name <input name='name' required maxlength='160'></label><label>Directory website <input name='website' type='url' placeholder='https://directory.example/' required></label><label>Exact submission or instructions URL <input name='submission_url' type='url' required></label><label>Public evidence/source URL <input name='source_url' type='url' required></label><label>Suggested YashiPDF destination <input name='target_url' type='url' value='https://www.yashipdf.online/' required></label><label>Why relevant <textarea name='why_relevant' required></textarea></label><label>Free submission? <select name='free_submission'><option value='true'>Verified free</option><option value='false'>Unknown / not free</option></select></label><label>Registration required? <select name='registration_required'><option value='false'>No</option><option value='true'>Yes</option></select></label><label>Link type <input name='link_type' value='Unknown until public listing is checked'></label><label>Instructions <textarea name='instructions' required></textarea></label><label>Category <input name='category'></label><label>Accurate, tailored description <textarea name='description' required></textarea></label><label>Tags <input name='tags'></label><label>Draft submission text <textarea name='submission_draft'></textarea></label><button>Add prospect</button></form></section>"""
    return f"""<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>Backlink prospect tracker · YashiPDF monitor</title><style>body{{margin:0;background:#0b1220;color:#e8eff7;font:15px/1.5 system-ui,Segoe UI,sans-serif}}main{{max-width:1200px;margin:auto;padding:24px}}a{{color:#7cc4ff;overflow-wrap:anywhere}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:12px}}.card{{background:#121d2e;border:1px solid #26374b;border-radius:10px;padding:16px;margin:12px 0}}.cards .card{{margin:0}}.n{{font-size:24px;font-weight:700}}input,select,textarea{{display:block;width:min(600px,100%);padding:8px;margin:5px 0 10px;background:#0b1220;color:#e8eff7;border:1px solid #40536a;border-radius:5px}}textarea{{min-height:70px}}button{{background:#1987d4;color:white;border:0;padding:9px 13px;border-radius:6px;cursor:pointer}}.secondary{{background:#304459}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.notice{{color:#83e1a8}}</style><main><p><a href='/'>← Site monitor dashboard</a></p><h1>Free backlink prospect tracker</h1><p>Local-first research and verification. Nothing is submitted or emailed automatically. Link attributes and ranking value are not assumed.</p><p class='notice'>{esc(notice)}</p><div class='cards'>{''.join(f"<div class='card'><small>{label}</small><div class='n'>{s[key]}</div></div>" for label,key in [('Researched','researched'),('Free opportunities','free'),('Ready to submit','ready'),('Submitted','submitted'),('Verified backlinks','verified'),('Removed/broken','removed_or_broken'),('Pending manual tasks','pending_manual')])}</div><p><a href='/backlinks.csv'>Export CSV</a> · <a href='/backlinks.json'>Export JSON</a> · <a href='/backlinks.md'>Markdown report</a></p><p>Status meanings: prospect → draft_prepared → submitted → published_unverified → verified; rejected and removed remain separate. A record is not a backlink until public link verification succeeds.</p>{add_form}{cards or '<p>No prospects stored.</p>'}</main></html>"""
