"""Local autopilot maintenance and alerting; no external services by default."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# These destinations are explicit equivalents backed by source routes in this repository.
SAFE_LINK_REPAIRS = {
    "/blog/convert-tiff-to-gpj-online": "/blog/convert-tiff-to-jpg-online",
    "/blog/crop-image": "/crop",
    "/blog/jpg-to-pdf-converter": "/img-pdf",
    "/crop-image": "/crop",
    "/compress-image-to-50kb-online-free": "/compress",
    "/image-compressor": "/compress",
    "/jpg-to-pdf-converter": "/img-pdf",
    "/png-to-jpg": "/png-jpg",
    "/resize-image-to-200x230-pixels-online": "/resize",
    "/favicon.ico": "/favicon.png",
}


def _source_file(root, source_url):
    path=urlsplit(source_url).path.strip("/")
    candidate=Path(root)/(path+".html" if path else "index.html")
    return candidate if candidate.is_file() else None


def apply_safe_repairs(m, run_id, root=None, validator=None):
    """Repair an exact known href/src only when same-run evidence proves destination 2xx.

    Every changed source gets a byte-for-byte backup and unified diff. Validation failure
    restores every touched file before returning.
    """
    if os.environ.get("YASHIPDF_AUTOPILOT_SAFE_FIXES","1").strip().lower() in ("0","false","no"):
        return []
    root=Path(root or m.ROOT)
    if validator is None:
        audit_script=root/"scripts"/"seo-audit.js"
        validator=lambda: subprocess.run(["node",str(audit_script),str(root)],capture_output=True,text=True,timeout=120).returncode==0
    db=m.connect(); rows=db.execute("SELECT url,status FROM urls WHERE run_id=?",(run_id,)).fetchall()
    statuses={r["url"]:r["status"] for r in rows}; paths={urlsplit(r["url"]).path.rstrip("/"):r["status"] for r in rows}
    edges=db.execute("SELECT source,target,kind FROM internal_links WHERE run_id=? AND status=404",(run_id,)).fetchall()
    changes={}; assertions=[]
    site=urlsplit(m.SITE)
    for edge in edges:
        old=urlsplit(edge["target"]); new_path=SAFE_LINK_REPAIRS.get(old.path.rstrip("/"))
        if not new_path: continue
        dest=urlunsplit((site.scheme,site.netloc,new_path,"",""))
        status=statuses.get(dest,paths.get(new_path.rstrip("/")))
        if status is None or not 200<=status<300: continue
        source=_source_file(root,edge["source"])
        if not source: continue
        original_text=source.read_text(encoding="utf-8")
        before=changes[source][1] if source in changes else original_text
        # Only exact quoted href/src attributes are eligible. Narrative/code examples remain untouched.
        attr=re.compile(r"(?P<p>\b(?:href|src)\s*=\s*)(?P<q>['\"])(?P<v>[^'\"]+)(?P=q)",re.I)
        after,count=attr.subn(lambda x:x.group("p")+x.group("q")+dest+x.group("q") if x.group("v")==edge["target"] else x.group(0),before)
        if old.path.rstrip("/")=="/favicon.ico":
            def correct_icon_type(match):
                tag=match.group(0)
                if re.search(r"(?i)\brel\s*=\s*['\"]?icon",tag) and dest in tag:
                    tag=re.sub(r"(?i)(\btype\s*=\s*['\"])image/x-icon(['\"])",r"\1image/png\2",tag)
                return tag
            after=re.sub(r"(?is)<link\b[^>]*>",correct_icon_type,after)
        if count and after!=before:
            original=changes[source][0] if source in changes else original_text
            changes[source]=(original,after); assertions.append((source,edge["target"],dest))
    if not changes:
        db.close(); return []
    backup_dir=m.DATA/"fix-backups"; backup_dir.mkdir(parents=True,exist_ok=True)
    written=[]
    try:
        for path,(before,after) in changes.items():
            stamp=int(time.time()*1000); rel=path.relative_to(root).as_posix()
            backup=backup_dir/f"autopilot-{run_id}-{rel.replace('/','_')}-{stamp}.bak"
            shutil.copy2(path,backup)
            diff="".join(difflib.unified_diff(before.splitlines(keepends=True),after.splitlines(keepends=True),fromfile=rel+" before",tofile=rel+" after"))
            temp=path.with_suffix(path.suffix+".autopilot-tmp"); temp.write_text(after,encoding="utf-8"); os.replace(temp,path)
            written.append((path,before,backup,diff))
        for path,old,dest in assertions:
            content=path.read_text(encoding="utf-8")
            attrs=list(attr.finditer(content))
            remaining=[match.group("v") for match in attrs if match.group("v")==old]
            if dest not in content or remaining: raise ValueError(f"Link verification failed for {path}: target={old!r}, destination_present={dest in content}, unchanged_attributes={len(remaining)}")
        if not validator(): raise ValueError("SEO regression checker failed")
        for path,before,backup,diff in written:
            mdb=m.connect(); mdb.execute("INSERT INTO file_fixes(run_id,path,category,created,status,backup_path,diff,verification) VALUES(?,?,?,?,?,?,?,?)",(run_id,path.relative_to(root).as_posix(),"equivalent_route",m.now(),"verified",str(backup),diff,"Same-run destination returned 2xx; attribute assertion and SEO regression check passed.")); mdb.commit(); mdb.close()
        m.log_event(f"Autopilot safely repaired {len(assertions)} exact links across {len(written)} backed-up files.",run_id=run_id)
        return [{"path":str(path.relative_to(root)),"backup":str(backup),"diff":diff} for path,before,backup,diff in written]
    except Exception as e:
        for path,before,backup,diff in reversed(written):
            try: path.write_text(before,encoding="utf-8")
            except OSError: pass
        for path,before,backup,diff in written:
            mdb=m.connect(); mdb.execute("INSERT INTO file_fixes(run_id,path,category,created,status,backup_path,diff,verification) VALUES(?,?,?,?,?,?,?,?)",(run_id,path.relative_to(root).as_posix(),"equivalent_route",m.now(),"rolled_back",str(backup),diff,f"Validation failure restored the original bytes: {type(e).__name__}: {e}")); mdb.commit(); mdb.close()
        m.log_event(f"Autopilot repair validation failed; rolled back {len(written)} files: {type(e).__name__}: {e}","ERROR",run_id)
        return []
    finally:
        db.close()


def persist_alerts(m, run_id, summary):
    db=m.connect(); candidates=[]
    if summary.get("execution")!="completed": candidates.append(("critical",f"Audit {summary.get('execution')}: {summary.get('failure','see execution log')}"))
    if summary.get("http_errors",0)>=10 or summary.get("connection_failures",0)>0:
        candidates.append(("high",f"Audit regression: {summary.get('http_errors',0)} HTTP errors and {summary.get('connection_failures',0)} connection failures."))
    for row in db.execute("SELECT severity,category,url,evidence FROM issue_registry WHERE last_run=? AND severity IN ('critical','high')",(run_id,)):
        candidates.append((row["severity"],f"{row['category']}: {row['url']} — {row['evidence']}"))
    for row in db.execute("SELECT url,evidence,occurrences FROM issue_registry WHERE category='http' AND status IN ('open','reopened') AND occurrences>=2 AND last_run=?",(run_id,)):
        candidates.append(("medium",f"Repeated HTTP issue ({row['occurrences']} audits): {row['url']} — {row['evidence']}"))
    for row in db.execute("SELECT url,evidence FROM issue_registry WHERE category='search_traffic_drop' AND last_run=?",(run_id,)):
        candidates.append(("medium",f"Measured search traffic decline: {row['url']} — {row['evidence']}"))
    newly_created=[]
    for severity,message in candidates:
        fp=hashlib.sha256(message.encode()).hexdigest(); stamp=m.now()
        if not db.execute("SELECT 1 FROM alerts WHERE fingerprint=?",(fp,)).fetchone(): newly_created.append(fp)
        db.execute("INSERT INTO alerts(fingerprint,severity,message,first_seen,last_seen,occurrences,last_run,status) VALUES(?,?,?,?,?,1,?,'open') ON CONFLICT(fingerprint) DO UPDATE SET last_seen=excluded.last_seen,occurrences=alerts.occurrences+1,last_run=excluded.last_run,status='open'",(fp,severity,message,stamp,stamp,run_id))
    db.commit(); rows=db.execute("SELECT id,severity,message,occurrences FROM alerts WHERE last_run=? AND status='open' AND fingerprint IN ("+(",".join("?" for _ in newly_created) or "NULL")+")",(run_id,*newly_created)).fetchall(); db.close()
    if rows and os.environ.get("YASHIPDF_LOCAL_NOTIFY","1").lower() in ("1","true","yes"):
        # This local JSONL outbox works for interactive and headless scheduler runs.
        path=Path(os.environ.get("YASHIPDF_ALERT_FILE",str(m.DATA/"alerts.jsonl"))); path.parent.mkdir(parents=True,exist_ok=True)
        with path.open("a",encoding="utf-8") as f:
            for row in rows: f.write(json.dumps({"occurred":m.now(),"id":row["id"],"severity":row["severity"],"message":row["message"],"occurrences":row["occurrences"]},ensure_ascii=False)+"\n")
    return len(rows)
