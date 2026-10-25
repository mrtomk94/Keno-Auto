"""KenoAuto — runs by itself, logs every California Hot Spot draw, scores the engines.

    python keno_auto.py              # run forever (what Task Scheduler starts)
    python keno_auto.py --diagnose   # one pass, prints what it sees, saves debug files
    python keno_auto.py --report     # rebuild the report now
    python keno_auto.py --import FILE.json   # import draws from an old tracker report
"""
import argparse
import datetime as dt
import json
import logging
import os
import re
import socket
import sys
import time
import traceback

import kdb
import ledger

HERE = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(HERE, "debug")
LOG_DIR = os.path.join(HERE, "logs")
STATUS = os.path.join(HERE, "data", "status.json")
log = logging.getLogger("keno")


def setup_logging(console=True):
    os.makedirs(LOG_DIR, exist_ok=True)
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(os.path.join(LOG_DIR, f"keno-{dt.date.today():%Y-%m-%d}.log"), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers = [fh]
    if console and sys.stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        log.addHandler(sh)


def load_cfg():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    # GitHub Actions (Linux) uses the bundled Chromium instead of Edge
    if "KENO_BROWSER_CHANNEL" in os.environ:
        cfg["browser_channel"] = os.environ["KENO_BROWSER_CHANNEL"]
    return cfg


def save_cfg(cfg):
    with open(os.path.join(HERE, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def write_status(**kw):
    os.makedirs(os.path.dirname(STATUS), exist_ok=True)
    kw["updated"] = kdb.now_str()
    with open(STATUS, "w", encoding="utf-8") as f:
        json.dump(kw, f, indent=2)


def dump_debug(name, text):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(os.path.join(DEBUG_DIR, name), "w", encoding="utf-8") as f:
        f.write(text if isinstance(text, str) else json.dumps(text, indent=2, default=str))


# ------------------------------------------------------------------ parsing CA Lottery JSON
def _num(x):
    try:
        return int(str(x).strip())
    except Exception:
        return None


def parse_draw_obj(d):
    """Turn one CA Lottery draw object into (id, numbers, bullseye, date) or None."""
    if not isinstance(d, dict):
        return None
    did = _num(d.get("DrawNumber") or d.get("drawNumber") or d.get("DrawNo"))
    wn = d.get("WinningNumbers") or d.get("winningNumbers")
    if did is None or not wn:
        return None
    items = list(wn.values()) if isinstance(wn, dict) else list(wn)
    nums, specials = [], []
    for it in items:
        if isinstance(it, dict):
            n = _num(it.get("Number") or it.get("number"))
            if n is None:
                continue
            if it.get("IsSpecial") or it.get("isSpecial"):
                specials.append(n)
            else:
                nums.append(n)
        else:
            n = _num(it)
            if n is not None:
                nums.append(n)
    be = None
    for k, v in d.items():
        if re.search(r"bull", k, re.I):
            be = _num(v if not isinstance(v, dict) else v.get("Number"))
    if specials and be is None:
        be = specials[0]
    if len(nums) == 19 and specials:  # bulls-eye stored separately but part of the 20
        nums = nums + specials[:1]
    if len(nums) == 21 and be is None:
        be = nums.pop()
    nums = sorted(set(nums))
    if len(nums) != 20 or not all(1 <= n <= 80 for n in nums):
        return None
    if be is not None and be not in nums:
        be = None
    date = d.get("DrawDate") or d.get("drawDate")
    return did, nums, be, (str(date)[:10] if date else None)


def find_draws(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        p = parse_draw_obj(obj)
        if p:
            out.append(p)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                find_draws(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_draws(v, out)
    return out


# ------------------------------------------------------------------ browser side
PAGE_JS = r"""
() => {
  // Works on both the Hot Spot page and past-winning-numbers?query=NNNNNNN.
  // CA Lottery renders the 20 numbers twice: a screen-reader list (19 numbers + "Bulls-eye number is NN")
  // and a visual 80-cell grid with the drawn ones marked blue-num.
  const txt = document.body ? document.body.innerText : '';
  const el = document.querySelector('.current-drawNumber');
  let draw = el ? el.textContent.trim() : null;
  if (!draw) { const m = txt.match(/Draw\s*(?:Number|#)\s*:?\s*([0-9]{7})/i); draw = m ? m[1] : null; }
  const dm = txt.match(/Draw Date:?\s*([A-Za-z]{3,9}\.? \d{1,2},? \d{4})/i);
  const tm = txt.match(/Draw Time:?\s*([0-9]{1,2}:[0-9]{2}\s*[ap]\.?m\.?)/i) || txt.match(/at\s+([0-9]{1,2}:[0-9]{2}\s*[ap]\.?m\.?)/i);
  let best = null;
  for (const ul of document.querySelectorAll('ul')) {
    const nums = []; let be = null;
    for (const li of ul.querySelectorAll(':scope > li')) {
      const t = li.textContent.replace(/\s+/g, ' ').trim();
      let m = t.match(/^(\d{1,2})$/);
      if (m) { nums.push(+m[1]); continue; }
      m = t.match(/Bulls-?eye[^0-9]*(\d{1,2})\s*$/i);
      if (m) { be = +m[1]; nums.push(be); }
    }
    if (nums.length === 20 && (!best || (be && !best.be))) best = {nums, be};
  }
  if (!best) {  // fallback: the blue cells of the visual grid
    const blue = Array.from(document.querySelectorAll('li.blue-num')).map(li => +li.textContent.trim());
    if (blue.length === 20) {
      const b = txt.match(/Bulls-?eye number is[^0-9]{0,20}(\d{1,2})/i);
      best = {nums: blue, be: b ? +b[1] : null};
    }
  }
  return {draw, date: dm ? dm[1] + (tm ? ' ' + tm[1] : '') : null,
          nums: best ? best.nums : null, bullseye: best ? best.be : null};
}
"""


class Collector:
    def __init__(self, cfg, con):
        self.cfg, self.con = cfg, con
        self.pw = self.browser = self.page = None
        self.api_hits = {}
        self.last_reload = 0
        self.active = False  # True while we call the API ourselves (don't double-store)
        if not cfg.get("hotspot_game_id"):
            cfg["hotspot_game_id"] = kdb.meta_get(con, "hotspot_game_id")

    # -- lifecycle
    def start(self):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        kw = {"headless": self.cfg.get("headless", True)}
        try:
            self.browser = self.pw.chromium.launch(channel=self.cfg.get("browser_channel") or None, **kw)
        except Exception as e:
            log.warning("could not start %s (%s) — using bundled Chromium", self.cfg.get("browser_channel"), e)
            self.browser = self.pw.chromium.launch(**kw)
        ctx = self.browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"),
            locale="en-US", timezone_id="America/Los_Angeles", viewport={"width": 1280, "height": 900})
        self.page = ctx.new_page()
        self.page.on("response", self._on_response)
        self.open_page()

    def stop(self):
        for x in (self.browser, self.pw):
            try:
                x and (x.close() if x is self.browser else x.stop())
            except Exception:
                pass

    def open_page(self):
        self.page.goto(self.cfg["page_url"], wait_until="domcontentloaded", timeout=90000)
        try:
            self.page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass
        self.last_reload = time.time()

    # -- passive: any JSON the page itself loads
    def _on_response(self, resp):
        try:
            if "/api/" not in resp.url or "json" not in (resp.headers.get("content-type") or ""):
                return
            data = resp.json()
            draws = find_draws(data)
            self.api_hits[resp.url] = len(draws)
            if draws and not self.active:
                self.store(draws, "api")
        except Exception:
            pass

    # -- active: call the CA Lottery API from inside the page (same origin, same cookies)
    def api(self, game_id, page_no, size):
        self.active = True
        url = f"{self.cfg['api_base']}/{game_id}/{page_no}/{size}"
        return self.page.evaluate(
            """async (u) => { try { const r = await fetch(u, {credentials:'same-origin', headers:{Accept:'application/json'}});
                 return r.ok ? await r.json() : {__status: r.status}; } catch (e) { return {__error: String(e)}; } }""", url)

    def discover_game_id(self):
        found = {}
        for gid in range(1, 41):
            data = self.api(gid, 1, 5)
            draws = find_draws(data)
            found[gid] = len(draws)
            if draws:
                log.info("Hot Spot API found: game id %d (%d draws)", gid, len(draws))
                self.active = False
                self.cfg["hotspot_game_id"] = gid
                kdb.meta_set(self.con, "hotspot_game_id", gid)
                try:
                    save_cfg(self.cfg)
                except OSError:
                    pass
                return gid
        self.active = False
        kdb.meta_set(self.con, "hotspot_game_id", -1)  # remember "no API" so we don't probe every start
        self.cfg["hotspot_game_id"] = -1
        dump_debug("api_probe.json", found)
        log.warning("Hot Spot API id not found — falling back to reading the page")
        return None

    def poll_api(self):
        gid = self.cfg.get("hotspot_game_id")
        if not gid or gid < 0:
            return None
        known = kdb.latest_draw_id(self.con) or 0
        got = []
        for p in range(1, self.cfg.get("backfill_max_pages", 20) + 1):
            data = self.api(gid, p, self.cfg.get("api_page_size", 50))
            draws = find_draws(data)
            if not draws:
                if p == 1:
                    self.active = False
                    dump_debug("api_last_bad.json", data)
                    return None
                break
            got += draws
            if min(d[0] for d in draws) <= known:
                break
        self.active = False
        return self.store(got, "api")

    def read_page(self, page=None):
        page = page or self.page
        r = page.evaluate(PAGE_JS)
        if not r.get("draw") or not r.get("nums") or len(set(r["nums"])) != 20:
            return None, r
        be = r.get("bullseye")
        if be is not None and be not in r["nums"]:
            be = None
        return (int(r["draw"]), sorted(r["nums"]), be, r.get("date")), r

    def poll_page(self):
        if time.time() - self.last_reload > 50:
            self.open_page()
        d, raw = self.read_page()
        if not d:
            dump_debug("page_last_fail.html", self.page.content())
            dump_debug("page_last_fail.json", raw)
            return None
        got = self.backfill(d[0]) + [d]
        return self.store(got, "page")

    def backfill(self, current):
        """Fetch draws we missed (PC asleep, or GitHub's 30-min gap) one by one from
        past-winning-numbers?query=NNNNNNN, oldest first."""
        live_max = self.con.execute("SELECT MAX(draw_id) FROM draws WHERE source!='import'").fetchone()[0]
        cap = self.cfg.get("backfill_max_draws", 150)
        start = (live_max + 1) if live_max else current - self.cfg.get("first_run_backfill", 60)
        start = max(start, current - cap)
        ids = list(range(start, current))
        if not ids:
            return []
        out, fails = [], 0
        bp = self.page.context.new_page()
        bp.route("**/*", lambda r: r.abort() if r.request.resource_type in ("image", "media", "font", "stylesheet", "script") else r.continue_())
        try:
            for did in ids:
                url = f"{self.cfg['past_url']}?query={did}"
                try:
                    bp.goto(url, wait_until="domcontentloaded", timeout=45000)
                    d, raw = self.read_page(bp)
                    if not d and raw.get("nums") and len(set(raw["nums"])) == 20 and str(did) in bp.inner_text("body"):
                        d = (did, sorted(raw["nums"]), raw.get("bullseye"), raw.get("date"))
                    if d and d[0] != did and str(did) in bp.inner_text("body"):
                        d = (did,) + d[1:]  # page header shows the latest draw; the body shows the one we asked for
                    if d and d[0] == did:
                        out.append(d)
                        continue
                    fails += 1
                    if fails == 1:
                        dump_debug("past_last_fail.html", bp.content())
                        dump_debug("past_last_fail.json", raw)
                except Exception as e:
                    fails += 1
                    log.warning("backfill %s failed: %s", did, e)
                if fails >= 5 and not out:
                    log.warning("backfill not working — giving up this round")
                    break
        finally:
            bp.close()
        if out:
            log.info("backfilled %d of %d missed draw(s)", len(out), len(ids))
        return out

    def store(self, draws, source):
        prev_max = kdb.latest_draw_id(self.con) or 0
        new = []
        for did, nums, be, date in draws:
            try:
                if kdb.insert_draw(self.con, did, nums, be, date, source):
                    new.append(did)
            except ValueError as e:
                log.warning("%s", e)
        if new:
            log.info("logged %d new draw(s): %s", len(new), ", ".join(map(str, sorted(new)[-5:])))
            ledger.on_new_draws(self.con, self.cfg, new, prev_max=prev_max)
        return new

    def poll(self):
        if time.time() - self.last_reload > self.cfg.get("page_reload_minutes", 30) * 60:
            self.open_page()
        res = self.poll_api()
        if res is None:
            res = self.poll_page()
        return res


# ------------------------------------------------------------------ modes
def single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47653))
        return s
    except OSError:
        return None


def run_forever():
    lock = single_instance()
    if not lock:
        log.info("already running — exit")
        return
    cfg = load_cfg()
    con = kdb.connect()
    import report
    last_report = 0
    log_day = dt.date.today()
    backoff = 30
    while True:
        col = Collector(cfg, con)
        try:
            col.start()
            if cfg.get("hotspot_game_id") is None:
                col.discover_game_id()
            log.info("collector started")
            backoff = 30
            while True:
                col.poll()
                n = con.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
                write_status(state="running", draws_logged=n, latest_draw=kdb.latest_draw_id(con),
                             mode="api" if (cfg.get("hotspot_game_id") or 0) > 0 else "page")
                if time.time() - last_report > cfg.get("report_every_minutes", 60) * 60:
                    report.build(con, cfg)
                    last_report = time.time()
                if dt.date.today() != log_day:  # new log file each day
                    log_day = dt.date.today()
                    setup_logging(console=False)
                    report.build(con, cfg, archive_day=log_day - dt.timedelta(days=1))
                time.sleep(cfg.get("poll_seconds", 60))
        except KeyboardInterrupt:
            break
        except Exception:
            log.error("collector crashed:\n%s", traceback.format_exc())
            write_status(state="error", error=traceback.format_exc()[-1500:])
        finally:
            col.stop()
        time.sleep(backoff)
        backoff = min(backoff * 2, 900)


def diagnose():
    cfg = load_cfg()
    con = kdb.connect()
    col = Collector(cfg, con)
    try:
        col.start()
        print("Page title:", col.page.title())
        gid = cfg.get("hotspot_game_id") or col.discover_game_id()
        print("API game id:", gid)
        new = col.poll()
        print("New draws this pass:", new)
        print("Latest draw in DB:", kdb.latest_draw_id(con))
        print("API URLs the page loaded:", json.dumps(col.api_hits, indent=1)[:2000])
        dump_debug("diagnose_page.html", col.page.content())
        dump_debug("diagnose_api_urls.json", col.api_hits)
        col.page.screenshot(path=os.path.join(DEBUG_DIR, "diagnose.png"), full_page=True)
        print("Debug files saved in", DEBUG_DIR)
    finally:
        col.stop()


def export_csv(con, days=3):
    """data/draws/YYYY-MM-DD.csv — plain-text copy of every draw, one file per day."""
    out = os.path.join(HERE, "data", "draws")
    os.makedirs(out, exist_ok=True)
    dates = [r[0] for r in con.execute(
        "SELECT DISTINCT substr(captured_at,1,10) FROM draws WHERE source!='import' ORDER BY 1 DESC LIMIT ?", (days,))]
    for d in dates:
        rows = con.execute("SELECT draw_id, draw_date, numbers, bullseye, captured_at, source FROM draws "
                           "WHERE source!='import' AND substr(captured_at,1,10)=? ORDER BY draw_id", (d,)).fetchall()
        with open(os.path.join(out, f"{d}.csv"), "w", encoding="utf-8") as f:
            f.write("draw_id,draw_date,numbers,bullseye,captured_at,source\n")
            for did, dd, nums, be, cap, src in rows:
                f.write(f"{did},{dd or ''},{' '.join(map(str, json.loads(nums)))},{be or ''},{cap},{src}\n")


def run_once():
    """One batch pass (GitHub Actions): fetch + backfill, score, report, exit."""
    cfg = load_cfg()
    con = kdb.connect()
    import report
    before = kdb.latest_draw_id(con) or 0
    count_before = con.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
    col = Collector(cfg, con)
    ok = False
    try:
        col.start()
        if cfg.get("hotspot_game_id") is None:
            col.discover_game_id()
        col.poll()
        ok = True
    except Exception:
        log.error("run failed:\n%s", traceback.format_exc())
        try:
            dump_debug("once_fail.html", col.page.content())
            col.page.screenshot(path=os.path.join(DEBUG_DIR, "once_fail.png"), full_page=True)
        except Exception:
            pass
    finally:
        col.stop()
    after = kdb.latest_draw_id(con) or 0
    n = con.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
    write_status(state="ok" if ok and after > before else ("no-new-draws" if ok else "error"),
                 draws_logged=n, latest_draw=after, new_this_run=n - count_before,
                 mode="api" if (cfg.get("hotspot_game_id") or 0) > 0 else "page", runner="github")
    export_csv(con)
    report.build(con, cfg, archive_day=dt.date.today())
    log.info("batch done: latest %s (was %s), %d draws total", after, before, n)
    return 0 if ok else 1


def import_report(path):
    con = kdb.connect()
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    n = 0
    # The old screen-reader tracker mostly saved counters like "LIVE495" instead of real draw
    # numbers, so imported draws get small sequential ids (1, 2, 3 …) in log order. They sort
    # before every real draw number and only serve as history for the engines.
    for i, x in enumerate(d.get("log", {}).get("draws", []), 1):
        try:
            n += kdb.insert_draw(con, i, x["numbers"], x.get("bullseye"), None, "import")
        except (ValueError, KeyError):
            pass
    print(f"imported {n} draws (history only — they are never scored)")
    ledger.maybe_retrain(con, load_cfg(), upto=kdb.latest_draw_id(con) or 0, force=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--import", dest="imp")
    ap.add_argument("--once", action="store_true", help="one batch pass, for GitHub Actions")
    a = ap.parse_args()
    setup_logging(console=True)
    if a.diagnose:
        diagnose()
    elif a.report:
        import report
        print(report.build(kdb.connect(), load_cfg()))
    elif a.once:
        sys.exit(run_once())
    elif a.imp:
        import_report(a.imp)
    else:
        run_forever()
