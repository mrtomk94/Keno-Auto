"""Engine lab: 10 versions of each engine family (v1 = the current engine, v2–v10 = different settings).
Every version gets a locked ticket for each upcoming draw and is scored afterwards, exactly like the main engines.
RANDOM v1–v10 are 10 independent random tickets: the spread between them shows how big differences look by pure luck.
Scored on the 10-spot ticket (and the first 5 numbers as a 5-spot ticket)."""
import json
import math

import numpy as np

import engines as E
import kdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_pred (target_draw INTEGER, family TEXT, v INTEGER, ranked TEXT, basis_draw INTEGER,
  created_at TEXT, kind TEXT, PRIMARY KEY(target_draw, family, v));
CREATE TABLE IF NOT EXISTS lab_res (target_draw INTEGER, family TEXT, v INTEGER, hits10 INTEGER, hits5 INTEGER,
  be INTEGER, valid INTEGER, PRIMARY KEY(target_draw, family, v));
"""


def _top(scores):
    return E.rank(scores, 10)


def _gap_k(X, k):
    """Draws since the number's k-th most recent appearance (k=1 = plain gap)."""
    T = X.shape[0]
    out = np.full(80, float(T))
    for n in range(80):
        idx = np.nonzero(X[:, n])[0]
        if len(idx) >= k:
            out[n] = T - 1 - idx[-k]
    return out


def _heat(X, short):
    return X[-short:].sum(0) + 0.5 * X[-30:].sum(0) / 5.0


def _cluster(X, short):
    h = _heat(X, short)
    chosen = [E.rank(h, 1)[0]]
    while len(chosen) < 10:
        frontier = {m for c in chosen for m in E.neighbors(c)} - set(chosen)
        chosen.append(max(frontier, key=lambda m: (h[m - 1], E._rng.random())))
    return chosen


def _block(X, short, rows, cols):
    h = _heat(X, short)
    best, best_s = None, -1
    for r in range(E.ROWS - rows + 1):
        for c in range(E.COLS - cols + 1):
            cells = [(r + i) * E.COLS + c + j + 1 for i in range(rows) for j in range(cols)]
            s = sum(h[m - 1] for m in cells)
            if s > best_s:
                best, best_s = cells, s
    return sorted(best, key=lambda m: (-h[m - 1], E._rng.random()))


def _markov(X, look):
    Xw = X[-look:]
    if Xw.shape[0] < 30:
        return E.e_random(X, {})
    A, B = Xw[:-1], Xw[1:]
    tr = (A.T @ B) / np.maximum(A.sum(0)[:, None], 1)
    return _top(tr[X[-1].astype(bool)].sum(0))


def _deviation(X, wins):
    score = np.zeros(80)
    for w, wt in wins:
        n = min(w, X.shape[0])
        c = X[-n:].sum(0)
        z = (c - n * 0.25) / math.sqrt(n * 0.1875) * n / (n + 30)
        score += wt * z
    return _top(score)


def _zone(X, window, pick):
    heat = np.array([X[-window:].sum(0)[z].sum() + E._rng.random() * 1e-3 for z in E.ZONES])
    zi = int(np.argmax(heat)) if pick == "hot" else int(np.argmin(heat))
    return E._rank_in(E.ZONES[zi], X)


def _zone_block(X, window):
    heat = np.array([X[-window:].sum(0)[z].sum() + E._rng.random() * 1e-3 for z in E.ZONES])
    z = E.ZONES[int(np.argmax(heat))]
    h = X[-window:].sum(0)
    blocks = [z[i * 5:(i + 2) * 5] for i in range(3)]
    return E._rank_in(max(blocks, key=lambda b: h[b].sum() + E._rng.random() * 1e-3), X)


W10 = [10, 1, 3, 5, 15, 20, 30, 50, 100, 200]
FAMILIES = {
    "HOT":       ("cửa sổ (kỳ)", [30, 5, 10, 15, 20, 40, 60, 90, 150, 300], lambda X, p: _top(X[-p:].sum(0))),
    "GAP":       ("lâu chưa ra lần thứ k", list(range(1, 11)), lambda X, p: _top(_gap_k(X, p))),
    "CLUSTER":   ("cửa sổ nhiệt ngắn", [6, 2, 3, 4, 8, 10, 12, 15, 20, 30], lambda X, p: _cluster(X, p)),
    "BLOCK":     ("khối · cửa sổ", ["2×5·6", "2×5·3", "2×5·12", "2×5·30", "1×10·6", "1×10·30", "5×2·6", "5×2·30", "2×5·1", "5×2·1"],
                  lambda X, p: _block(X, int(p.split("·")[1]), *[int(x) for x in p.split("·")[0].split("×")])),
    "MARKOV":    ("số kỳ để học", [2000, 50, 100, 200, 300, 500, 750, 1000, 1500, 1250], lambda X, p: _markov(X, p)),
    "DEVIATION": ("khung z-score", ["30/90/all", "10", "20", "30", "50", "90", "150", "300", "600", "all"],
                  lambda X, p: _deviation(X, [(30, .5), (90, .3), (10 ** 6, .2)] if p == "30/90/all"
                                          else [(10 ** 6 if p == "all" else int(p), 1.0)])),
    "Z-HOT":     ("cửa sổ vùng", W10, lambda X, p: _zone(X, p, "hot")),
    "Z-COLD":    ("cửa sổ vùng", W10, lambda X, p: _zone(X, p, "cold")),
    "Z-BLOCK":   ("cửa sổ vùng", W10, lambda X, p: _zone_block(X, p)),
    "RANDOM":    ("vé ngẫu nhiên độc lập", list(range(1, 11)), lambda X, p: E.e_random(X, {})),
}


