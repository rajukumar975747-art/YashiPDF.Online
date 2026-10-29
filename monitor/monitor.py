"""Local-first YashiPDF technical SEO monitor. Python standard library only."""
from __future__ import annotations

import html
import json
import fnmatch
import difflib
import hashlib
import ipaddress
import os
import re
import sqlite3
import socket
import threading
import time
import ssl
import shutil
import subprocess
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("YASHIPDF_DATA_DIR", Path(__file__).parent / "data"))
DB = DATA / "monitor.sqlite3"
SITE = os.environ.get("YASHIPDF_SITE_URL", "https://www.yashipdf.online/").rstrip("/")
MAX_URLS = int(os.environ.get("YASHIPDF_MAX_URLS", "250"))
INTERVAL = max(60, int(os.environ.get("YASHIPDF_AUDIT_INTERVAL_MINUTES", "1440"))) * 60
RETRY_INTERVAL = max(60, int(os.environ.get("YASHIPDF_RETRY_INTERVAL_MINUTES", "15"))) * 60
TIMEOUT = max(2, int(os.environ.get("YASHIPDF_REQUEST_TIMEOUT", "12")))
RETRIES = min(4, max(1, int(os.environ.get("YASHIPDF_REQUEST_RETRIES", "2"))))
CRAWL_DELAY = max(0.05, float(os.environ.get("YASHIPDF_CRAWL_DELAY_SECONDS", "0.2")))
BING_RATE_LIMIT_SECONDS = max(0.5, float(os.environ.get("YASHIPDF_API_DELAY_SECONDS", "1")))
USER_AGENT = "YashiPDFLocalMonitor/1.0 (+local technical audit; respects robots.txt)"
HOST_LOG = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "YashiPDFMonitor" / "logs" / "monitor-host.log"
_HOST_LOG_LOCK = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def host_log(message):
    """Persist startup/runtime output when launched without a console (pythonw)."""
    try:
        HOST_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _HOST_LOG_LOCK, HOST_LOG.open("a", encoding="utf-8") as stream:
            stream.write(f"{now()} {message}\n")
    except OSError:
        pass


