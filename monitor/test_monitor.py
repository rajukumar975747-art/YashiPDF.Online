import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET

import monitor as m


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.old_data, m.DATA = m.DATA, Path(self.tmp.name) / "data"
        self.site = Path(self.tmp.name) / "site"
        self.site.mkdir()
        (self.site / "robots.txt").write_text("User-agent: *\nAllow: /\nDisallow: /private/*\nSitemap: http://127.0.0.1/sitemap.xml\n")
        (self.site / "sitemap.xml").write_text('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>http://127.0.0.1:8766/</loc></url></urlset>')
        (self.site / "index.html").write_text('<!doctype html><html><head><title>Example home</title><meta name="description" content="Useful summary"><link rel="canonical" href="http://127.0.0.1:8766/"><script type="application/ld+json">{"@type":"WebSite"}</script></head><body><h1>Home</h1><a href="/private/secret.html">private</a><a href="/missing.html">missing</a><img src="x.png"></body></html>')
        (self.site / "private").mkdir()
        (self.site / "private" / "secret.html").write_text("secret")
    def tearDown(self):
        m.DATA = self.old_data
        self.tmp.cleanup()

    def test_parser_extracts_metadata_h1_links_and_schema_errors(self):
        p = m.PageParser()
        p.feed('<head><title>One</title><meta name="description" content="Desc"><link rel="canonical" href="/x"></head><h1>Hi</h1><h1>Two</h1><script type="application/ld+json">{</script><img src="x">')
        self.assertEqual(p.title, "One")
        self.assertEqual(p.description, "Desc")
        self.assertEqual(p.h1_count, 2)
        self.assertEqual(p.images_missing_alt, 1)
        self.assertTrue(p.schema_errors)

    def test_robots_groups_allow_rules_and_specific_agents(self):
        content="User-agent: OtherBot\nDisallow: /\n\nUser-agent: *\nAllow: /\nDisallow: /private/*\n"
        self.assertTrue(m.robots_allowed(content,"/index.html"))
        self.assertFalse(m.robots_allowed(content,"/private/x"))
        self.assertFalse(m.robots_allowed(content,"/index.html","OtherBot"))

    def test_fixture_audit_persists_findings_and_respects_robots(self):
        sitemap = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>http://127.0.0.1/</loc></url></urlset>'
        page = b'<html><head><title>Example home</title><meta name="description" content="Useful summary"><link rel="canonical" href="http://127.0.0.1/"></head><body><h1>Home</h1><a href="/private/secret.html">private</a><a href="/missing.html">missing</a><a href="/other.html">other</a></body></html>'
        def fake_fetch(url):
            if url.endswith("robots.txt"): return 200,url,b"User-agent: *"+bytes([10])+b"Allow: /"+bytes([10])+b"Disallow: /private/*", "text/plain", ""
            if url.endswith("sitemap.xml"): return 200,url,sitemap,"application/xml",""
            if url.endswith("missing.html"): return 404,url,b"Not found","text/html","HTTP 404"
            if url.endswith("other.html"): return 200,url,b'<html><head><title>Other</title><meta name="description" content="Other"><link rel="canonical" href="http://127.0.0.1/other.html"></head><body><h1>Other</h1></body></html>',"text/html",""
            if "/private/" in url: return 200,url,b"private","text/html",""
            return 200,url,page,"text/html",""
        with patch.object(m,"fetch_detailed",side_effect=lambda url: (*fake_fetch(url),[],{})):
            result = m.audit("http://127.0.0.1/", max_urls=10)
        self.assertEqual(result["execution"], "completed")
        self.assertEqual(result["urls_checked"], 3)
        db = m.connect()
        urls = [r[0] for r in db.execute("SELECT url FROM urls")]
        self.assertIn("http://127.0.0.1/private/secret.html", urls)
        self.assertIsNone(db.execute("SELECT status FROM urls WHERE url='http://127.0.0.1/private/secret.html'").fetchone()[0])
        issue_categories = [r[0] for r in db.execute("SELECT category FROM issues")]
        self.assertIn("http", issue_categories)
        self.assertIn("sitemap_coverage", issue_categories)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM runs WHERE status='complete'").fetchone()[0], 1)
        db.close()

    def test_missing_credentials_are_truthfully_reported(self):
        for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "GOOGLE_SEARCH_CONSOLE_SITE_URL", "BING_WEBMASTER_API_KEY", "BING_WEBMASTER_ACCESS_TOKEN", "BING_WEBMASTER_SITE_URL", "BING_WEBMASTER_CLIENT_ID", "BING_WEBMASTER_CLIENT_SECRET", "BING_WEBMASTER_REFRESH_TOKEN"):
            __import__("os").environ.pop(key, None)
        state = m.connector_snapshot()
        self.assertEqual(state["Google Search Console"]["status"], "configured_not_connected")
        self.assertEqual(state["Bing Webmaster Tools"]["status"], "configured_not_connected")

    def test_windowless_monitor_output_is_persisted_to_host_log(self):
        log_path=Path(self.tmp.name)/"logs"/"monitor-host.log"
        with patch.object(m,"HOST_LOG",log_path), patch.object(m.sys,"stdout",None):
            m.console_log("windowless task diagnostic")
        self.assertIn("windowless task diagnostic",log_path.read_text(encoding="utf-8"))

    def test_verify_url_is_site_scoped_and_persisted(self):
        with patch.object(m,"fetch",return_value=(200,"https://www.yashipdf.online/",b'<html><head><title>Home</title></head><body><h1>Home</h1></body></html>',"text/html","")):
            result=m.verify_url("https://www.yashipdf.online/")
        self.assertEqual(result["status"],"verified_http")
        self.assertEqual(m.verify_url("https://example.com/\u0000")["status"],"rejected")
        db=m.connect(); self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots WHERE source LIKE 'verification:%'").fetchone()[0],1); db.close()

    def test_dashboard_and_blog_sitemap_route(self):
        db=m.connect()
        cur=db.execute("INSERT INTO runs(started,status) VALUES(?,?)",(m.now(),"complete"))
        m.issue(db,cur.lastrowid,"medium","sitemap_coverage","https://www.yashipdf.online/example","Not listed in sitemap","Add after verification")
        db.commit(); db.close()
        page=m.dashboard()
        self.assertIn("Run full audit now",page)
        self.assertIn("Content improvement queue",page)
        self.assertIn("/report.json",page)
        self.assertIn("Apply safe sitemap fix",page)
        self.assertIn("Google URL Inspection",page)
        root=Path(__file__).resolve().parent.parent
        urls={e.text.strip() for e in ET.parse(root/"sitemap.xml").getroot().iter() if e.tag.endswith("loc") and e.text}
        self.assertIn("https://www.yashipdf.online/blog",urls)

    def test_scheduler_runs_when_due_and_suppresses_duplicate(self):
        db=m.connect(); db.execute("INSERT INTO runs(started,finished,status) VALUES(?,?,?)",("2000-01-01T00:00:00+00:00","2000-01-01T00:00:00+00:00","complete")); db.commit(); db.close()
        def finish_audit():
            db=m.connect(); cur=db.execute("INSERT INTO runs(started,finished,status) VALUES(?,?,?)",(m.now(),m.now(),"complete")); db.commit(); db.close()
            return {"id":cur.lastrowid,"execution":"completed","http_errors":0,"connection_failures":0}
        with patch.object(m,"INTERVAL",60), patch.object(m,"audit",side_effect=finish_audit), patch.object(m,"connector_snapshot",return_value={}):
            self.assertTrue(m.scheduler_once())
            self.assertFalse(m.scheduler_once())
        m._lock.acquire()
        try:
            with patch.object(m,"INTERVAL",60): self.assertFalse(m.scheduler_once())
        finally: m._lock.release()

    def test_scheduler_failed_runs_use_retry_backoff(self):
        db=m.connect(); db.execute("INSERT INTO runs(started,finished,status) VALUES(?,?,?)",(m.now(),m.now(),"failed")); db.commit(); db.close()
        with patch.object(m,"INTERVAL",60), patch.object(m,"RETRY_INTERVAL",900):
            self.assertFalse(m.scheduler_once())

    def test_restart_marks_orphaned_run_failed_and_releases_lease(self):
        db=m.connect(); run=db.execute("INSERT INTO runs(started,status) VALUES(?,?)",(m.now(),"running")).lastrowid
        db.execute("INSERT INTO scheduler_state(key,value) VALUES('audit_owner_pid','2147483647')"); db.commit(); db.close()
        with patch.object(m.os,"kill",side_effect=ProcessLookupError):
            self.assertEqual(m.recover_interrupted_audits(),1)
        db=m.connect(); self.assertEqual(db.execute("SELECT status FROM runs WHERE id=?",(run,)).fetchone()[0],"failed"); self.assertIsNone(db.execute("SELECT value FROM scheduler_state WHERE key='audit_owner_pid'").fetchone()); db.close()

    def test_autopilot_safe_link_repair_has_backup_diff_and_rolls_back(self):
        import autopilot
        root=Path(self.tmp.name)/"project"; source_dir=root/"blog"; source_dir.mkdir(parents=True)
        source=source_dir/"article.html"; original='<a href="https://www.yashipdf.online/blog/convert-tiff-to-gpj-online">Convert TIFF to JPG</a>'
        source.write_text(original,encoding="utf-8")
        db=m.connect(); run=db.execute("INSERT INTO runs(started,status) VALUES(?,?)",(m.now(),"complete")).lastrowid
        old="https://www.yashipdf.online/blog/convert-tiff-to-gpj-online"; new="https://www.yashipdf.online/blog/convert-tiff-to-jpg-online"
        db.execute("INSERT INTO urls(run_id,url,status) VALUES(?,?,?)",(run,old,404)); db.execute("INSERT INTO urls(run_id,url,status) VALUES(?,?,?)",(run,new,200)); db.execute("INSERT INTO internal_links(run_id,source,target,status,kind) VALUES(?,?,?,?,?)",(run,"https://www.yashipdf.online/blog/article",old,404,"link")); db.commit(); db.close()
        result=autopilot.apply_safe_repairs(m,run,root,validator=lambda:True)
        self.assertEqual(len(result),1); self.assertIn(new,source.read_text(encoding="utf-8")); self.assertTrue(Path(result[0]["backup"]).exists()); self.assertIn("before",result[0]["diff"])
        source.write_text(original,encoding="utf-8")
        db=m.connect(); run2=db.execute("INSERT INTO runs(started,status) VALUES(?,?)",(m.now(),"complete")).lastrowid
        db.execute("INSERT INTO urls(run_id,url,status) VALUES(?,?,?)",(run2,old,404)); db.execute("INSERT INTO urls(run_id,url,status) VALUES(?,?,?)",(run2,new,200)); db.execute("INSERT INTO internal_links(run_id,source,target,status,kind) VALUES(?,?,?,?,?)",(run2,"https://www.yashipdf.online/blog/article",old,404,"link")); db.commit(); db.close()
        result=autopilot.apply_safe_repairs(m,run2,root,validator=lambda:False)
        self.assertEqual(result,[]); self.assertEqual(source.read_text(encoding="utf-8"),original)
        db=m.connect(); self.assertEqual(db.execute("SELECT status FROM file_fixes WHERE run_id=?",(run2,)).fetchone()[0],"rolled_back"); db.close()

    def test_autopilot_alerts_deduplicate_and_local_outbox_is_optional(self):
        import autopilot
        with patch.dict(os.environ,{"YASHIPDF_LOCAL_NOTIFY":"1","YASHIPDF_ALERT_FILE":str(Path(self.tmp.name)/"alerts.jsonl")},clear=False):
            first=autopilot.persist_alerts(m,55,{"execution":"completed","http_errors":12,"connection_failures":0})
            second=autopilot.persist_alerts(m,56,{"execution":"completed","http_errors":12,"connection_failures":0})
        self.assertEqual(first,1); self.assertEqual(second,0)
        db=m.connect(); self.assertEqual(db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0],1); self.assertEqual(db.execute("SELECT occurrences FROM alerts").fetchone()[0],2); db.close()
        self.assertEqual(len((Path(self.tmp.name)/"alerts.jsonl").read_text(encoding="utf-8").splitlines()),1)

    def test_alert_acknowledgement_is_persisted(self):
        import autopilot
        autopilot.persist_alerts(m,5,{"execution":"completed","http_errors":10,"connection_failures":0})
        db=m.connect(); alert_id=db.execute("SELECT id FROM alerts").fetchone()[0]; db.close()
        self.assertTrue(m.acknowledge_alert(alert_id)); self.assertFalse(m.acknowledge_alert(alert_id))

    def test_issue_registry_deduplicates_and_tracks_occurrences(self):
        db=m.connect()
        m.issue(db,1,"medium","metadata","https://www.yashipdf.online/x","missing title","Add a title")
        m.issue(db,2,"medium","metadata","https://www.yashipdf.online/x","missing title","Add a title")
        db.commit()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM issue_registry").fetchone()[0],1)
        self.assertEqual(db.execute("SELECT occurrences FROM issue_registry").fetchone()[0],2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM issue_events WHERE action='observed'").fetchone()[0],2)
        db.close()

    def test_sitemap_safe_fix_is_minimal_backed_up_and_verified(self):
        old_root=m.ROOT
        site=Path(self.tmp.name)/"project"; site.mkdir()
        sitemap=site/"sitemap.xml"
        original='<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n  <url><loc>https://www.yashipdf.online/</loc></url>\n</urlset>\n'
        sitemap.write_text(original,encoding="utf-8"); m.ROOT=site
        db=m.connect(); run=db.execute("INSERT INTO runs(started,status) VALUES(?,?)",(m.now(),"running")).lastrowid
        url="https://www.yashipdf.online/new-route"; db.execute("INSERT INTO urls(run_id,url,status,final_url,canonical,x_robots_tag,checked) VALUES(?,?,?,?,?,?,?)",(run,url,200,url,url,"",m.now())); db.commit(); db.close()
        try:
            result=m.auto_fix_sitemap_url(url,run)
            self.assertEqual(result["status"],"verified")
            self.assertIn("</urlset>",result["diff"])
            self.assertTrue(Path(result["backup"]).exists())
            self.assertIn(url,sitemap.read_text(encoding="utf-8"))
            self.assertEqual(sitemap.read_text(encoding="utf-8").replace("  <url>\n    <loc>https://www.yashipdf.online/new-route</loc>\n  </url>\n", ""),original)
        finally: m.ROOT=old_root

    def test_google_oauth_connector_persists_real_api_rows_when_mock_authorized(self):
        values={"GOOGLE_CLIENT_ID":"test-id","GOOGLE_CLIENT_SECRET":"test-secret","GOOGLE_REFRESH_TOKEN":"test-refresh","GOOGLE_SEARCH_CONSOLE_SITE_URL":"sc-domain:yashipdf.online"}
        with patch.dict(os.environ,values,clear=False), patch.object(m,"google_access_token",return_value="access-token"), patch.object(m,"gsc_api") as api:
            def response(url,token,method="GET",payload=None):
                if url.endswith("/sitemaps"): return {"sitemap":[{"path":"https://www.yashipdf.online/sitemap.xml","errors":0}]}
                if payload and payload.get("dimensions")==["query"]: return {"rows":[{"keys":["compress image to 50kb"],"clicks":7,"impressions":80,"ctr":0.0875,"position":8.4}]}
                if payload and payload.get("dimensions")==["page"]: return {"rows":[{"keys":["https://www.yashipdf.online/compress"],"clicks":7,"impressions":80,"ctr":0.0875,"position":8.4}]}
                return {"rows":[]}
            api.side_effect=response
            db=m.connect(); state=m.gsc_snapshot(db,1); db.commit()
            try:
                self.assertEqual(state["status"],"connected",state)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM metrics WHERE source='Google Search Console'").fetchone()[0],4,state)
                self.assertIn("not an exact live rank",state["detail"])
            finally: db.close()

    def test_bing_uses_documented_json_http_and_authenticated_site_list(self):
        with patch.dict(os.environ,{"BING_WEBMASTER_API_KEY":"test-key","BING_WEBMASTER_SITE_URL":"https://www.yashipdf.online/"},clear=False), patch.object(m,"bing_api",side_effect=[[{"Url":"https://www.yashipdf.online/","IsVerified":True}],[],[],[],[],[]]) as api:
            db=m.connect(); state=m.bing_snapshot(db,1); db.close()
        self.assertEqual(state["status"],"connected")
        self.assertEqual(api.call_args_list[0].args[0],"GetUserSites")
        with patch.object(m,"request_json",return_value={"d":[1]}) as request:
            result=m.bing_api("GetQueryStats","https://www.yashipdf.online/",("key","test-secret"))
        self.assertEqual(result,[1])
        self.assertIn("/webmaster/api.svc/json/GetQueryStats?",request.call_args.args[0])
        self.assertNotIn("/pox/",request.call_args.args[0])

    def test_connection_failures_are_classified_without_outage_claim(self):
        import socket, urllib.error
        self.assertIn("DNS resolution failed",m.classify_connection_error(urllib.error.URLError(socket.gaierror("no resolver"))))
        self.assertIn("timed out",m.classify_connection_error(TimeoutError("timed out")))


if __name__ == "__main__":
    unittest.main()
