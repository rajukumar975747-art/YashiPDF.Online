import traceback
import monitor

try:
    print(monitor.audit())
except BaseException as error:
    details=traceback.format_exc()
    print(details)
    try:
        db=monitor.connect()
        run=db.execute("SELECT id FROM runs WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
        if run:
            db.execute("UPDATE runs SET status='failed',finished=?,summary=? WHERE id=?",(monitor.now(),'{"execution":"failed","reason":"unhandled crawl exception; see logs"}',run[0]))
            db.execute("INSERT INTO monitor_logs(occurred,level,message,run_id) VALUES(?,?,?,?)",(monitor.now(),"ERROR",details[-1900:],run[0]))
        db.commit(); db.close()
    except Exception:
        pass
    raise
