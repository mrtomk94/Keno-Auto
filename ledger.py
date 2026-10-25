"""Lock tickets before a draw, settle them after. This is the honest bookkeeping layer.

Two kinds of tickets, both strictly out-of-sample (an engine never sees the draw it is scored on):
  live         — locked in real time, before the draw happened (PC version, every 60 s)
  walkforward  — the draw arrived in a batch (GitHub version, every 30 min). The ticket is built
                 from draws < target only, with a PATTERN model trained only on draws <= basis.
"""
import json
import logging

import engines
import kdb

log = logging.getLogger("keno")


def _ensure_schema(con):
    cols = [r[1] for r in con.execute("PRAGMA table_info(predictions)")]
    if "kind" not in cols:
        con.execute("ALTER TABLE predictions ADD COLUMN kind TEXT DEFAULT 'live'")
        con.commit()


def load_ctx(con, cfg, basis):
    model = kdb.meta_get(con, "pattern_model")
    if model and model.get("upto", 0) > basis:
        model = None  # trained on draws after the basis -> not allowed
    return {"hot_window": cfg.get("hot_window", 30), "pattern_model": model}


def maybe_retrain(con, cfg, upto, force=False):
    """(Re)train PATTERN on draws <= upto, every N draws."""
    every = cfg.get("pattern_retrain_every", 100)
    model = kdb.meta_get(con, "pattern_model")
    n = con.execute("SELECT COUNT(*) FROM draws WHERE draw_id<=?", (upto,)).fetchone()[0]
    if not force and model and model.get("upto", 0) <= upto and n - model.get("at_count", 0) < every:
        return model
    hist = kdb.history(con, upto_draw=upto, limit=2000)
    m = engines.train_pattern(engines.to_matrix(hist))
    if m:
        m.update(at_count=n, upto=upto, trained_at=kdb.now_str())
        kdb.meta_set(con, "pattern_model", m)
        log.info("PATTERN retrained on %d draws: %s", m["trained_on"],
                 ", ".join(f"{f}={w:+.3f}" for f, w in zip(engines.FEATURES, m["w"])))
    return m


def lock_predictions(con, cfg, basis_draw, kind="live"):
    """Create tickets for draw basis_draw+1 using only draws <= basis_draw."""
    _ensure_schema(con)
    target = basis_draw + 1
    if con.execute("SELECT 1 FROM predictions WHERE target_draw=?", (target,)).fetchone():
        return False
    if kind == "live" and con.execute("SELECT 1 FROM draws WHERE draw_id=?", (target,)).fetchone():
        return False  # target already known -> can't be a live ticket
    hist = kdb.history(con, upto_draw=basis_draw, limit=2000)
    if len(hist) < 30 or hist[-1][0] != basis_draw:
        return False
    picks = engines.predict_all(hist, load_ctx(con, cfg, basis_draw))
    ts = kdb.now_str()
    con.executemany(
        "INSERT OR IGNORE INTO predictions(target_draw,basis_draw,engine,ranked,created_at,kind) VALUES(?,?,?,?,?,?)",
        [(target, basis_draw, e, json.dumps(r), ts, kind) for e, r in picks.items()])
    con.commit()
    return True


def settle(con, cfg, draw_id):
    _ensure_schema(con)
    row = con.execute("SELECT numbers, bullseye, captured_at FROM draws WHERE draw_id=?", (draw_id,)).fetchone()
    if not row:
        return 0
    nums, be, captured = set(json.loads(row[0])), row[1], row[2]
    preds = con.execute("SELECT engine, ranked, created_at, basis_draw, kind FROM predictions WHERE target_draw=?",
                        (draw_id,)).fetchall()
    out = []
    for engine, ranked, created, basis, kind in preds:
        ranked = json.loads(ranked)
        valid = int(basis < draw_id and (kind == "walkforward" or created < captured))
        for spot in cfg.get("spots", [5, 6, 7, 8, 9, 10]):
            pick = ranked[:spot]
            hits = len(nums.intersection(pick))
            be_hit = int(be is not None and be in pick)
            hs, bp = kdb.payout(spot, hits, be_hit)
            out.append((draw_id, engine, spot, hits, be_hit, hs, bp, valid))
    con.executemany("INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?,?,?)", out)
    con.commit()
    return len(preds)


def on_new_draws(con, cfg, new_ids, prev_max=None):
    """Settle new draws in order; draws that had no live ticket get a walk-forward ticket first."""
    _ensure_schema(con)
    for d in sorted(new_ids):
        if prev_max is not None and d > prev_max:
            if not con.execute("SELECT 1 FROM predictions WHERE target_draw=?", (d,)).fetchone():
                maybe_retrain(con, cfg, upto=d - 1)
                lock_predictions(con, cfg, d - 1, kind="walkforward")
        settle(con, cfg, d)
    latest = kdb.latest_draw_id(con)
    if latest:
        maybe_retrain(con, cfg, upto=latest)
        lock_predictions(con, cfg, latest, kind="live")