def console_log(message):
    if sys.stdout is None:
        host_log(message)
    else:
        print(message)


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / "monitor.sqlite3", timeout=20)
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started TEXT, finished TEXT, status TEXT, urls_found INTEGER DEFAULT 0, urls_checked INTEGER DEFAULT 0, summary TEXT);
    CREATE TABLE IF NOT EXISTS urls(run_id INTEGER, url TEXT, status INTEGER, final_url TEXT, title TEXT, description TEXT, canonical TEXT, h1_count INTEGER, checked TEXT, error TEXT, PRIMARY KEY(run_id,url));
    CREATE TABLE IF NOT EXISTS issues(id INTEGER PRIMARY KEY, run_id INTEGER, severity TEXT, category TEXT, url TEXT, evidence TEXT, recommendation TEXT, status TEXT DEFAULT 'open', created TEXT);
    CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY, captured TEXT, source TEXT, payload TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS content_reviews(id INTEGER PRIMARY KEY, run_id INTEGER, url TEXT, title TEXT, word_count INTEGER, evidence TEXT, status TEXT DEFAULT 'awaiting_review', created TEXT, UNIQUE(run_id,url));
    CREATE TABLE IF NOT EXISTS issue_registry(fingerprint TEXT PRIMARY KEY, severity TEXT NOT NULL, category TEXT NOT NULL, url TEXT NOT NULL, evidence TEXT NOT NULL, recommendation TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, last_run INTEGER NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1, resolved_at TEXT);
    CREATE TABLE IF NOT EXISTS issue_events(id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, run_id INTEGER NOT NULL, action TEXT NOT NULL, occurred TEXT NOT NULL, UNIQUE(fingerprint,run_id,action));
    CREATE TABLE IF NOT EXISTS internal_links(run_id INTEGER NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL, status INTEGER, kind TEXT NOT NULL DEFAULT 'link', PRIMARY KEY(run_id,source,target));
    CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY, source TEXT NOT NULL, dimension TEXT NOT NULL, key TEXT NOT NULL, period_start TEXT, period_end TEXT, captured TEXT NOT NULL, clicks REAL, impressions REAL, ctr REAL, position REAL, extra_json TEXT);
    CREATE TABLE IF NOT EXISTS fixes(id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, url TEXT NOT NULL, created TEXT NOT NULL, status TEXT NOT NULL, backup_path TEXT NOT NULL, diff TEXT NOT NULL, verification_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS scheduler_state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS monitor_logs(id INTEGER PRIMARY KEY, occurred TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL, run_id INTEGER);
    CREATE TABLE IF NOT EXISTS file_fixes(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, path TEXT NOT NULL, category TEXT NOT NULL, created TEXT NOT NULL, status TEXT NOT NULL, backup_path TEXT NOT NULL, diff TEXT NOT NULL, verification TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, occurrences INTEGER NOT NULL DEFAULT 1, last_run INTEGER, status TEXT NOT NULL DEFAULT 'open');
    """)
    for col, spec in (("redirect_chain","TEXT NOT NULL DEFAULT '[]'"),("x_robots_tag","TEXT"),("content_hash","TEXT"),("response_ms","REAL")):
        existing={r[1] for r in db.execute("PRAGMA table_info(urls)")}
        if col not in existing: db.execute(f"ALTER TABLE urls ADD COLUMN {col} {spec}")
    link_columns={r[1] for r in db.execute("PRAGMA table_info(internal_links)")}
    if "kind" not in link_columns: db.execute("ALTER TABLE internal_links ADD COLUMN kind TEXT NOT NULL DEFAULT 'link'")
    db.commit()
    return db


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = self.description = self.canonical = ""
        self.titles = []
        self.descriptions = []
        self.canonicals = []
        self.robots_directives = []
        self.h1_count = 0
        self.word_count = 0
        self.links = []
        self.resources = []
        self.in_title = False
        self.in_h1 = False
        self.images_missing_alt = 0
        self.meta_robots = ""
        self.schema_errors = []
        self.schema_types = []
        self._jsonld = False
        self._jsonld_text = ""
        self._hidden_text = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title": self.in_title = True; self._title_text = ""
        if tag == "h1": self.in_h1 = True; self.h1_count += 1
        if tag == "meta" and a.get("name", "").lower() == "description": self.description = a.get("content", ""); self.descriptions.append(self.description)
        if tag == "meta" and a.get("name", "").lower() == "robots": self.meta_robots = a.get("content", ""); self.robots_directives.append(self.meta_robots)
        if tag == "link" and "canonical" in a.get("rel", "").lower().split(): self.canonical = a.get("href", ""); self.canonicals.append(self.canonical)
        if tag == "a" and a.get("href"): self.links.append(a["href"])
        if tag in ("script", "img", "iframe", "source", "video", "audio") and a.get("src"): self.resources.append(a["src"])
        if tag == "link" and a.get("href") and ("stylesheet" in a.get("rel", "").lower().split() or a.get("rel", "").lower() == "icon"): self.resources.append(a["href"])
        if tag == "img" and not a.get("alt"): self.images_missing_alt += 1
        if tag == "script" and a.get("type", "").lower() == "application/ld+json": self._jsonld = True; self._jsonld_text = ""
        if tag in ("script", "style"): self._hidden_text = True

    def handle_data(self, data):
        if self.in_title: self._title_text += data
        if self._jsonld: self._jsonld_text += data
        if not self._hidden_text: self.word_count += len(re.findall(r"\b[\w'-]+\b", data))

    def handle_endtag(self, tag):
        if tag == "title" and self.in_title:
            self.title=self._title_text.strip(); self.titles.append(self.title); self.in_title=False
        if tag == "h1": self.in_h1 = False
        if tag == "script" and self._jsonld:
            try:
                data=json.loads(self._jsonld_text); items=data if isinstance(data,list) else [data]
                for item in items:
                    if isinstance(item,dict):
                        types=item.get("@type",[]); types=[types] if isinstance(types,str) else types
                        self.schema_types.extend(t for t in types if isinstance(t,str))
                        if not types: self.schema_errors.append("JSON-LD object has no @type")
                    else: self.schema_errors.append("JSON-LD item is not an object")
            except (ValueError,TypeError): self.schema_errors.append("Malformed JSON-LD")
            self._jsonld=False
        if tag in ("script","style"): self._hidden_text=False


class RedirectRecorder(urllib.request.HTTPRedirectHandler):
    def __init__(self,origin): super().__init__(); self.chain=[]; self.origin=origin
    def _record(self, req, code, headers):
        self.chain.append({"status":code,"from":req.full_url,"to":urllib.parse.urljoin(req.full_url,headers.get("Location", ""))})
    def http_error_301(self,req,fp,code,msg,headers): self._record(req,code,headers); return super().http_error_301(req,fp,code,msg,headers)
    def http_error_302(self,req,fp,code,msg,headers): self._record(req,code,headers); return super().http_error_302(req,fp,code,msg,headers)
    def http_error_303(self,req,fp,code,msg,headers): self._record(req,code,headers); return super().http_error_303(req,fp,code,msg,headers)
    def http_error_307(self,req,fp,code,msg,headers): self._record(req,code,headers); return super().http_error_307(req,fp,code,msg,headers)
    def http_error_308(self,req,fp,code,msg,headers): self._record(req,code,headers); return super().http_error_308(req,fp,code,msg,headers)
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        parsed=urllib.parse.urlsplit(newurl)
        if parsed.scheme!="https" or parsed.netloc.lower()!=self.origin: return None
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def classify_connection_error(error):
    reason=error.reason if isinstance(error,urllib.error.URLError) else error
    message=str(reason).lower()
    if isinstance(reason,socket.gaierror) or "getaddrinfo" in message or "name or service not known" in message or "no such host" in message:
        return "DNS resolution failed: the configured system resolver did not resolve the host."
    if "proxy" in message or "tunnel connection" in message: return "Proxy connection failed: check the configured proxy and its reachability."
    if isinstance(reason,ssl.SSLError) or "certificate verify failed" in message or "ssl" in message:
        return "TLS handshake/certificate failed: host was resolved, but HTTPS verification failed."
    if isinstance(reason,socket.timeout) or "timed out" in message or "timeout" in message:
        return "Connection timed out: DNS or TCP/HTTPS did not respond before the configured timeout."
    if isinstance(reason,ConnectionRefusedError) or "connection refused" in message:
        return "TCP connection refused: host resolved, but port/service rejected the connection."
    return f"Network request failed ({type(reason).__name__}): {str(reason)[:240]}"

def fetch_detailed(url):
    last_error=""
    for attempt in range(RETRIES+1):
        recorder=RedirectRecorder(urllib.parse.urlsplit(url).netloc.lower()); req=urllib.request.Request(url,headers={"User-Agent":USER_AGENT,"Accept":"text/html,application/xml,text/plain,*/*"})
        opener=urllib.request.build_opener(recorder)
        try:
            with opener.open(req,timeout=TIMEOUT) as response:
                return response.status,response.geturl(),response.read(4_000_000),response.headers.get_content_type(),"",recorder.chain,dict(response.headers.items())
        except urllib.error.HTTPError as e:
            return e.code,e.geturl(),e.read(1_000_000),e.headers.get_content_type(),f"HTTP {e.code}",recorder.chain,dict(e.headers.items())
        except Exception as e:
            last_error=classify_connection_error(e)
            if attempt < RETRIES:
                time.sleep(min(2.0,0.35*(2**attempt)))
    return 0,url,b"","",last_error,[],{}

def fetch(url):
    status,final,body,ctype,error,_,_=fetch_detailed(url)
    return status,final,body,ctype,error


def issue(db, run, severity, category, url, evidence, recommendation):
    fingerprint=hashlib.sha256("\0".join((category,url,evidence[:1000])).encode()).hexdigest()
    stamp=now()
    db.execute("INSERT INTO issue_registry(fingerprint,severity,category,url,evidence,recommendation,status,first_seen,last_seen,last_run,occurrences) VALUES(?,?,?,?,?,?,?,?,?,?,1) ON CONFLICT(fingerprint) DO UPDATE SET severity=excluded.severity,evidence=excluded.evidence,recommendation=excluded.recommendation,status=CASE WHEN issue_registry.status='resolved' THEN 'reopened' ELSE issue_registry.status END,last_seen=excluded.last_seen,last_run=excluded.last_run,occurrences=issue_registry.occurrences+1,resolved_at=NULL",
               (fingerprint,severity,category,url,evidence[:1000],recommendation,"open",stamp,stamp,run))
    db.execute("INSERT OR IGNORE INTO issue_events(fingerprint,run_id,action,occurred) VALUES(?,?,?,?)",(fingerprint,run,"observed",stamp))
    # Keep legacy per-run records available to older report consumers during migration.
    db.execute("INSERT INTO issues(run_id,severity,category,url,evidence,recommendation,status,created) VALUES(?,?,?,?,?,?,?,?)",
               (run,severity,category,url,evidence[:1000],recommendation,"open",stamp))

def log_event(message,level="INFO",run_id=None):
    console_log(f"{now()} {level} {message}")
    db=connect(); db.execute("INSERT INTO monitor_logs(occurred,level,message,run_id) VALUES(?,?,?,?)",(now(),level,str(message)[:2000],run_id)); db.commit(); db.close()

def robots_allowed(content, path, agent="YashiPDFLocalMonitor"):
    groups=[]; agents=[]; rules=[]
    def finish():
        if agents: groups.append((list(agents),list(rules)))
        agents.clear(); rules.clear()
    for raw in content.splitlines():
        line=raw.split("#",1)[0].strip()
        if not line or ":" not in line: continue
        key,value=(part.strip() for part in line.split(":",1)); key=key.lower()
        if key=="user-agent":
            if rules: finish()
            agents.append(value.lower())
        elif key in ("allow","disallow") and agents and value:
            rules.append((key,value))
        elif key=="sitemap" and rules:
            finish()
    finish()
    matching=[(len(a),r) for names,r in groups for a in names if a=="*" or a in agent.lower()]
    if not matching: return True
    specificity=max(score for score,_ in matching)
    def matches(pattern):
        if pattern.endswith("$"): return fnmatch.fnmatchcase(path,pattern[:-1])
        if "*" in pattern: return fnmatch.fnmatchcase(path,pattern)
        return path.startswith(pattern)
    applicable=[rule for score,group in matching if score==specificity for rule in group if matches(rule[1])]
    if not applicable: return True
    # Most-specific rule wins; Allow wins equal-length ties per the common robots convention.
    best=max(len(pattern) for _,pattern in applicable)
    return any(kind=="allow" for kind,pattern in applicable if len(pattern)==best)


def network_diagnostics(site_url):
    host=urllib.parse.urlsplit(site_url).hostname
    result={"host":host,"system_dns":"unavailable","addresses":[],"proxy_configured":False,"proxy_hosts":[]}
    try:
        addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
        result["addresses"]=sorted({entry[4][0] for entry in addresses})
        result["system_dns"]="resolved"
    except Exception as e:
        result["system_dns"]="failed"; result["dns_error"]=classify_connection_error(e)
    proxies=urllib.request.getproxies()
    result["proxy_configured"]=bool(proxies)
    result["proxy_hosts"]=sorted({urllib.parse.urlsplit(v if "://" in v else "http://"+v).hostname or "configured" for v in proxies.values()})
    return result

def sitemap_fetch_set(origin, robots_text, db, run):
    listed=[origin+"/sitemap.xml"]
    for line in robots_text.splitlines():
        if line.strip().lower().startswith("sitemap:"):
            candidate=line.split(":",1)[1].strip()
            if candidate and candidate not in listed: listed.append(candidate)
    all_urls=[]; fetched=[]; failures=[]; queue=listed[:20]
    last_fetch=0.0
    while queue and len(fetched)<20:
        sitemap=queue.pop(0)
        p=urllib.parse.urlsplit(sitemap)
        if p.scheme not in ("http","https") or p.netloc.lower()!=urllib.parse.urlsplit(origin).netloc.lower():
            issue(db,run,"low","sitemap",sitemap,"External or invalid sitemap location was not fetched","Verify the sitemap location and host ownership."); continue
        wait=CRAWL_DELAY-(time.monotonic()-last_fetch)
        if last_fetch and wait>0: time.sleep(wait)
        status,final,body,ctype,error,redirects,headers=fetch_detailed(sitemap); last_fetch=time.monotonic()
        fetched.append(sitemap)
        if status!=200:
            failures.append({"url":sitemap,"status":status,"error":error})
            category="connectivity" if status==0 else "sitemap"
            sev="medium" if status==0 else ("high" if status>=500 else "medium")
            recommendation="Check local resolver, proxy, firewall and HTTPS path; this failure alone does not prove site downtime." if status==0 else "Restore the sitemap endpoint or correct its status and XML response."
            issue(db,run,sev,category,sitemap,error or f"HTTP {status}",recommendation)
            continue
        try:
            root=ET.fromstring(body); kind=root.tag.rsplit("}",1)[-1].lower()
            locs=[e.text.strip() for e in root.iter() if e.tag.endswith("loc") and e.text]
            if kind=="sitemapindex": queue.extend(x for x in locs if x not in fetched and x not in queue)
            elif kind=="urlset":
                if not locs: issue(db,run,"medium","sitemap",sitemap,"Valid URL set contains no loc entries","Add intended canonical public URLs or remove the empty sitemap from robots.txt.")
                all_urls.extend(locs)
            else: issue(db,run,"high","sitemap",sitemap,f"Unexpected XML root element: {kind}","Serve a sitemap index or URL set as valid XML.")
        except ET.ParseError as e:
            failures.append({"url":sitemap,"status":status,"error":f"Invalid XML: {e}"})
            issue(db,run,"high","sitemap",sitemap,f"Invalid XML: {e}","Repair the sitemap XML and verify its namespace and elements.")
        if redirects: issue(db,run,"low","redirect",sitemap,"Sitemap endpoint redirects: "+" → ".join(x["to"] for x in redirects),"Point robots.txt and internal configuration at the final sitemap URL.")
    return all_urls,fetched,failures

def audit(base_url=SITE, max_urls=MAX_URLS):
    """Sequential, polite same-origin crawl seeded from robots and sitemap routes."""
    db=connect(); started=now()
    run=db.execute("INSERT INTO runs(started,status) VALUES(?,?)",(started,"running")).lastrowid; db.commit()
    db.execute("INSERT INTO monitor_logs(occurred,level,message,run_id) VALUES(?,?,?,?)",(started,"INFO",f"Audit started for {base_url}; URL cap {max_urls}.",run)); db.commit()
    base=urllib.parse.urlsplit(base_url); origin=f"{base.scheme}://{base.netloc}"
    diagnostics=network_diagnostics(base_url)
    robots_url=origin+"/robots.txt"
    rs,rf,rb,_,robots_error,rr,rheaders=fetch_detailed(robots_url)
    robots_text=rb.decode("utf-8","replace") if rs==200 else ""
    if rs==0:
        issue(db,run,"medium","connectivity",robots_url,robots_error,"Check local DNS, proxy/firewall configuration, and retry. A resolver failure is not evidence that the public site is down.")
    elif rs>=500: issue(db,run,"high","robots",robots_url,f"HTTP {rs}","Restore robots.txt availability before crawling.")
    elif rs>=400: issue(db,run,"medium","robots",robots_url,f"HTTP {rs}","Serve a readable robots.txt and verify crawler access.")
    elif not re.search(r"(?im)^\s*sitemap\s*:",robots_text): issue(db,run,"medium","robots",robots_url,"No Sitemap directive found","Add the canonical public sitemap URL to robots.txt.")
    sitemap_urls,sitemap_files,sitemap_failures=sitemap_fetch_set(origin,robots_text,db,run)
    if diagnostics["system_dns"]=="failed":
        issue(db,run,"medium","connectivity",base_url,diagnostics.get("dns_error","System DNS resolution failed"),"Inspect the local DNS resolver; use independent public records to distinguish this from a site outage.")

    queue=[]; seen=set(); sitemap_normalized=set()
    def normalized(value,keep_query=False):
        p=urllib.parse.urlsplit(value)
        return urllib.parse.urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path or "/",p.query if keep_query else "",""))
    def enqueue(value):
        try:
            absolute=urllib.parse.urljoin(base_url,value); p=urllib.parse.urlsplit(absolute)
            if p.scheme not in ("http","https") or p.netloc.lower()!=base.netloc.lower(): return
            request_path=p.path+(("?"+p.query) if p.query else "")
            if p.query and rs==200 and not robots_allowed(robots_text,request_path):
                blocked=normalized(absolute,True)
                if blocked not in seen:
                    seen.add(blocked); db.execute("INSERT OR IGNORE INTO urls(run_id,url,checked) VALUES(?,?,?)",(run,blocked,""))
                    issue(db,run,"low","robots_exclusion",blocked,"Query URL skipped because robots.txt disallows this query variant","Confirm query-parameter crawl exclusions are intentional.")
                return
            clean=normalized(absolute)
            if clean not in seen:
                seen.add(clean); queue.append(clean); db.execute("INSERT OR IGNORE INTO urls(run_id,url,checked) VALUES(?,?,?)",(run,clean,""))
        except (ValueError,UnicodeError): return
    for item in sitemap_urls:
        p=urllib.parse.urlsplit(urllib.parse.urljoin(base_url,item))
        if p.scheme in ("http","https") and p.netloc.lower()==base.netloc.lower():
            clean=normalized(urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,"","")))
            sitemap_normalized.add(clean); enqueue(clean)
        else: issue(db,run,"low","sitemap",item,"Sitemap contains a URL outside the configured site origin","Review cross-host sitemap entries and include only intended canonical site URLs.")
    enqueue(base_url)
    checked={}; response_times=[]; previous_time=0.0
    # If robots is unavailable (DNS/network or 5xx), fail closed and retain discovered inventory.
    can_crawl=not (rs==0 or rs>=500)
    i=0
    while can_crawl and i<len(queue) and len(checked)<max_urls:
        url=queue[i]; i+=1; parts=urllib.parse.urlsplit(url)
        if rs==200 and not robots_allowed(robots_text,parts.path+(("?"+parts.query) if parts.query else "")):
            issue(db,run,"low","robots_exclusion",url,"URL skipped because robots.txt disallows it","Verify the crawl exclusion is intentional."); continue
        delay=CRAWL_DELAY-(time.monotonic()-previous_time)
        if previous_time and delay>0: time.sleep(delay)
        request_started=time.monotonic(); status,final,body,ctype,error,redirects,headers=fetch_detailed(url); elapsed_ms=round((time.monotonic()-request_started)*1000,1); response_times.append(elapsed_ms); previous_time=time.monotonic()
        checked[url]=status; is_html="html" in ctype or body.lstrip().lower().startswith((b"<!doctype html",b"<html")); parser=PageParser()
        if status and is_html:
            try: parser.feed(body.decode("utf-8","replace"))
            except Exception: pass
        xrobots=headers.get("X-Robots-Tag",""); digest=hashlib.sha256(body).hexdigest() if body else ""
        db.execute("INSERT OR REPLACE INTO urls(run_id,url,status,final_url,title,description,canonical,h1_count,checked,error,redirect_chain,x_robots_tag,content_hash,response_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(run,url,status,final,parser.title.strip(),parser.description.strip(),parser.canonical,parser.h1_count,now(),error,json.dumps(redirects),xrobots,digest,elapsed_ms))
        if status==0:
            issue(db,run,"medium","connectivity",url,error,"Check local DNS/proxy/firewall/HTTPS path and retry; do not infer production downtime from local connection failure.")
        elif status>=400:
            issue(db,run,"high" if status>=500 else "medium","http",url,f"HTTP {status}","Repair the relevant route or update links to a valid destination.")
        if redirects:
            chain=" → ".join(r["from"]+f" [{r['status']}]" for r in redirects)+" → "+final
            issue(db,run,"low","redirect",url,chain,"Update internal links and sitemap entries to use the final canonical URL where the route is stable.")
        if xrobots and re.search(r"(?i)(?:^|,)\s*noindex\b",xrobots): issue(db,run,"high","indexing",url,f"X-Robots-Tag: {xrobots}","Confirm the response-level noindex directive is intentional before changing it.")
        if is_html and status<400:
            if len(parser.titles)!=1 or not parser.title: issue(db,run,"high","metadata",url,f"Found {len(parser.titles)} title tags","Add exactly one non-empty, page-specific title.")
            if len(parser.title)>70: issue(db,run,"low","metadata",url,f"Title is {len(parser.title)} characters","Review concise, accurate wording.")
            if len(parser.descriptions)!=1 or not parser.description: issue(db,run,"medium","metadata",url,f"Found {len(parser.descriptions)} meta descriptions","Provide exactly one accurate page summary.")
            if parser.description and len(parser.description)>200: issue(db,run,"low","metadata",url,f"Meta description is {len(parser.description)} characters","Review for concise wording without changing meaning.")
            if len(parser.canonicals)!=1 or not parser.canonical: issue(db,run,"high","canonical",url,f"Found {len(parser.canonicals)} canonical links","Keep exactly one absolute canonical URL on the preferred route.")
            else:
                canon=urllib.parse.urljoin(url,parser.canonical)
                if normalized(canon)!=normalized(final): issue(db,run,"high","canonical",url,f"Canonical resolves to {canon}; final response URL is {final}","Confirm the canonical points to this page's actual preferred URL.")
            if parser.h1_count==0: issue(db,run,"medium","headings",url,"No H1 in HTML response","Add one descriptive primary heading to the page.")
            if parser.h1_count>1: issue(db,run,"low","headings",url,f"{parser.h1_count} H1 elements","Review the document's primary heading hierarchy.")
            directives=",".join(parser.robots_directives)+","+xrobots
            if re.search(r"(?i)\bnoindex\b",directives): issue(db,run,"high","indexing",url,f"Noindex directive found: {directives}","Review the indexing directive before any change; never remove it without owner intent.")
            if parser.images_missing_alt: issue(db,run,"low","accessibility",url,f"{parser.images_missing_alt} images lack alt attributes","Add accurate alt text to meaningful images; leave decorative images empty.")
            for warning in parser.schema_errors: issue(db,run,"medium","structured_data",url,warning,"Correct JSON-LD syntax/type or remove unsupported markup; don't invent claims.")
            if url in sitemap_normalized and not parser.canonical: pass
            if url not in sitemap_normalized: issue(db,run,"low","sitemap_coverage",url,"Discovered HTML route is absent from current sitemap","Add only after verifying canonicality and indexability; auto-fix can add a verified sitemap URL.")
            if urllib.parse.urlsplit(url).path.startswith("/blog/"):
                checks=[]
                if parser.word_count<300: checks.append(f"Short source text ({parser.word_count} words); review completeness and whether it solves the stated task.")
                if not parser.links: checks.append("No internal links found; consider links to the relevant tool and supporting guides.")
                if not checks: checks.append("Review accuracy, examples, intent fit, date claims, trustworthy citations, and links to relevant tools.")
                db.execute("INSERT OR IGNORE INTO content_reviews(run_id,url,title,word_count,evidence,status,created) VALUES(?,?,?,?,?,?,?)",(run,url,parser.title,parser.word_count," ".join(checks),"awaiting_review",now()))
            for link in parser.links+parser.resources:
                if "<" in link or ">" in link or "\n" in link or "\r" in link:
                    issue(db,run,"medium","malformed_internal_link",url,f"Malformed href/src contains markup or newline characters: {link[:240]!r}","Repair the source HTML attribute; the monitor skipped this malformed URL.")
                    continue
                target=urllib.parse.urljoin(url,link); lp=urllib.parse.urlsplit(target)
                if lp.scheme in ("http","https") and lp.netloc.lower()==base.netloc.lower():
                    clean=normalized(target); enqueue(target)
                    db.execute("INSERT OR IGNORE INTO internal_links(run_id,source,target,kind) VALUES(?,?,?,?)",(run,url,clean,"link" if link in parser.links else "resource"))
        db.commit()

    # Record links whose targets were actually checked as broken; cap-limited targets remain unverified.
    db.execute("UPDATE internal_links SET status=(SELECT status FROM urls WHERE urls.run_id=internal_links.run_id AND urls.url=internal_links.target) WHERE run_id=?",(run,))
    for row in db.execute("SELECT source,target,status FROM internal_links WHERE run_id=? AND status>=400",(run,)).fetchall():
        issue(db,run,"high" if row["status"]>=500 else "medium","broken_internal_link",row["target"],f"Linked from {row['source']}; target returned HTTP {row['status']}","Fix the source link or restore the specific target route.")
    # Detect duplicate titles/descriptions across checked HTML pages.
    for column,category,label in (("title","metadata","title"),("description","metadata","meta description")):
        for row in db.execute(f"SELECT {column} value,group_concat(url,' | ') urls,COUNT(*) n FROM urls WHERE run_id=? AND status<400 AND {column}!='' GROUP BY lower({column}) HAVING COUNT(*)>1",(run,)).fetchall():
            for duplicate_url in row["urls"].split(" | "):
                issue(db,run,"medium",category,duplicate_url,f"Duplicate {label} shared by {row['n']} URLs: {row['urls'][:700]}",f"Write a distinct, accurate {label} for this page.")
    db.execute("UPDATE internal_links SET status=(SELECT status FROM urls WHERE urls.run_id=internal_links.run_id AND urls.url=internal_links.target) WHERE run_id=?",(run,))
    http_errors=db.execute("SELECT COUNT(*) FROM urls WHERE run_id=? AND status>=400",(run,)).fetchone()[0]
    connection_failures=db.execute("SELECT COUNT(*) FROM urls WHERE run_id=? AND status=0",(run,)).fetchone()[0]
    successful=db.execute("SELECT COUNT(*) FROM urls WHERE run_id=? AND status BETWEEN 200 AND 399",(run,)).fetchone()[0]
    # Only resolve old observations when the same URL was fetched successfully this run.
    for old in db.execute("SELECT fingerprint,url,category FROM issue_registry WHERE status IN ('open','reopened') AND last_run<>?",(run,)).fetchall():
        checked_status=db.execute("SELECT status FROM urls WHERE run_id=? AND url=?",(run,old["url"])).fetchone()
        category_seen=db.execute("SELECT 1 FROM issue_registry WHERE url=? AND category=? AND last_run=? AND status IN ('open','reopened') LIMIT 1",(old["url"],old["category"],run)).fetchone()
        if checked_status and checked_status[0] and 200<=checked_status[0]<400 and not category_seen:
            db.execute("UPDATE issue_registry SET status='resolved',resolved_at=?,last_run=? WHERE fingerprint=?",(now(),run,old["fingerprint"]))
            db.execute("INSERT OR IGNORE INTO issue_events(fingerprint,run_id,action,occurred) VALUES(?,?,?,?)",(old["fingerprint"],run,"resolved",now()))
    # Surface topical internal-link suggestions from actual crawled titles and existing routes.
    pages=db.execute("SELECT url,title FROM urls WHERE run_id=? AND status BETWEEN 200 AND 399 AND title!=''",(run,)).fetchall()
    for review in db.execute("SELECT id,url,title,evidence FROM content_reviews WHERE run_id=?",(run,)).fetchall():
        words=set(re.findall(r"[a-z0-9]{4,}",((review["title"] or "")+" "+urllib.parse.urlsplit(review["url"]).path).lower()))
        options=[]
        for page in pages:
            if page["url"]==review["url"]: continue
            overlap=words & set(re.findall(r"[a-z0-9]{4,}",(page["title"]+" "+urllib.parse.urlsplit(page["url"]).path).lower()))
            if len(overlap)>=2: options.append((len(overlap),page["url"],page["title"],", ".join(sorted(overlap))))
        options.sort(reverse=True)
        suggestions=("Potential existing routes for contextual links (check relevance before editing): "+"; ".join(f"{title} — {target} (shared terms: {terms})" for _,target,title,terms in options[:3])) if options else "No strong topical route match found; do not force an irrelevant internal link."
        db.execute("UPDATE content_reviews SET evidence=? WHERE id=?",(review["evidence"]+" "+suggestions,review["id"]))
    ordered=sorted(response_times); p50=ordered[len(ordered)//2] if ordered else None; p95=ordered[min(len(ordered)-1,int(len(ordered)*.95))] if ordered else None
    summary={"execution":"completed" if successful or queue else "failed","health":"degraded" if http_errors or connection_failures or sitemap_failures or rs==0 or rs>=500 else "no_http_errors_found","urls_found":len(seen),"urls_checked":len(checked),"successful_http_urls":successful,"http_errors":http_errors,"connection_failures":connection_failures,"response_ms_p50":p50,"response_ms_p95":p95,"response_ms_sample_count":len(response_times),"sitemap_urls":len(sitemap_urls),"sitemap_files":sitemap_files,"robots_http":rs,"sitemap_failures":sitemap_failures,"network":diagnostics,"proxy_in_use":diagnostics["proxy_configured"],"message":"Connection failures describe this monitor's network path; only an HTTP response establishes an origin server response. Response timing includes body transfer and is not a Core Web Vital."}
    finish=now(); db.execute("UPDATE runs SET finished=?,status=?,urls_found=?,urls_checked=?,summary=? WHERE id=?",(finish,"complete" if summary["execution"]=="completed" else "failed",len(seen),len(checked),json.dumps(summary),run)); db.execute("INSERT INTO monitor_logs(occurred,level,message,run_id) VALUES(?,?,?,?)",(finish,"INFO" if summary["execution"]=="completed" else "ERROR",f"Audit {summary['execution']}: {successful} successful, {http_errors} HTTP errors, {connection_failures} connection failures, {len(seen)} discovered.",run)); db.commit(); db.close()
    return {"id":run,"started":started,"finished":finish,**summary}


class ApiError(Exception):
    def __init__(self,status,message): self.status=status; super().__init__(message)

_api_lock=threading.Lock(); _last_api_request=0.0
def request_json(url,method="GET",payload=None,headers=None,rate_limit=False):
    """JSON HTTPS API call with bounded retries; never includes URL/query/credential in errors."""
    global _last_api_request
    for attempt in range(3):
        if rate_limit:
            with _api_lock:
                wait=BING_RATE_LIMIT_SECONDS-(time.monotonic()-_last_api_request)
                if wait>0: time.sleep(wait)
                _last_api_request=time.monotonic()
        data=json.dumps(payload).encode() if payload is not None else None
        req=urllib.request.Request(url,data=data,headers={"Accept":"application/json","Content-Type":"application/json",**(headers or {})},method=method)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=TIMEOUT) as response:
                return json.loads(response.read(8_000_000).decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429,500,502,503,504) and attempt<2:
                time.sleep(min(4,0.5*(2**attempt))); continue
            raise ApiError(e.code,f"HTTP {e.code}") from None
        except (urllib.error.URLError,socket.timeout,TimeoutError) as e:
            if attempt<2: time.sleep(min(4,0.5*(2**attempt))); continue
            raise ApiError(0,classify_connection_error(e)) from None
        except (ValueError,UnicodeDecodeError) as e:
            raise ApiError(0,f"Invalid JSON API response ({type(e).__name__})") from None
    raise ApiError(0,"API request failed after retries")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

def google_access_token():
    form=urllib.parse.urlencode({"client_id":os.environ["GOOGLE_CLIENT_ID"],"client_secret":os.environ["GOOGLE_CLIENT_SECRET"],"refresh_token":os.environ["GOOGLE_REFRESH_TOKEN"],"grant_type":"refresh_token"}).encode()
    req=urllib.request.Request("https://oauth2.googleapis.com/token",data=form,headers={"Content-Type":"application/x-www-form-urlencoded"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=TIMEOUT) as response: return json.loads(response.read().decode())["access_token"]
    except urllib.error.HTTPError as e: raise ApiError(e.code,f"OAuth token endpoint HTTP {e.code}") from None
    except Exception as e: raise ApiError(0,f"OAuth token request failed ({type(e).__name__}); check network and OAuth configuration") from None

def gsc_api(url,token,method="GET",payload=None):
    return request_json(url,method,payload,{"Authorization":f"Bearer {token}"},rate_limit=True)

def save_metrics(db,source,dimension,start,end,rows):
    captured=now()
    for row in rows:
        keys=row.get("keys") or ([row[k] for k in ("Query","PageURL") if row.get(k)] or ([row["Date"]] if row.get("Date") else []))
        position=row.get("position",row.get("AvgImpressionPosition",row.get("AvgClickPosition")))
        db.execute("INSERT INTO metrics(source,dimension,key,period_start,period_end,captured,clicks,impressions,ctr,position,extra_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(source,dimension,json.dumps(keys,ensure_ascii=False),start,end,captured,row.get("clicks",row.get("Clicks")),row.get("impressions",row.get("Impressions")),row.get("ctr"),position,json.dumps(row,ensure_ascii=False)))

def gsc_snapshot(db,run_id=None):
    required=("GOOGLE_CLIENT_ID","GOOGLE_CLIENT_SECRET","GOOGLE_REFRESH_TOKEN")
    if not all(os.environ.get(k) for k in required): return {"status":"configured_not_connected","detail":"Connector is ready; set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN. No account data fetched."}
    site=os.environ.get("GOOGLE_SEARCH_CONSOLE_SITE_URL","")
    if not site: return {"status":"configured_not_connected","detail":"Set GOOGLE_SEARCH_CONSOLE_SITE_URL to the exact verified GSC property."}
    try:
        token=google_access_token(); encoded=urllib.parse.quote(site,safe="")
        base=f"https://www.googleapis.com/webmasters/v3/sites/{encoded}"
        sitemap_data=gsc_api(base+"/sitemaps",token)
        sitemaps=[]
        for item in sitemap_data.get("sitemap",[]):
            sitemaps.append({k:item.get(k) for k in ("path","lastSubmitted","lastDownloaded","isPending","isSitemapsIndex","type","errors","warnings") if k in item})
        end=datetime.now(timezone.utc).date()-timedelta(days=3); start=end-timedelta(days=27); prev_end=start-timedelta(days=1); prev_start=prev_end-timedelta(days=27)
        dims={"site":[],"page":["page"],"query":["query"],"date":["date"],"country":["country"],"device":["device"],"query_page":["query","page"]}
        periods={}
        for label,lo,hi in (("current",start,end),("previous",prev_start,prev_end)):
            periods[label]={}
            for name,dimensions in dims.items():
                query={"startDate":lo.isoformat(),"endDate":hi.isoformat(),"type":"web","rowLimit":25000}
                if dimensions: query["dimensions"]=dimensions
                data=gsc_api(base+"/searchAnalytics/query",token,"POST",query)
                rows=data.get("rows",[]); periods[label][name]=rows
                save_metrics(db,"Google Search Console",name,lo.isoformat(),hi.isoformat(),rows)
        inspection=site
        result={"status":"connected","detail":f"Google Search Analytics finalized web data. Current {start} to {end}; previous {prev_start} to {prev_end}. Row limits apply; average position is not an exact live rank.","property":site,"sitemaps":sitemaps,"periods":periods,"date_ranges":{"current":[start.isoformat(),end.isoformat()],"previous":[prev_start.isoformat(),prev_end.isoformat()]}}
        _record_search_deltas(db,run_id,"Google Search Console",periods)
        return result
    except ApiError as e:
        return {"status":"error","detail":f"Google API request failed: {e}. Check OAuth scope webmasters.readonly, property access, quota and network."}

def bing_access():
    key=os.environ.get("BING_WEBMASTER_API_KEY","")
    if key: return "key",key
    required=("BING_WEBMASTER_CLIENT_ID","BING_WEBMASTER_CLIENT_SECRET","BING_WEBMASTER_REFRESH_TOKEN")
    if not all(os.environ.get(k) for k in required): return None,None
    form=urllib.parse.urlencode({"client_id":os.environ[required[0]],"client_secret":os.environ[required[1]],"refresh_token":os.environ[required[2]],"grant_type":"refresh_token"}).encode()
    req=urllib.request.Request("https://www.bing.com/webmasters/oauth/token",data=form,headers={"Content-Type":"application/x-www-form-urlencoded"})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=TIMEOUT) as response: return "oauth",json.loads(response.read().decode())["access_token"]
    except urllib.error.HTTPError as e: raise ApiError(e.code,f"OAuth token endpoint HTTP {e.code}") from None
    except Exception as e: raise ApiError(0,f"OAuth token request failed ({type(e).__name__}); check network and OAuth configuration") from None

def bing_api(method,site_url,auth,extra=None):
    kind,secret=auth
    params=({"siteUrl":site_url,**(extra or {})} if method!="GetUserSites" else dict(extra or {}))
    headers={}
    if kind=="key": params["apikey"]=secret; host="https://ssl.bing.com"
    else: headers["Authorization"]=f"Bearer {secret}"; host="https://www.bing.com"
    url=host+"/webmaster/api.svc/json/"+method+"?"+urllib.parse.urlencode(params)
    data=request_json(url,headers=headers,rate_limit=True)
    return data.get("d",data)

def bing_snapshot(db,run_id=None):
    try: auth=bing_access()
    except ApiError as e: return {"status":"error","detail":f"Bing OAuth failed: {e}"}
    if not auth[0]: return {"status":"configured_not_connected","detail":"Connector is ready; set BING_WEBMASTER_API_KEY or the Bing OAuth client ID, client secret and refresh token. No account data fetched."}
    site=os.environ.get("BING_WEBMASTER_SITE_URL",SITE)
    try:
        sites=bing_api("GetUserSites",site,auth)
        site_rows=sites if isinstance(sites,list) else []
        match=next((x for x in site_rows if urllib.parse.urlsplit(x.get("Url","" )).netloc.lower()==urllib.parse.urlsplit(site).netloc.lower()),None)
        if not match: return {"status":"not_authorized","detail":"Bing credentials responded but this site was not listed in the account's verified sites."}
        if match.get("IsVerified") is False: return {"status":"not_verified","detail":"This site is listed in Bing Webmaster Tools but is not verified."}
        rank=bing_api("GetRankAndTrafficStats",site,auth)
        queries=bing_api("GetQueryStats",site,auth)
        pages=bing_api("GetPageStats",site,auth)
        crawl=bing_api("GetCrawlStats",site,auth)
        feeds=bing_api("GetFeeds",site,auth)
        datasets={"site":rank,"query":queries,"page":pages,"crawl":crawl,"sitemaps":feeds}
        for dimension,rows in datasets.items():
            rows=rows if isinstance(rows,list) else ([rows] if isinstance(rows,dict) else [])
            save_metrics(db,"Bing Webmaster Tools",dimension,"",now()[:10],rows)
        _record_search_deltas(db,run_id,"Bing Webmaster Tools",{"current":{k:(v if isinstance(v,list) else []) for k,v in datasets.items()},"previous":{}})
        return {"status":"connected","detail":"Authenticated Bing Webmaster JSON/HTTP read-only API. Legacy SOAP/POX are not used. API result date coverage depends on each report.","site":{k:match.get(k) for k in ("Url","IsVerified")},"data":datasets}
    except ApiError as e:
        return {"status":"error","detail":f"Bing JSON/HTTP API request failed: {e}. Check API key/OAuth scope, verified site, endpoint access and rate limits."}

def _record_search_deltas(db,run_id,source,periods):
    if run_id is None or "current" not in periods or "previous" not in periods: return
    current=periods["current"]; previous=periods["previous"]
    for dimension in ("site","page","query","query_page"):
        old={tuple(r.get("keys",[])):r for r in previous.get(dimension,[])}
        for row in current.get(dimension,[]):
            key=tuple(row.get("keys",[])); prior=old.get(key)
            if not prior: continue
            before=prior.get("clicks",0); after=row.get("clicks",0); impr=row.get("impressions",0)
            if dimension=="site" and before>=20 and after<before*0.7:
                issue(db,run_id,"medium","search_traffic_drop",SITE,f"{source}: clicks {before:g} → {after:g} in consecutive 28-day finalized periods","Review the affected pages/queries and compare seasonality, deployment changes and search reporting.")
            if dimension in ("page","query_page") and before>=5 and after<before*0.5:
                target=key[-1] if dimension=="page" else key[1]
                issue(db,run_id,"medium","search_traffic_drop",target,f"{source} clicks {before:g} → {after:g}; impressions {prior.get('impressions',0):g} → {impr:g}","Review the page/query pair, intent fit, technical changes and the date comparison.")
    # Query/page pairs are a real GSC cannibalization signal, not proof of harmful cannibalization.
    by_query={}
    for row in current.get("query_page",[]):
        keys=row.get("keys",[])
        if len(keys)>=2 and row.get("impressions",0)>0: by_query.setdefault(keys[0],set()).add(keys[1])
    for query,pages in by_query.items():
        if len(pages)>1:
            issue(db,run_id,"low","possible_cannibalization",SITE,f"{source} query {query!r} appeared with {len(pages)} pages: {', '.join(sorted(pages)[:5])}","Review whether these pages serve distinct intents; this signal alone does not establish a problem.")

def connector_snapshot(run_id=None):
    db=connect()
    google=gsc_snapshot(db,run_id)
    bing=bing_snapshot(db,run_id)
    for source,state in (("Google Search Console",google),("Bing Webmaster Tools",bing)):
        db.execute("INSERT INTO snapshots(captured,source,payload,status) VALUES(?,?,?,?)",(now(),source,json.dumps(state,ensure_ascii=False),state["status"]))
    db.commit(); db.close()
    return {"Google Search Console":google,"Bing Webmaster Tools":bing}

def inspect_gsc_url(url):
    parsed=urllib.parse.urlsplit(url); site=urllib.parse.urlsplit(SITE)
    if parsed.scheme!="https" or parsed.netloc.lower()!=site.netloc.lower(): return {"status":"rejected","detail":"Inspection accepts HTTPS URLs on the configured site only."}
    if not all(os.environ.get(k) for k in ("GOOGLE_CLIENT_ID","GOOGLE_CLIENT_SECRET","GOOGLE_REFRESH_TOKEN")):
        state={"status":"configured_not_connected","detail":"Set Google OAuth credentials to use URL Inspection."}
    else:
        try:
            token=google_access_token(); prop=os.environ.get("GOOGLE_SEARCH_CONSOLE_SITE_URL","")
            if not prop: raise ApiError(0,"GOOGLE_SEARCH_CONSOLE_SITE_URL is missing")
            body={"inspectionUrl":url,"siteUrl":prop,"languageCode":"en-US"}
            result=gsc_api("https://searchconsole.googleapis.com/v1/urlInspection/index:inspect",token,"POST",body)
            info=result.get("inspectionResult",{}); index=info.get("indexStatusResult",{})
            state={"status":"connected","detail":"Google URL Inspection API snapshot. Verdict and canonical/index state describe Google’s latest inspection, not a guarantee of inclusion.","url":url,"inspection_result_link":info.get("inspectionResultLink"),"index_status":{k:index.get(k) for k in ("verdict","coverageState","robotsTxtState","indexingState","pageFetchState","lastCrawlTime","googleCanonical","userCanonical","crawledAs","referringUrls","sitemaps","robotsTxtState") if k in index},"result":{k:info[k] for k in ("mobileUsabilityResult","richResultsResult") if k in info}}
        except ApiError as e: state={"status":"error","detail":f"Google URL Inspection failed: {e}"}
    db=connect(); db.execute("INSERT INTO snapshots(captured,source,payload,status) VALUES(?,?,?,?)",(now(),"Google URL Inspection: "+url,json.dumps(state),state["status"])); db.commit(); db.close()
    return state

def auto_fix_sitemap_url(url,run_id):
    """Add only a successfully fetched, self-canonical, indexable, same-origin URL."""
    parsed=urllib.parse.urlsplit(url); base=urllib.parse.urlsplit(SITE)
    if parsed.scheme!="https" or parsed.netloc.lower()!=base.netloc.lower(): return {"status":"rejected","detail":"URL is not HTTPS on the configured host."}
    db=connect()
    row=db.execute("SELECT status,final_url,canonical,x_robots_tag FROM urls WHERE run_id=? AND url=?",(run_id,url)).fetchone()
    if not row or row["status"] is None or not 200<=row["status"]<300: db.close(); return {"status":"rejected","detail":"URL has no successful 2xx result in this audit."}
    if re.search(r"(?i)\bnoindex\b",row["x_robots_tag"] or ""): db.close(); return {"status":"rejected","detail":"Response has a noindex directive; review required."}
    if not row["canonical"] or normalized_for_site(url,row["canonical"])!=normalized_for_site(url,row["final_url"] or url): db.close(); return {"status":"rejected","detail":"A self-canonical matching the successful final URL was not verified."}
    sitemap=ROOT/"sitemap.xml"
    old=sitemap.read_text(encoding="utf-8")
    root=ET.fromstring(old)
    existing={e.text.strip() for e in root.iter() if e.tag.endswith("loc") and e.text}
    if url in existing: db.close(); return {"status":"already_present","detail":"URL is already listed in sitemap."}
    snippet="  <url>\n    <loc>"+html.escape(url,quote=True)+"</loc>\n  </url>\n"
    closing="</urlset>"
    if closing not in old: db.close(); return {"status":"failed","detail":"Expected </urlset> root closure not found; no change made."}
    new=old.replace(closing,snippet+closing,1)
    # Validate output before touching the source; keep a per-fix exact backup and reviewable diff.
    parsed_root=ET.fromstring(new)
    if url not in {e.text.strip() for e in parsed_root.iter() if e.tag.endswith("loc") and e.text}: db.close(); return {"status":"failed","detail":"Generated sitemap did not validate."}
    backup_dir=DATA/"fix-backups"; backup_dir.mkdir(parents=True,exist_ok=True)
    backup=backup_dir/f"sitemap-{run_id}-{int(time.time())}.xml"; backup.write_text(old,encoding="utf-8")
    diff="".join(difflib.unified_diff(old.splitlines(keepends=True),new.splitlines(keepends=True),fromfile="sitemap.xml before",tofile="sitemap.xml after"))
    temp=sitemap.with_suffix(".xml.monitor-tmp"); temp.write_text(new,encoding="utf-8"); ET.parse(temp); os.replace(temp,sitemap)
    result={"status":"verified","url":url,"backup":str(backup),"diff":diff,"verification":"XML parsed and the exact URL is present once."}
    fingerprint=hashlib.sha256(f"sitemap_coverage\0{url}\0Discovered HTML route is absent from current sitemap".encode()).hexdigest()
    db.execute("INSERT INTO fixes(fingerprint,url,created,status,backup_path,diff,verification_json) VALUES(?,?,?,?,?,?,?)",(fingerprint,url,now(),"verified",str(backup),diff,json.dumps(result)))
    db.execute("UPDATE issue_registry SET status='resolved',resolved_at=?,last_run=? WHERE category='sitemap_coverage' AND url=?",(now(),run_id,url))
    db.execute("INSERT OR IGNORE INTO issue_events(fingerprint,run_id,action,occurred) VALUES(?,?,?,?)",(fingerprint,run_id,"auto_fix_verified",now()))
    db.close(); return result

def normalized_for_site(page_url,candidate):
    return urllib.parse.urlunsplit((urllib.parse.urlsplit(urllib.parse.urljoin(page_url,candidate)).scheme.lower(),urllib.parse.urlsplit(urllib.parse.urljoin(page_url,candidate)).netloc.lower(),urllib.parse.urlsplit(urllib.parse.urljoin(page_url,candidate)).path or "/","",""))

def review_content(url,status):
    if status not in ("accepted","dismissed"): return {"status":"rejected"}
    db=connect(); db.execute("UPDATE content_reviews SET status=? WHERE id=(SELECT id FROM content_reviews WHERE url=? ORDER BY id DESC LIMIT 1)",(status,url)); db.commit(); db.close(); return {"status":status}

def acknowledge_alert(alert_id):
    db=connect(); cur=db.execute("UPDATE alerts SET status='acknowledged' WHERE id=? AND status='open'",(int(alert_id),)); db.commit(); db.close(); return cur.rowcount==1

def metric_summary(db):
    rows=db.execute("SELECT source,dimension,key,period_start,period_end,clicks,impressions,ctr,position FROM metrics ORDER BY id DESC LIMIT 500").fetchall()
    # Most recent value per source/dimension/key/period; preserve API returned aggregate fields.
    latest={}
    for r in rows:
        k=(r["source"],r["dimension"],r["key"],r["period_start"],r["period_end"])
        latest.setdefault(k,dict(r))
    return list(latest.values())


def dashboard():
    db=connect()
    run=db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    issues=db.execute("SELECT severity,category,url,evidence,recommendation,status,fingerprint FROM issue_registry WHERE status IN ('open','reopened') ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,last_seen DESC LIMIT 300").fetchall()
    reviews=db.execute("SELECT url,title,word_count,evidence,status FROM content_reviews WHERE run_id=? ORDER BY word_count ASC LIMIT 150",(run["id"],)).fetchall() if run else []
    counts={r["severity"]:r["n"] for r in db.execute("SELECT severity,COUNT(*) n FROM issue_registry WHERE status IN ('open','reopened') GROUP BY severity").fetchall()}
    recent=db.execute("SELECT id,started,finished,status,urls_found,urls_checked FROM runs ORDER BY id DESC LIMIT 12").fetchall()
    logs=db.execute("SELECT occurred,level,message FROM monitor_logs ORDER BY id DESC LIMIT 30").fetchall()
    alerts=db.execute("SELECT id,severity,message,occurrences,last_seen,status FROM alerts WHERE status='open' ORDER BY last_seen DESC LIMIT 30").fetchall()
    file_fixes=db.execute("SELECT path,status,backup_path,diff,verification,created FROM file_fixes ORDER BY id DESC LIMIT 15").fetchall()
    verified=db.execute("SELECT source,status,captured,payload FROM snapshots WHERE source LIKE 'verification:%' OR source LIKE 'Google URL Inspection:%' ORDER BY id DESC LIMIT 12").fetchall()
    fixes=db.execute("SELECT url,created,status,backup_path,diff,verification_json FROM fixes ORDER BY id DESC LIMIT 12").fetchall()
    snaps=db.execute("SELECT source,status,captured,payload FROM snapshots ORDER BY id DESC LIMIT 4").fetchall(); db.close()
    metric_db=connect(); metrics=metric_summary(metric_db); metric_db.close()
    conns={}
    for s in snaps:
        if s["source"] not in conns: conns[s["source"]]={"status":s["status"],"captured":s["captured"],"payload":json.loads(s["payload"])}
    run_summary=json.loads(run["summary"] or "{}") if run else {}
    def esc(v): return html.escape(str(v or ""),quote=True)
    issue_rows="".join(f"<tr><td><b class='{esc(i['severity'])}'>{esc(i['severity'])}</b></td><td>{esc(i['category'])}</td><td><a href='{esc(i['url'])}' target='_blank' rel='noreferrer'>{esc(i['url'])}</a></td><td>{esc(i['evidence'])}</td><td>{esc(i['recommendation'])}<form method='post' action='/verify'><input type='hidden' name='url' value='{esc(i['url'])}'><button class='secondary' type='submit'>Verify URL</button></form>{('<form method="post" action="/fix-sitemap"><input type="hidden" name="url" value="'+esc(i['url'])+'"><input type="hidden" name="run_id" value="'+str(run['id'] if run else 0)+'"><button type="submit">Apply safe sitemap fix</button></form>') if i['category']=='sitemap_coverage' and run else ''}</td></tr>" for i in issues)
    review_rows="".join(f"<tr><td><a href='{esc(r['url'])}' target='_blank' rel='noreferrer'>{esc(r['title'] or r['url'])}</a></td><td>{r['word_count']}</td><td>{esc(r['evidence'])}<br><small>Internal links and crawl findings above provide topic context. Review intent fit, examples, facts, and trust sources.</small></td><td>{esc(r['status'])}<form method='post' action='/review'><input type='hidden' name='url' value='{esc(r['url'])}'><button name='status' value='accepted'>Accept review</button><button class='secondary' name='status' value='dismissed'>Dismiss</button></form></td></tr>" for r in reviews)
    recent_rows="".join(f"<tr><td>{esc(r['started'])}</td><td>{esc(r['status'])}</td><td>{r['urls_checked']}/{r['urls_found']}</td></tr>" for r in recent)
    log_rows="".join(f"<tr><td>{esc(x['occurred'])}</td><td>{esc(x['level'])}</td><td>{esc(x['message'])}</td></tr>" for x in logs)
    alert_rows="".join(f"<tr><td>{esc(a['severity'])}</td><td>{esc(a['last_seen'])}</td><td>{a['occurrences']}</td><td>{esc(a['message'])}<form method='post' action='/alert-ack'><input type='hidden' name='id' value='{a['id']}'><button class='secondary'>Acknowledge</button></form></td></tr>" for a in alerts)
    verify_rows="".join(f"<tr><td>{esc(x['captured'])}</td><td>{esc(x['status'])}</td><td>{esc(json.loads(x['payload']).get('url',''))}</td><td>{esc(json.loads(x['payload']).get('detail',''))}</td></tr>" for x in verified)
    fix_rows="".join(f"<tr><td>{esc(x['created'])}</td><td>{esc(x['status'])}</td><td>{esc(x['url'])}</td><td>{esc(x['backup_path'])}<details><summary>Before/after diff</summary><pre>{esc(x['diff'])}</pre></details><small>{esc(json.loads(x['verification_json']).get('verification',''))}</small></td></tr>" for x in fixes)
    source_fix_rows="".join(f"<tr><td>{esc(f['created'])}</td><td>{esc(f['status'])}</td><td>{esc(f['path'])}</td><td>{esc(f['backup_path'])}<details><summary>Diff and verification</summary><pre>{esc(f['diff'])}</pre>{esc(f['verification'])}</details></td></tr>" for f in file_fixes)
    conn_rows="".join(f"<p><b>{esc(k)}</b>: {esc(v['status'])} <small>{esc(v['captured'])} — {esc(v['payload'].get('detail',''))}</small></p>" for k,v in conns.items()) or "<p>Connector status is unavailable until the first audit.</p>"
    metric_rows="".join(f"<tr><td>{esc(m['source'])}</td><td>{esc(m['dimension'])}</td><td>{esc(m['key'])}</td><td>{esc(m['period_start'])} – {esc(m['period_end'])}</td><td>{esc(m['clicks'])}</td><td>{esc(m['impressions'])}</td><td>{esc(m['ctr'])}</td><td>{esc(m['position'])}</td></tr>" for m in metrics)
    latest=esc(run["started"] if run else "Never")
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>YashiPDF Site Monitor</title><style>
    :root{{color-scheme:dark;--bg:#0b1220;--card:#121d2e;--line:#26374b;--text:#e8eff7;--muted:#9aacc0;--blue:#7cc4ff}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,Segoe UI,sans-serif}}main{{max-width:1400px;margin:auto;padding:28px}}h1{{margin:0}}h2{{font-size:19px}}a{{color:var(--blue);overflow-wrap:anywhere}}small,.muted{{color:var(--muted)}}header,.grid,.buttons{{display:flex;gap:14px;align-items:center;flex-wrap:wrap}}header{{justify-content:space-between;margin-bottom:22px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:14px 0}}.grid>.card{{flex:1;min-width:155px;margin:0}}.n{{font-size:28px;font-weight:700}}button{{border:0;border-radius:8px;background:#1987d4;color:white;padding:11px 16px;font:inherit;cursor:pointer}}button.secondary{{background:#304459}}button:disabled{{opacity:.6}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{position:sticky;top:0;background:var(--card)}}.tablewrap{{overflow:auto;max-height:600px}}.high{{color:#ff8d8d}}.medium{{color:#ffd46a}}.low{{color:#83c9ff}}.ok{{color:#7ce1a8}}.note{{font-size:13px;color:var(--muted)}}
    </style><main><header><div><h1>YashiPDF Site Monitor</h1><div class="muted">Local audit dashboard · {esc(SITE)} · Latest run {latest}</div></div><div class="buttons"><form method="post" action="/run"><button id="run">Run full audit now</button></form><a href="/report.json"><button class="secondary">Export JSON</button></a><a href="/report.md"><button class="secondary">Export report</button></a></div></header>
    <section class="grid"><div class="card"><small>URLs discovered</small><div class="n">{run['urls_found'] if run else 0}</div></div><div class="card"><small>URLs checked</small><div class="n">{run['urls_checked'] if run else 0}</div></div><div class="card"><small>Critical/high</small><div class="n high">{counts.get('critical',0)+counts.get('high',0)}</div></div><div class="card"><small>Medium</small><div class="n medium">{counts.get('medium',0)}</div></div><div class="card"><small>Low</small><div class="n low">{counts.get('low',0)}</div></div><div class="card"><small>Crawl health</small><div class="n {'ok' if run_summary.get('health')=='no_http_errors_found' else 'medium'}">{esc(run_summary.get('health','not run'))}</div></div></section>
    <section class="card"><h2>Crawl configuration and data limits</h2><p>Run interval: {INTERVAL//3600}h · Failed-run retry: {RETRY_INTERVAL//60}m · Maximum URLs: {MAX_URLS} · Sequential requests with safe retries and rate limiting · Robots.txt exclusions respected.</p><p>Latest response-time sample: p50 {esc(run_summary.get('response_ms_p50','unavailable'))} ms; p95 {esc(run_summary.get('response_ms_p95','unavailable'))} ms across {esc(run_summary.get('response_ms_sample_count',0))} fetched URLs.</p><p class="note">Timing includes body transfer; it is not a Core Web Vital or browser runtime measurement. Search metrics are shown only when returned by connected official APIs. Average position is not an exact live ranking. Public crawling cannot reveal indexation status.</p><h2>Search connectors</h2>{conn_rows}</section>
    <section class="card"><h2>Autopilot alerts ({len(alerts)} open)</h2><p class="note">Alerts persist in SQLite. Optional local JSONL notifications use YASHIPDF_LOCAL_NOTIFY=1 and stay on this device.</p><div class="tablewrap"><table><thead><tr><th>Severity</th><th>Last seen</th><th>Occurrences</th><th>Alert</th></tr></thead><tbody>{alert_rows or '<tr><td colspan="4">No active alerts.</td></tr>'}</tbody></table></div></section>
    <section class="card"><h2>Search performance snapshots ({len(metrics)} rows)</h2><div class="tablewrap"><table><thead><tr><th>Source</th><th>Dimension</th><th>Query/page key</th><th>Period</th><th>Clicks</th><th>Impressions</th><th>CTR</th><th>Average position</th></tr></thead><tbody>{metric_rows or '<tr><td colspan="8">No authenticated metrics stored.</td></tr>'}</tbody></table></div></section>
    <section class="card"><h2>Verify and inspect a URL</h2><form method="post" action="/verify"><label>Site URL <input name="url" value="{esc(SITE)}/" required></label><button>Verify live HTTP/metadata</button></form><form method="post" action="/inspect-gsc"><label>Google URL Inspection <input name="url" value="{esc(SITE)}/" required></label><button class="secondary">Inspect Google index state</button></form><p class="note">Google inspection requires credentials and reports the API snapshot; it does not guarantee indexing.</p></section>
    <section class="card"><h2>Issue queue ({len(issues)} active deduplicated findings)</h2><div class="tablewrap"><table><thead><tr><th>Severity</th><th>Type</th><th>URL</th><th>Evidence</th><th>Suggested action</th></tr></thead><tbody>{issue_rows or '<tr><td colspan="5">No active findings.</td></tr>'}</tbody></table></div></section>
    <section class="card"><h2>Content improvement queue ({len(reviews)} pages awaiting review)</h2><p class="note">These are screening signals for an editor, not a quality score. No article text is rewritten automatically.</p><div class="tablewrap"><table><thead><tr><th>Article</th><th>Source words</th><th>Review notes</th><th>Status</th></tr></thead><tbody>{review_rows or '<tr><td colspan="4">No blog pages checked yet.</td></tr>'}</tbody></table></div></section>
    <section class="card"><h2>Audit history</h2><div class="tablewrap"><table><thead><tr><th>Started (UTC)</th><th>Status</th><th>Checked / discovered</th></tr></thead><tbody>{recent_rows or '<tr><td colspan="3">No audits yet.</td></tr>'}</tbody></table></div></section>
    <section class="card"><h2>Verification results</h2><div class="tablewrap"><table><thead><tr><th>When</th><th>Status</th><th>URL</th><th>Evidence</th></tr></thead><tbody>{verify_rows or '<tr><td colspan="4">No URL verifications recorded.</td></tr>'}</tbody></table></div></section>
    <section class="card"><h2>Verified safe fixes ({len(fixes)} recorded)</h2><div class="tablewrap"><table><thead><tr><th>When</th><th>Status</th><th>URL</th><th>Backup and diff</th></tr></thead><tbody>{fix_rows or '<tr><td colspan="4">No automated fixes applied.</td></tr>'}</tbody></table></div></section>
    <section class="card"><h2>Autopilot source fixes ({len(file_fixes)} recorded)</h2><p class="note">Exact known-equivalent href/src repairs require same-run 2xx evidence. Every changed file has a local backup, diff, and SEO source regression check.</p><div class="tablewrap"><table><thead><tr><th>When</th><th>Status</th><th>File</th><th>Backup, diff and verification</th></tr></thead><tbody>{source_fix_rows or '<tr><td colspan="4">No autopilot source fixes applied.</td></tr>'}</tbody></table></div></section>
    <section class="card"><h2>Execution log</h2><div class="tablewrap"><table><thead><tr><th>When (UTC)</th><th>Level</th><th>Message</th></tr></thead><tbody>{log_rows or '<tr><td colspan="3">No execution logs yet.</td></tr>'}</tbody></table></div></section>
    <p class="note">Local-only dashboard bound to loopback. Verified equivalent-route source fixes may be applied locally with backups and rollback; this monitor never deploys changes.</p></main></html>"""


_lock=threading.Lock()
def run_audit_once():
    if not _lock.acquire(False): return {"status":"already_running"}
    try:
        return autopilot_cycle()
    finally: _lock.release()

def verify_url(url):
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != urllib.parse.urlsplit(SITE).netloc.lower():
        return {"status":"rejected","detail":"Verification accepts HTTPS URLs on the configured site only."}
    status,final,body,ctype,error=fetch(url)
    result={"url":url,"http_status":status,"final_url":final,"checked":now(),"error":error}
    if status and status < 400 and "html" in ctype:
        parser=PageParser(); parser.feed(body.decode("utf-8","replace"))
        result.update({"title":parser.title.strip(),"description":parser.description.strip(),"canonical":parser.canonical,"h1_count":parser.h1_count,"status":"verified_http","detail":"HTTP response and current metadata recorded. This does not verify search engine indexation."})
    else:
        result.update({"status":"failed","detail":error or f"HTTP {status}; no HTML metadata verified."})
    db=connect(); db.execute("INSERT INTO snapshots(captured,source,payload,status) VALUES(?,?,?,?)",(result["checked"],"verification:"+url,json.dumps(result),result["status"])); db.commit(); db.close()
    return result

def autopilot_cycle():
    """Run a persisted audit, safe maintenance, connectors, and deduplicated alerts."""
    try:
        result=audit()
    except Exception as e:
        detail=f"{type(e).__name__}: {str(e)[:500]}"; stamp=now(); db=connect()
        row=db.execute("SELECT id FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            db.execute("UPDATE runs SET status='failed',finished=?,summary=? WHERE id=?",(stamp,json.dumps({"execution":"failed","failure":detail}),row["id"]))
            db.execute("INSERT INTO monitor_logs(occurred,level,message,run_id) VALUES(?,?,?,?)",(stamp,"ERROR",f"Audit crashed and was marked failed: {detail}",row["id"]))
        db.commit(); db.close(); result={"id":row["id"] if row else None,"execution":"failed","failure":detail}
    run_id=result.get("id")
    if run_id and result.get("execution")=="completed":
        try:
            from autopilot import apply_safe_repairs
            result["safe_fixes"]=apply_safe_repairs(sys.modules[__name__],run_id)
        except Exception as e: log_event(f"Safe maintenance failed without blocking monitoring: {type(e).__name__}: {e}","ERROR",run_id)
    try: result["connectors"]=connector_snapshot(run_id)
    except Exception as e:
        log_event(f"Search connector collection failed: {type(e).__name__}: {e}","ERROR",run_id); result["connectors"]={"status":"error"}
    try:
        from autopilot import persist_alerts
        result["alerts_created"]=persist_alerts(sys.modules[__name__],run_id,result)
    except Exception as e: log_event(f"Alert persistence failed: {type(e).__name__}: {e}","ERROR",run_id)
    return result

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): console_log(f"{self.log_date_time_string()} {fmt % args}")
    def send(self, code, body, ctype="text/html; charset=utf-8"):
        data=body.encode() if isinstance(body,str) else body
        self.send_response(code); self.send_header("Content-Type",ctype); self.send_header("Content-Length",str(len(data))); self.send_header("X-Content-Type-Options","nosniff"); self.send_header("Cache-Control","no-store"); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path=="/": return self.send(200,dashboard())
        if self.path=="/health": return self.send(200,json.dumps({"status":"ok","time":now()}),"application/json")
        if self.path=="/report.json":
            db=connect(); run=db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone(); issues=[dict(r) for r in db.execute("SELECT * FROM issues WHERE run_id=?",(run["id"],))] if run else []; urls=[dict(r) for r in db.execute("SELECT * FROM urls WHERE run_id=?",(run["id"],))] if run else []; db.close()
            return self.send(200,json.dumps({"run":dict(run) if run else None,"issues":issues,"urls":urls},indent=2),"application/json; charset=utf-8")
        if self.path=="/report.md":
            db=connect(); run=db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone(); issues=db.execute("SELECT severity,category,url,evidence,recommendation FROM issues WHERE run_id=?",(run["id"],)).fetchall() if run else []; db.close()
            out=["# YashiPDF Monitoring Report",f"\nGenerated: {now()} (UTC)",f"\nTarget: {SITE}"]
            if run:
                run_summary=json.loads(run["summary"] or "{}")
                out += [f"\nLatest run: {run['status']} ({run['started']}); crawl health: {run_summary.get('health','unknown')}; checked {run['urls_checked']} of {run['urls_found']} discovered URLs.",f"\nHTTP fetch duration p50/p95: {run_summary.get('response_ms_p50','unavailable')} / {run_summary.get('response_ms_p95','unavailable')} ms (includes body transfer; not Core Web Vitals).","\n## Findings"]+[f"\n- **{r['severity']} · {r['category']}** {r['url']} — {r['evidence']} Suggested: {r['recommendation']}" for r in issues]
            else: out.append("\nNo audit has run.")
            out.append("\nSearch visibility and rankings require connected official APIs and are not inferred.")
            return self.send(200,"\n".join(out),"text/markdown; charset=utf-8")
        return self.send(404,"Not found")
    def do_POST(self):
        if self.path=="/run":
            if not _lock.acquire(False): return self.send(409,"Audit already running", "text/plain")
            def task():
                try:
                    autopilot_cycle()
                finally: _lock.release()
            threading.Thread(target=task,daemon=True).start()
            self.send_response(303); self.send_header("Location","/"); self.end_headers(); return
        if self.path in ("/verify","/inspect-gsc","/fix-sitemap","/review","/alert-ack"):
            length=min(int(self.headers.get("Content-Length","0")),8192)
            form=urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8","replace"))
            url=form.get("url",[""])[0]
            if self.path=="/verify": verify_url(url)
            elif self.path=="/inspect-gsc": inspect_gsc_url(url)
            elif self.path=="/alert-ack":
                try: acknowledge_alert(form.get("id",["0"])[0])
                except ValueError: pass
            elif self.path=="/fix-sitemap":
                try: auto_fix_sitemap_url(url,int(form.get("run_id",["0"])[0]))
                except (ValueError,OSError,ET.ParseError) as e: console_log(f"Sitemap fix failed: {type(e).__name__}: {e}")
            elif self.path=="/review": review_content(url,form.get("status",[""])[0])
            self.send_response(303); self.send_header("Location","/"); self.end_headers(); return
        self.send(404,"Not found")

def scheduler_once():
    db=connect(); last=db.execute("SELECT finished,status,started FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not last: due=True
    else:
        stamp=last["finished"] or last["started"]
        try: age=time.time()-datetime.fromisoformat(stamp).timestamp()
        except (ValueError,TypeError): age=INTERVAL
        due=age >= (INTERVAL if last["status"]=="complete" else RETRY_INTERVAL)
    if not due or not _lock.acquire(False): db.close(); return False
    try:
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT value FROM scheduler_state WHERE key='audit_owner_pid'").fetchone()
        if row:
            try: os.kill(int(row[0]),0); owner_alive=True
            except (OSError,ValueError): owner_alive=False
            if owner_alive and int(row[0])!=os.getpid(): db.rollback(); db.close(); return False
        db.execute("INSERT OR REPLACE INTO scheduler_state(key,value) VALUES('audit_owner_pid',?)",(str(os.getpid()),)); db.commit(); db.close()
        log_event("Scheduled audit acquired process-safe database lease.")
        autopilot_cycle()
        return True
    finally:
        try:
            release=connect(); release.execute("DELETE FROM scheduler_state WHERE key='audit_owner_pid' AND value=?",(str(os.getpid()),)); release.commit(); release.close()
        except Exception as e: console_log(f"Failed to release scheduled audit lease: {type(e).__name__}")
        _lock.release()

def scheduler():
    while True:
        try: scheduler_once()
        except Exception as e: log_event(f"Scheduled audit failed: {type(e).__name__}: {e}","ERROR")
        time.sleep(min(60,INTERVAL))

def recover_interrupted_audits():
    db=connect(); owner=db.execute("SELECT value FROM scheduler_state WHERE key='audit_owner_pid'").fetchone(); recovered=0
    if owner:
        try: os.kill(int(owner[0]),0); alive=True
        except (OSError,ValueError): alive=False
        if not alive:
            db.execute("DELETE FROM scheduler_state WHERE key='audit_owner_pid'")
            cur=db.execute("UPDATE runs SET status='failed',finished=?,summary=? WHERE status='running'",(now(),json.dumps({"execution":"failed","reason":"Process restarted during audit"})))
            recovered=cur.rowcount
            db.execute("INSERT INTO monitor_logs(occurred,level,message) VALUES(?,?,?)",(now(),"WARN","Recovered interrupted audit after previous process exited."))
    db.commit(); db.close(); return recovered

def main():
    console_log(f"Starting YashiPDF monitor; data: {DATA}")
    connect(); recover_interrupted_audits()
    if os.environ.get("YASHIPDF_AUDIT_ONCE") == "1":
        console_log(json.dumps(autopilot_cycle(),indent=2)); return
    host=os.environ.get("YASHIPDF_HOST","127.0.0.1"); port=int(os.environ.get("YASHIPDF_PORT","8765"))
    try: is_loopback=ipaddress.ip_address(host).is_loopback
    except ValueError: is_loopback=host.lower()=="localhost"
    if not is_loopback: raise SystemExit("YASHIPDF_HOST must be localhost or a loopback IP; remote dashboard binding is disabled.")
    server=ThreadingHTTPServer((host,port),Handler)
    threading.Thread(target=scheduler,daemon=True).start()
    console_log(f"YashiPDF monitor on http://{host}:{port} (data: {DATA})")
    try: server.serve_forever()
    except KeyboardInterrupt: console_log("Stopping monitor...")
    finally: server.server_close()

if __name__=="__main__":
    try:
        main()
    except BaseException:
        host_log("Fatal monitor process error:\n" + traceback.format_exc())
        raise
