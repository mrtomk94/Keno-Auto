"""SQLite storage for KenoAuto: draws, locked predictions, settlements."""
import json
import os
import sqlite3
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "keno.db")

# California Hot Spot pay tables (prize per $1), copied from your tracker v23 report.
PAY_HOTSPOT = {
    1: {1: 2}, 2: {2: 10}, 3: {2: 2, 3: 25}, 4: {2: 1, 3: 4, 4: 78},
    5: {3: 2, 4: 15, 5: 435}, 6: {3: 1, 4: 6, 5: 67, 6: 900},
    7: {3: 1, 4: 3, 5: 12, 6: 190, 7: 2000},
    8: {0: 1, 5: 12, 6: 80, 7: 550, 8: 10000},
    9: {0: 1, 5: 6, 6: 30, 7: 135, 8: 2750, 9: 30000},
    10: {0: 2, 5: 3, 6: 17, 7: 42, 8: 575, 9: 5000, 10: 100000},
}
PAY_BULLSEYE = {
    1: {1: 12}, 2: {1: 1, 2: 46}, 3: {1: 1, 2: 7, 3: 110}, 4: {1: 2, 2: 3, 3: 30, 4: 350},
    5: {1: 3, 2: 6, 3: 16, 4: 80, 5: 1000}, 6: {1: 5, 2: 2, 3: 7, 4: 40, 5: 250, 6: 2000},
    7: {1: 5, 2: 2, 3: 3, 4: 20, 5: 68, 6: 575, 7: 10000},
    8: {1: 5, 2: 2, 3: 2, 4: 7, 5: 50, 6: 200, 7: 1300, 8: 30000},
    9: {1: 5, 2: 2, 3: 2, 4: 5, 5: 20, 6: 90, 7: 475, 8: 6000, 9: 65000},
    10: {1: 5, 2: 2, 3: 2, 4: 2, 5: 9, 6: 60, 7: 215, 8: 1400, 9: 15000, 10: 300000},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS draws (
  draw_id     INTEGER PRIMARY KEY,
  draw_date   TEXT,              -- date shown by CA Lottery (may be NULL for imported)
  numbers     TEXT NOT NULL,     -- JSON list of 20 ints, sorted
  bullseye    INTEGER,
  captured_at TEXT NOT NULL,     -- local time we first saw it
  source      TEXT NOT NULL      -- api | page | import
);
CREATE TABLE IF NOT EXISTS predictions (
  target_draw INTEGER NOT NULL,  -- the draw this ticket is FOR
  basis_draw  INTEGER NOT NULL,  -- last draw the engine was allowed to see
  engine      TEXT NOT NULL,
  ranked      TEXT NOT NULL,     -- JSON list of 10 ints; spot k = first k
  created_at  TEXT NOT NULL,
  kind        TEXT DEFAULT 'live', -- live | walkforward
  PRIMARY KEY (target_draw, engine)
);
CREATE TABLE IF NOT EXISTS results (
  target_draw INTEGER NOT NULL,
  engine      TEXT NOT NULL,
  spot        INTEGER NOT NULL,
  hits        INTEGER NOT NULL,
  bullseye_hit INTEGER NOT NULL,
  pay_hotspot REAL NOT NULL,     -- prize on a $1 Hot Spot ticket
  pay_bullseye REAL NOT NULL,    -- prize on the $1 Bulls-Eye add-on
  valid       INTEGER NOT NULL,  -- 1 = ticket provably locked before the draw was seen
  PRIMARY KEY (target_draw, engine, spot)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE INDEX IF NOT EXISTS ix_results_engine ON results(engine, spot);
"""


def now_str():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.executescript(SCHEMA)
    return con


def meta_get(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def meta_set(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, json.dumps(v)))
    con.commit()


def insert_draw(con, draw_id, numbers, bullseye, draw_date, source):
    """Returns True if the draw is new."""
    nums = sorted(int(n) for n in numbers)
    if len(nums) != 20 or len(set(nums)) != 20 or not all(1 <= n <= 80 for n in nums):
        raise ValueError(f"bad draw {draw_id}: {numbers}")
    cur = con.execute(
        "INSERT OR IGNORE INTO draws(draw_id,draw_date,numbers,bullseye,captured_at,source) VALUES(?,?,?,?,?,?)",
        (int(draw_id), draw_date, json.dumps(nums), bullseye, now_str(), source),
    )
    con.commit()
    return cur.rowcount == 1


def history(con, upto_draw=None, limit=None):
    """List of (draw_id, set(numbers), bullseye) oldest->newest."""
    q = "SELECT draw_id, numbers, bullseye FROM draws"
    args = []
    if upto_draw is not None:
        q += " WHERE draw_id <= ?"
        args.append(upto_draw)
    q += " ORDER BY draw_id DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = con.execute(q, args).fetchall()
    rows.reverse()
    return [(r[0], set(json.loads(r[1])), r[2]) for r in rows]


def latest_draw_id(con):
    r = con.execute("SELECT MAX(draw_id) FROM draws WHERE source!='import'").fetchone()
    if r and r[0]:
        return r[0]
    r = con.execute("SELECT MAX(draw_id) FROM draws").fetchone()
    return r[0] if r else None


def payout(spot, hits, bullseye_hit):
    hs = PAY_HOTSPOT.get(spot, {}).get(hits, 0)
    be = PAY_BULLSEYE.get(spot, {}).get(hits, 0) if bullseye_hit else 0
    return hs, be