def ensure(con):
    con.executescript(SCHEMA)


def predict(hist):
    X = E.to_matrix(hist[-2000:])
    out = {}
    for fam, (_, params, fn) in FAMILIES.items():
        for v, p in enumerate(params, 1):
            try:
                r = [int(n) for n in fn(X, p)][:10]
                assert len(set(r)) == 10
            except Exception:
                r = E.e_random(X, {})
            out[(fam, v)] = r
    return out


def _lock(con, basis, targets, kind):
    hist = kdb.history(con, upto_draw=basis, limit=2000)
    if len(hist) < 300 or hist[-1][0] != basis or not targets:
        return 0
    picks = predict(hist)
    ts = kdb.now_str()
    con.executemany("INSERT OR IGNORE INTO lab_pred VALUES(?,?,?,?,?,?,?)",
                    [(t, f, v, json.dumps(r), basis, ts, kind) for t in targets for (f, v), r in picks.items()])
    con.commit()
    return len(targets)


def settle(con, draw_id):
    row = con.execute("SELECT numbers, bullseye, captured_at FROM draws WHERE draw_id=?", (draw_id,)).fetchone()
    if not row:
        return
    nums, be, cap = set(json.loads(row[0])), row[1], row[2]
    out = []
    for f, v, ranked, basis, created, kind in con.execute(
            "SELECT family, v, ranked, basis_draw, created_at, kind FROM lab_pred WHERE target_draw=?", (draw_id,)):
        r = json.loads(ranked)
        valid = int(basis < draw_id and (kind == "walkforward" or created < cap))
        out.append((draw_id, f, v, len(nums & set(r)), len(nums & set(r[:5])), int(be in r if be else 0), valid))
    con.executemany("INSERT OR REPLACE INTO lab_res VALUES(?,?,?,?,?,?,?)", out)
    con.commit()


def on_new_draws(con, cfg, new_ids, prev_max):
    if not cfg.get("lab", True):
        return
    ensure(con)
    for d in sorted(new_ids):
        if prev_max and d > prev_max and not con.execute("SELECT 1 FROM lab_pred WHERE target_draw=? LIMIT 1", (d,)).fetchone():
            _lock(con, d - 1, [d], "walkforward")
        settle(con, d)
    latest = kdb.latest_draw_id(con)
    if latest:
        horizon = cfg.get("lock_horizon", 12)
        todo = [t for t in range(latest + 1, latest + horizon + 1)
                if not con.execute("SELECT 1 FROM lab_pred WHERE target_draw=? LIMIT 1", (t,)).fetchone()]
        _lock(con, latest, todo, "live")


def table(con, since=None):
    """{family: [(v, param, n, avg10, z10, rate5plus)]}"""
    ensure(con)
    sd = math.sqrt(10 * 0.25 * 0.75 * 70 / 79)
    q = "SELECT family, v, COUNT(*), AVG(hits10), AVG(hits10>=5) FROM lab_res WHERE valid=1"
    args = []
    if since:
        q += " AND target_draw>=?"
        args.append(since)
    q += " GROUP BY family, v"
    out = {}
    for f, v, n, a, r5 in con.execute(q, args):
        if f not in FAMILIES:
            continue
        out.setdefault(f, []).append((v, FAMILIES[f][1][v - 1], n, a, (a - 2.5) / (sd / math.sqrt(n)), r5))
    for f in out:
        out[f].sort()
    return out


def paired_vs_v1(con, family, v):
    """Mean difference in hits (vN − v1) on the same draws, with its z."""
    rows = con.execute("""SELECT a.hits10 - b.hits10 FROM lab_res a JOIN lab_res b ON a.target_draw=b.target_draw
                          AND b.family=a.family AND b.v=1 AND b.valid=1 WHERE a.family=? AND a.v=? AND a.valid=1""",
                       (family, v)).fetchall()
    d = np.array([r[0] for r in rows], float)
    if len(d) < 30:
        return None
    return d.mean(), d.mean() / (d.std(ddof=1) / math.sqrt(len(d)) + 1e-12), len(d)
