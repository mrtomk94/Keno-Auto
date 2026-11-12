"""Builds reports/latest.html — the honest scoreboard: every engine vs pure chance."""
import datetime as dt
import html
import json
import math
import os
import shutil
from html import escape as e_

import engines
import kdb

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "reports")
_REPO = os.environ.get("KENO_REPO", "")  # "owner/repo" — set by the workflow; links then point at the data branch
RAW = f"https://raw.githubusercontent.com/{_REPO}/data/" if _REPO else ""
DRAWS = f"https://github.com/{_REPO}/tree/data/draws" if _REPO else "draws/"
COLORS = {"RANDOM": "#8b98a3", "HOT": "#ff6b5b", "GAP": "#f5a524", "CLUSTER": "#3ddc97",
          "BLOCK": "#2196c9", "MARKOV": "#a78bfa", "PATTERN": "#f472b6", "DEVIATION": "#e5e7eb", "LEARNED": "#facc15",
          "Z-HOT": "#fb923c", "Z-COLD": "#38bdf8", "Z-LAST": "#c084fc", "Z-BLOCK": "#4ade80", "Z-MID": "#f9a8d4"}


def hyper(k, h):
    """P(h of my k numbers are among the 20 drawn from 80)."""
    return math.comb(k, h) * math.comb(80 - k, 20 - h) / math.comb(80, 20)


def theo_return(spot):
    return sum(hyper(spot, h) * p for h, p in kdb.PAY_HOTSPOT[spot].items())


def theo_return_be(spot):
    # Bulls-Eye pays when the bulls-eye ball is one of your hits: P = (h/20) * P(h)
    return sum(hyper(spot, h) * (h / 20) * p for h, p in kdb.PAY_BULLSEYE[spot].items())


def p_upper(z):
    return 0.5 * math.erfc(z / math.sqrt(2))


def chi2_p(x, df):
    """Upper-tail p via Wilson–Hilferty (accurate enough for df >= 10)."""
    if df <= 0:
        return 1.0
    z = ((x / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return p_upper(z)


def holm(pvals):
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    adj = [1.0] * len(pvals)
    m, run = len(pvals), 0.0
    for rank_, i in enumerate(order):
        run = max(run, min(1.0, (m - rank_) * pvals[i]))
        adj[i] = run
    return adj


def engine_table(con, alpha):
    rows = con.execute("""SELECT engine, spot, COUNT(*), AVG(hits), SUM(pay_hotspot), SUM(pay_bullseye),
                                 SUM(CASE WHEN hits=spot THEN 1 ELSE 0 END)
                          FROM results WHERE valid=1 GROUP BY engine, spot ORDER BY spot, engine""").fetchall()
    out = []
    for e, k, n, mean, phs, pbe, perfect in rows:
        exp = k / 4
        var = k * 0.25 * 0.75 * (80 - k) / 79
        z = (mean - exp) / math.sqrt(var / n) if n else 0
        out.append(dict(engine=e, spot=k, n=n, mean=mean, exp=exp, z=z, p=p_upper(z),
                        ret=phs / n, ret_be=pbe / n, theo=theo_return(k), theo_be=theo_return_be(k),
                        perfect=perfect, perfect_exp=n * hyper(k, k)))
    test = [r for r in out if r["engine"] != "RANDOM"]
    for r, a in zip(test, holm([r["p"] for r in test])):
        r["p_adj"] = a
    for r in out:
        r.setdefault("p_adj", None)
        if r["n"] < 500:
            r["verdict"], r["cls"] = "Chưa đủ dữ liệu", "muted"
        elif r["engine"] == "RANDOM":
            r["verdict"], r["cls"] = "Đối chứng", "muted"
        elif r["p_adj"] < alpha:
            r["verdict"], r["cls"] = "Vượt ngẫu nhiên sau hiệu chỉnh — cần xác nhận trên dữ liệu mới", "good"
        elif r["p"] < alpha:
            r["verdict"], r["cls"] = "Trông cao nhưng chưa có ý nghĩa (do thử nhiều engine)", "warn"
        else:
            r["verdict"], r["cls"] = "≈ Ngẫu nhiên", "flat"
    return out


def randomness(con):
    draws = kdb.history(con)
    real = [d for d in draws if d[0] > 100000]  # imported rows have small fake ids
    if len(real) >= 3000:
        draws = real
    T = len(draws)
    if T < 50:
        return None
    X = engines.to_matrix(draws)
    f = X.sum(0)
    e = T * 0.25
    chi = float(((f - e) ** 2 / e).sum())
    be = [b for _, _, b in draws if b]
    be_chi = be_p = None
    if len(be) >= 400:
        cnt = [0] * 81
        for b in be:
            cnt[b] += 1
        eb = len(be) / 80
        be_chi = sum((cnt[i] - eb) ** 2 / eb for i in range(1, 81))
        be_p = chi2_p(be_chi, 79)
    overlaps = [len(draws[i][1] & draws[i + 1][1]) for i in range(T - 1) if draws[i + 1][0] == draws[i][0] + 1]
    ov = sum(overlaps) / len(overlaps) if overlaps else None
    ov_sd = math.sqrt(20 * 0.25 * 0.75 * 60 / 79)
    ov_z = (ov - 5) / (ov_sd / math.sqrt(len(overlaps))) if overlaps else None
    hot = sorted(range(80), key=lambda i: -f[i])[:5]
    return dict(draws=T, chi=chi, chi_p=chi2_p(chi / 0.75, 79), be_n=len(be), be_chi=be_chi, be_p=be_p,
                overlap=ov, overlap_n=len(overlaps), overlap_z=ov_z,
                top=[(i + 1, int(f[i]), round(float(f[i]) / T * 100, 1)) for i in hot])


def daily(con):
    rows = con.execute("""SELECT substr(captured_at,1,10) d, COUNT(*), MIN(draw_id), MAX(draw_id)
                          FROM draws WHERE source IN ('page','api') GROUP BY d ORDER BY d DESC LIMIT 30""").fetchall()
    out = []
    for d, n, lo, hi in rows:
        eng = dict(con.execute("""SELECT r.engine, AVG(r.hits) FROM results r JOIN draws w ON w.draw_id=r.target_draw
                                   WHERE r.valid=1 AND r.spot=10 AND substr(w.captured_at,1,10)=? GROUP BY r.engine""",
                               (d,)).fetchall())
        rounds = con.execute("""SELECT COUNT(DISTINCT r.target_draw) FROM results r JOIN draws w ON w.draw_id=r.target_draw
                                WHERE r.valid=1 AND substr(w.captured_at,1,10)=?""", (d,)).fetchone()[0]
        out.append(dict(date=d, draws=n, span=hi - lo + 1, rounds=rounds, eng=eng))
    return out


def zchart(con):
    """Cumulative z-score at 10-spot per engine, sampled to <= 300 points."""
    data = con.execute("SELECT engine, hits FROM results WHERE valid=1 AND spot=10 ORDER BY target_draw").fetchall()
    series = {}
    for e, h in data:
        series.setdefault(e, []).append(h)
    sd = math.sqrt(10 * 0.25 * 0.75 * 70 / 79)
    W, H, P = 900, 260, 34
    if not series or max(len(v) for v in series.values()) < 20:
        return "<p class='muted'>Biểu đồ sẽ hiện khi có đủ 20 kỳ đã chấm.</p>"
    N = max(len(v) for v in series.values())
    lines, zmax = {}, 3.5
    for e, hs in series.items():
        pts, s = [], 0.0
        step = max(1, len(hs) // 300)
        for i, h in enumerate(hs, 1):
            s += h - 2.5
            if i % step == 0 or i == len(hs):
                z = s / (sd * math.sqrt(i))
                zmax = max(zmax, abs(z) + 0.3)
                pts.append((i, z))
        lines[e] = pts
    X = lambda i: P + (W - 2 * P) * i / N
    Y = lambda z: H / 2 - (H / 2 - 14) * z / zmax
    svg = [f"<svg viewBox='0 0 {W} {H}' class='chart' role='img' aria-label='z-score tích lũy'>"]
    for z, lab in ((1.96, "+1.96"), (-1.96, "−1.96"), (0, "0")):
        svg.append(f"<line x1='{P}' x2='{W-P}' y1='{Y(z):.1f}' y2='{Y(z):.1f}' class='{'zero' if z == 0 else 'band'}'/>"
                   f"<text x='4' y='{Y(z)+4:.1f}' class='ax'>{lab}</text>")
    for e, pts in lines.items():
        d = " ".join(f"{X(i):.1f},{Y(z):.1f}" for i, z in pts)
        svg.append(f"<polyline points='{d}' fill='none' stroke='{COLORS.get(e,'#fff')}' stroke-width='{2.2 if e=='RANDOM' else 1.4}'/>")
    svg.append(f"<text x='{W-P}' y='{H-4}' text-anchor='end' class='ax'>{N} kỳ</text></svg>")
    legend = "".join(f"<span><i style='background:{COLORS.get(e)}'></i>{e}</span>" for e in lines)
    return "".join(svg) + f"<div class='legend'>{legend}</div>"


def fmt_p(p):
    return "—" if p is None else ("<0.001" if p < 0.001 else f"{p:.3f}")



# ---------------------------------------------------------------- upcoming draws + prediction log
import re as _re
try:
    from zoneinfo import ZoneInfo
    _PT = ZoneInfo("America/Los_Angeles")
except Exception:  # no tz database: assume the machine runs on Pacific time
    _PT = None
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def draw_epoch(text):
    """'October 25, 2026 9:56 a.m.' / 'Oct 25, 2026 3:20 p.m.' -> epoch ms (Pacific), or None."""
    if not text:
        return None
    m = _re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\s+(\d{1,2}):(\d{2})\s*([ap])", text, _re.I)
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).lower())
    h = int(m.group(4)) % 12 + (12 if m.group(6).lower() == "p" else 0)
    t = dt.datetime(int(m.group(3)), mon, int(m.group(2)), h, int(m.group(5)), tzinfo=_PT)
    return int(t.timestamp() * 1000)


def suggested_engine(tab):
    cands = [r for r in tab if r["spot"] == 10 and r["engine"] != "RANDOM" and r["n"] >= 200]
    if not cands:
        return "HOT", None
    best = max(cands, key=lambda r: r["z"])
    return best["engine"], best


def upcoming(con):
    row = con.execute("SELECT draw_id, draw_date FROM draws WHERE source!='import' AND draw_date IS NOT NULL "
                      "ORDER BY draw_id DESC LIMIT 1").fetchone()
    base_id, base_ms = (row[0], draw_epoch(row[1])) if row else (None, None)
    rows = con.execute("""SELECT p.target_draw, p.basis_draw, p.engine, p.ranked, p.created_at FROM predictions p
                          WHERE p.target_draw NOT IN (SELECT draw_id FROM draws) ORDER BY p.target_draw""").fetchall()
    targets = {}
    for t, b, en, rk, created in rows:
        d = targets.setdefault(t, {"draw": t, "basis": b, "locked": created, "picks": {},
                                   "eta": (base_ms + (t - base_id) * 240000) if base_ms else None})
        d["picks"][en] = json.loads(rk)
    fb = None
    last = con.execute("SELECT MAX(basis_draw) FROM predictions WHERE kind='live'").fetchone()[0]
    if last:
        picks = {en: json.loads(rk) for en, rk in con.execute(
            "SELECT engine, ranked FROM predictions WHERE basis_draw=? AND kind='live' GROUP BY engine", (last,))}
        fb = {"basis": last, "picks": picks}
    return {"latest": base_id, "latest_ms": base_ms, "targets": list(targets.values()), "fallback": fb}


def prediction_log(con, limit=40):
    draws = con.execute("""SELECT DISTINCT r.target_draw, d.draw_date, d.numbers, d.bullseye FROM results r
                           JOIN draws d ON d.draw_id=r.target_draw WHERE r.valid=1
                           ORDER BY r.target_draw DESC LIMIT ?""", (limit,)).fetchall()
    out = []
    for did, date, nums, be in draws:
        hits = dict(con.execute("SELECT engine, hits FROM results WHERE target_draw=? AND spot=10 AND valid=1",
                                (did,)).fetchall())
        kind = con.execute("SELECT kind FROM predictions WHERE target_draw=? LIMIT 1", (did,)).fetchone()
        best = con.execute("""SELECT engine, spot, hits, pay_hotspot, pay_bullseye FROM results WHERE target_draw=? AND valid=1
                              ORDER BY pay_hotspot + pay_bullseye DESC, hits DESC LIMIT 1""", (did,)).fetchone()
        out.append(dict(draw=did, date=date, numbers=json.loads(nums), bullseye=be, hits=hits,
                        kind=kind[0] if kind else "", best=best))
    return out


def time_tables(con):
    """Average 10-spot hits per engine by hour of day and by weekday (scored tickets only)."""
    rows = con.execute("""SELECT d.draw_date, r.engine, r.hits FROM results r JOIN draws d ON d.draw_id=r.target_draw
                          WHERE r.valid=1 AND r.spot=10""").fetchall()
    by_h, by_d, cache = {}, {}, {}
    for date, en, h in rows:
        if date not in cache:
            ms = draw_epoch(date)
            cache[date] = dt.datetime.fromtimestamp(ms / 1000, _PT) if ms else None
        t = cache[date]
        if not t:
            continue
        for key, bucket in ((t.hour, by_h), (t.weekday(), by_d)):
            a = bucket.setdefault(key, {}).setdefault(en, [0, 0])
            a[0] += h
            a[1] += 1
    return by_h, by_d


def time_html(con, names):
    by_h, by_d = time_tables(con)
    if not by_h:
        return ""
    sd = math.sqrt(10 * 0.25 * 0.75 * 70 / 79)

    def cell(a):
        if not a:
            return "<td class='muted'>—</td>"
        m = a[0] / a[1]
        z = (m - 2.5) / (sd / math.sqrt(a[1]))
        cls = "" if a[1] < 50 else (" class='big'" if z >= 2 else (" class='warnt'" if z <= -2 else ""))
        return f"<td{cls}>{m:.2f}</td>"

    def table(bucket, label, keys, fmt):
        head = "".join(f"<th>{n}</th>" for n in names)
        body = ""
        for k in keys:
            if k not in bucket:
                continue
            n = max(a[1] for a in bucket[k].values())
            body += f"<tr><td>{fmt(k)}</td><td>{n}</td>" + "".join(cell(bucket[k].get(e)) for e in names) + "</tr>"
        return f"<div class='scroll'><table><tr><th>{label}</th><th>Kỳ</th>{head}</tr>{body}</table></div>"

    days = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "CN"]
    return ("<h2>Theo giờ trong ngày · TB trúng vé 10 số (kỳ vọng 2.50)</h2>"
            + table(by_h, "Giờ", range(24), lambda h: f"{h:02d}:00")
            + "<h2>Theo thứ trong tuần</h2>" + table(by_d, "Ngày", range(7), lambda d: days[d])
            + "<p class='note'>Ô xanh = cao hơn may rủi (z ≥ 2), ô vàng = thấp hơn (z ≤ −2), chỉ tô khi có ≥ 50 kỳ. Với hơn 200 ô, "
              "khoảng 10 ô sẽ tô màu chỉ do may rủi. Một giờ 'tốt' thật phải giữ màu xanh qua nhiều tuần.</p>")


WALLET_LEVELS = [300, 1000, 2000, 5000, 10000, 25000, 50000, 100000]


_WROWS = {}


def _wallet_rows(con, engine, spot):
    """Scored draws of one engine/spot grouped by Pacific day (cached per report build)."""
    key = (engine, spot)
    if key not in _WROWS:
        rows = con.execute("""SELECT r.target_draw, d.draw_date, r.hits, r.bullseye_hit FROM results r
                              JOIN draws d ON d.draw_id=r.target_draw
                              WHERE r.valid=1 AND r.engine=? AND r.spot=? ORDER BY r.target_draw""", (engine, spot)).fetchall()
        days = {}
        for did, date, hits, be in rows:
            ms = draw_epoch(date)
            if ms:
                day = dt.datetime.fromtimestamp(ms / 1000, _PT).date().isoformat()
                days.setdefault(day, []).append((did, hits, be, date))
        _WROWS[key] = days
    return _WROWS[key]


def play_session(draws, spot, be_on=True, max_draws=999, take_profit=None, levels=False, budget=100):
    """One day with $budget. Each draw costs $1 (+$1 Bulls-Eye if be_on). Stops at max_draws, when broke,
    at take_profit, or (levels=True) when profit falls back to a locked level (+300, +1000 …)."""
    cost = 2 if be_on else 1
    bal, floor, peak, n, why, big = float(budget), None, 0.0, 0, "hết kỳ trong ngày", None
    for did, hits, be, date in draws:
        if n >= max_draws:
            why = f"đủ {max_draws} kỳ"
            break
        if bal < cost:
            why = "hết tiền"
            break
        bal -= cost
        win = kdb.PAY_BULLSEYE[spot].get(hits, 0) if (be_on and be) else kdb.PAY_HOTSPOT[spot].get(hits, 0)
        bal += win
        n += 1
        if not big or win > big[0]:
            big = (win, did, hits, be and be_on, date)
        p = bal - budget
        peak = max(peak, p)
        if take_profit and p >= take_profit:
            why = f"chốt lời +${take_profit}"
            break
        if levels:
            for L in WALLET_LEVELS:
                if p >= L and (floor is None or L > floor):
                    floor = L
            if floor is not None and p <= floor:
                why = f"chốt lời +${floor:,}"
                break
    else:
        if n >= max_draws:
            why = f"đủ {max_draws} kỳ"
        elif bal < cost:
            why = "hết tiền"
    return dict(result=bal - budget, draws=n, why=why, peak=peak, big=big, playing=(why == "hết kỳ trong ngày"))


def wallet_days(con, engine, spot, be=True, max_draws=999, take_profit=None, levels=True, label=None):
    out = []
    for day, draws in sorted(_wallet_rows(con, engine, spot).items()):
        r = play_session(draws, spot, be, max_draws, take_profit, levels)
        r.update(day=day, plan=label)
        out.append(r)
    return out


def plan_name(p):
    return (f"{p['engine']} {p['spot']} số{' +BE' if p['be'] else ''} · "
            f"{'tới hết tiền' if p['max_draws'] >= 999 else str(p['max_draws']) + ' kỳ'}"
            f"{' · chốt +' + str(p['take_profit']) if p['take_profit'] else ''}")


def adaptive_days(con, lookback=7):
    """The self-improving wallet: each day it plays the plan that made the most money over the previous
    `lookback` days (it never sees the day it is playing), then the day's real result is added to its memory."""
    plans = [dict(engine=e, spot=s, be=b, max_draws=m, take_profit=t, levels=False)
             for e in engines.ENGINES for s in (5, 8, 10) for b in (False, True)
             for m in (25, 50, 100, 999) for t in (None, 50, 100)]
    table = {}
    for i, p in enumerate(plans):
        for r in wallet_days(con, p["engine"], p["spot"], p["be"], p["max_draws"], p["take_profit"], False):
            table.setdefault(r["day"], {})[i] = r
    out = []
    days = sorted(table)
    for j, day in enumerate(days):
        past = days[max(0, j - lookback):j]
        if len(past) < 3:
            continue
        score = {i: sum(table[d][i]["result"] for d in past if i in table[d]) for i in table[day]}
        best = max(score, key=score.get)
        r = dict(table[day][best])
        r.update(day=day, plan=plan_name(plans[best]))
        out.append(r)
    return out


def wallet_html(con, cfg):
    _WROWS.clear()
    wallets = cfg.get("wallets") or [{"engine": "GAP", "spot": 10}]
    today = dt.datetime.now(_PT).date().isoformat() if _PT else dt.date.today().isoformat()
    cards, tables = [], []
    for w in wallets:
        if w.get("adaptive"):
            days = adaptive_days(con, w.get("lookback", 7))
            title, col = w.get("name", "Vòng lặp tự chọn"), "#facc15"
        else:
            p = dict(engine=w["engine"], spot=w["spot"], be=w.get("be", True), max_draws=w.get("max_draws", 999),
                     take_profit=w.get("take_profit"), levels=w.get("levels", True))
            days = wallet_days(con, p["engine"], p["spot"], p["be"], p["max_draws"], p["take_profit"], p["levels"])
            title, col = w.get("name") or plan_name(p), COLORS.get(w["engine"], "#fff")
        if not days:
            continue
        tot = sum(d["result"] for d in days)
        wins = sum(d["result"] > 0 for d in days)
        t = next((d for d in days if d["day"] == today), None)
        now = (f"Hôm nay: <strong>{t['result']:+,.0f}$</strong> sau {t['draws']} kỳ · {'đang chơi' if t['playing'] else t['why']}"
               if t else "Hôm nay: chưa có kỳ nào")
        cards.append(f"<div><span style='color:{col}'>{e_(title)}</span><b style='color:{'var(--green)' if tot > 0 else 'var(--red)'}'>"
                     f"{tot:+,.0f}$</b><em>{len(days)} ngày · {wins} ngày lời · bỏ ra ${100 * len(days):,}<br>{now}</em></div>")
        rows = ""
        for d in reversed(days[-31:]):
            b = d["big"]
            bt = f"{b[2]} trúng{' +BE' if b[3] else ''} → ${b[0]:,.0f}" if b and b[0] > 0 else "—"
            plan = f"<td class='muted'>{e_(d['plan'])}</td>" if w.get("adaptive") else ""
            rows += (f"<tr><td>{d['day']}</td><td class='{'big' if d['result'] > 0 else ''}'>{d['result']:+,.0f}$</td>"
                     f"<td>{d['draws']}</td><td>{d['peak']:+,.0f}$</td><td>{bt}</td><td class='muted'>{d['why']}</td>{plan}</tr>")
        ph = "<th>Cách chơi đã chọn</th>" if w.get("adaptive") else ""
        tables.append(f"<h3 style='font-size:12px;color:{col};margin:16px 0 6px;letter-spacing:.08em'>{e_(title).upper()}</h3>"
                      f"<div class='scroll'><table><tr><th>Ngày</th><th>Kết quả</th><th>Kỳ đã chơi</th><th>Lời cao nhất</th>"
                      f"<th>Trúng lớn nhất</th><th>Lý do dừng</th>{ph}</tr>{rows}</table></div>")
    if not cards:
        return ""
    return ("<h2>Ví ảo · $100 mỗi ngày</h2><div class='kpis'>" + "".join(cards) + "</div>"
            "<p class='note'>Mỗi ví bắt đầu ngày với $100 và chơi bằng vé đã khóa trước kỳ quay. "
            "<b>Vòng lặp tự chọn</b>: mỗi ngày chọn trong 648 cách chơi cách đã lời nhiều nhất 7 ngày trước đó, chơi mù, rồi học thêm kết quả. "
            "<b>Mất ít nhất</b>: 5 số, không Bulls-Eye, 25 kỳ — cách vòng lặp tìm ra là lỗ ít nhất. "
            "Tiền ảo — Hot Spot trả lại khoảng 64¢ cho mỗi $1, không cách chơi nào đổi được con số đó.</p>" + "".join(tables))


def prereg_html(con, cfg):
    """Pre-registered test, criteria fixed BEFORE the data was seen (28/10/2026)."""
    pr = cfg.get("prereg")
    if not pr:
        return ""
    e1, e0, sp, k, start = pr["engine"], pr["baseline"], pr["spot"], pr["min_hits"], pr["start_draw"]
    rows = con.execute("""SELECT a.target_draw, a.hits, b.hits FROM results a JOIN results b
                          ON a.target_draw=b.target_draw AND b.engine=? AND b.spot=a.spot AND b.valid=1
                          JOIN predictions p ON p.target_draw=a.target_draw AND p.engine=a.engine
                          WHERE a.engine=? AND a.spot=? AND a.valid=1 AND a.target_draw>=? AND p.kind='live'
                          ORDER BY a.target_draw""", (e0, e1, sp, start)).fetchall()
    n = len(rows)
    p0 = sum(math.comb(sp, h) * math.comb(80 - sp, 20 - h) for h in range(k, sp + 1)) / math.comb(80, 20)
    hit1 = sum(r[1] >= k for r in rows)
    hit0 = sum(r[2] >= k for r in rows)
    b = sum(r[1] >= k and r[2] < k for r in rows)
    c = sum(r[1] < k and r[2] >= k for r in rows)
    rate = hit1 / n if n else 0
    z = (rate - p0) / math.sqrt(p0 * (1 - p0) / n) if n else 0
    p_one = p_upper(z) if n else 1
    zm = (b - c) / math.sqrt(b + c) if b + c else 0
    pm = p_upper(zm) if b + c else 1
    done = n >= pr["n_target"]
    pass1, pass2 = rate >= pr["threshold_rate"], pm < pr["alpha"]
    if not done:
        verdict = (f"Đang thu thập: {n:,}/{pr['n_target']:,} kỳ. Số tạm thời bên dưới <b>chưa dùng để kết luận</b> — "
                   "kết luận chỉ được đọc khi đủ mẫu, theo đúng tiêu chí đã ghi trước.")
        cls = "muted"
    elif pass1 and pass2:
        verdict, cls = f"ĐẠT cả 2 tiêu chí sau {n:,} kỳ — {e1} có lợi thế đã được kiểm định trước. Cần thêm 1 vòng xác nhận độc lập.", "good"
    else:
        verdict, cls = (f"KHÔNG ĐẠT sau {n:,} kỳ — {e1} không có lợi thế (tiêu chí 1: {'đạt' if pass1 else 'không'}, "
                        f"tiêu chí 2: {'đạt' if pass2 else 'không'})."), "flat"
    bar = min(100, n / pr["n_target"] * 100)
    prereg2 = ""
    p2 = cfg.get("prereg2")
    if p2:
        r2 = con.execute("""SELECT a.hits FROM results a JOIN predictions p ON p.target_draw=a.target_draw AND p.engine=a.engine
                            WHERE a.engine=? AND a.spot=10 AND a.valid=1 AND a.target_draw>=? AND p.kind='live'""",
                         (p2["engine"], p2["start_draw"])).fetchall()
        n2 = len(r2)
        m2 = sum(x[0] for x in r2) / n2 if n2 else 0
        z2 = (m2 - 2.5) / (math.sqrt(10 * .25 * .75 * 70 / 79) / math.sqrt(n2)) if n2 else 0
        st = ("đang thu thập — chưa kết luận" if n2 < p2["n_target"] else
              ("ĐẠT" if z2 >= p2["z_threshold"] else "KHÔNG ĐẠT"))
        prereg2 = (f"<div class='head' style='margin-top:10px'><b>Giả thuyết 2 (ghi {p2['registered']}):</b> {p2['engine']} trúng trung bình "
                   f"&gt; 2,500 số/vé 10 số. Mẫu {p2['n_target']:,} kỳ live từ #{p2['start_draw']}; đạt nếu z ≥ {p2['z_threshold']}.<br>"
                   f"Hiện: <b>{m2:.3f}</b> trên {n2:,} kỳ (z {z2:+.2f}) · {st}</div>")
    return f"""<h2>Kiểm định đăng ký trước · {e1} trúng ≥{k}/{sp}</h2>
<div class="head"><b>Giả thuyết:</b> {e1} (đã khóa, không chỉnh) trúng ≥{k}/{sp} nhiều hơn may rủi ({p0 * 100:.2f}%) và nhiều hơn {e0} trên cùng các kỳ.<br>
<b>Mẫu:</b> {pr['n_target']:,} kỳ live từ kỳ #{start} · <b>Đạt nếu:</b> (1) tỉ lệ ≥ {pr['threshold_rate'] * 100:.2f}% <b>và</b>
(2) so cặp từng kỳ với {e0} p &lt; {pr['alpha']} (McNemar, một phía) · ghi ngày {pr['registered']}.</div>
<div style="background:var(--line);border-radius:4px;height:10px;margin:10px 0"><div style="width:{bar:.1f}%;height:10px;border-radius:4px;background:var(--green)"></div></div>
<div class="kpis">
<div><span>{e1} ≥{k}/{sp}</span><b>{hit1}/{n} = {rate * 100:.2f}%</b><em>z {z:+.2f} · p một phía {fmt_p(p_one)} · ngưỡng {pr['threshold_rate'] * 100:.2f}%</em></div>
<div><span>{e0} ≥{k}/{sp} (cùng kỳ)</span><b>{hit0}/{n} = {(hit0 / n * 100 if n else 0):.2f}%</b><em>kỳ vọng {p0 * 100:.2f}%</em></div>
<div><span>So cặp từng kỳ</span><b>{b} : {c}</b><em>{e1} đạt mà {e0} không : ngược lại · p {fmt_p(pm)}</em></div>
</div><p class="note {cls}" style="font-size:13.5px">{verdict}</p>{prereg2}"""


def zfocus_html(con, cfg):
    """Focus tab for one engine the user picked (config focus_engine), plus its pre-registered test H3."""
    en = cfg.get("focus_engine")
    if not en:
        return "<p class='muted'>Chưa chọn engine theo dõi (config focus_engine).</p>"
    e_ = html.escape
    sd = math.sqrt(10 * .25 * .75 * 70 / 79)
    rows = con.execute("""SELECT r.target_draw, r.hits, r.bullseye_hit, d.draw_date FROM results r JOIN draws d ON d.draw_id=r.target_draw
                          WHERE r.engine=? AND r.spot=10 AND r.valid=1 ORDER BY r.target_draw""", (en,)).fetchall()
    n = len(rows)
    if not n:
        return f"<h2>{e_(en)}</h2><p class='muted'>Chưa có kỳ nào được chấm.</p>"
    P = [math.comb(10, h) * math.comb(70, 20 - h) / math.comb(80, 20) for h in range(11)]
    hits = [r[1] for r in rows]
    mean = sum(hits) / n
    z = (mean - 2.5) / (sd / math.sqrt(n))
    big = sum(h >= 7 for h in hits)
    p7 = sum(P[7:])
    # per day (Pacific date of the draw)
    days = {}
    for d_id, h, be, ds in rows:
        ms = draw_epoch(ds)
        key = dt.datetime.fromtimestamp(ms / 1000, _PT).strftime("%m/%d") if ms else "?"
        days.setdefault(key, []).append(h)
    drows = "".join(
        f"<tr><td>{k}</td><td>{len(v)}</td><td>{sum(v)/len(v):.3f}</td><td>{(sum(v)/len(v)-2.5)/(sd/math.sqrt(len(v))):+.2f}</td>"
        f"<td>{sum(x>=5 for x in v)/len(v)*100:.1f}%</td><td>{sum(x>=7 for x in v)}</td><td>{max(v)}</td></tr>"
        for k, v in list(days.items())[-10:][::-1])
    last_day, today = list(days.items())[-1]
    # hit distribution vs theory
    cnt = [hits.count(h) for h in range(11)]
    hs_ = [h for h in range(11) if cnt[h] or P[h] * n >= 0.5]
    dist = ("<tr><th>Trúng</th>" + "".join(f"<th>{h}</th>" for h in hs_) + "</tr><tr><td>Thật</td>" + "".join(f"<td>{cnt[h]:,}</td>" for h in hs_)
            + "</tr><tr><td class='muted'>May rủi</td>" + "".join(f"<td class='muted'>{P[h]*n:,.1f}</td>" for h in hs_) + "</tr>")
    bigs = [(d, h, be, ds) for d, h, be, ds in rows if h >= 6][-8:][::-1]
    def _short(ds):
        ms = draw_epoch(ds)
        return dt.datetime.fromtimestamp(ms / 1000, _PT).strftime("%m/%d %I:%M %p") if ms else str(ds)
    blist = "".join(f"<tr><td>#{d}</td><td>{_short(ds)}</td><td class='{'big' if h >= 7 else ''}'>{h}/10</td><td>{'✓' if be else ''}</td></tr>" for d, h, be, ds in bigs)
    # current ticket
    tk = con.execute("SELECT ranked, basis_draw FROM predictions WHERE engine=? AND kind='live' ORDER BY target_draw DESC LIMIT 1", (en,)).fetchone()
    balls = "".join(f"<div class='ball'>{x}</div>" for x in sorted(json.loads(tk[0])[:10])) if tk else ""
    # lab versions of this family
    labrow = ""
    try:
        import lab
        t = lab.table(con).get(en, [])
        if t:
            labrow = (f"<h2>10 phiên bản trong lab · {t[0][2]:,} kỳ mỗi bản (vé 10 số)</h2><div class='scroll'><table>"
                      "<tr><th>Bản</th>" + "".join(f"<th>v{v} · {e_(str(p_))}</th>" for v, p_, m_, a, zz, r5 in t) + "</tr>"
                      "<tr><td>TB</td>" + "".join(f"<td>{a:.3f}</td>" for v, p_, m_, a, zz, r5 in t) + "</tr>"
                      "<tr><td>z</td>" + "".join(f"<td class='{'warnt' if abs(zz) >= 2 else ''}'>{zz:+.2f}</td>" for v, p_, m_, a, zz, r5 in t) + "</tr>"
                      "</table></div><p class='note'>Cửa sổ = số kỳ gần nhất dùng để tìm vùng nóng. v1 là bản đang chạy.</p>")
    except Exception:
        pass
    # pre-registered H3
    h3 = ""
    pr = cfg.get("prereg3")
    if pr and pr.get("engine") == en:
        st = dt.datetime.strptime(pr["start_time"], "%Y-%m-%d %H:%M").replace(tzinfo=_PT).timestamp() * 1000
        live = [(d, h) for d, h, be, ds in rows if d > pr["after_draw"] and (draw_epoch(ds) or 0) >= st]
        # every valid ticket counts: live AND walk-forward (both are built only from draws before the target)
        n3 = len(live)
        m3 = sum(h for _, h in live) / n3 if n3 else 0
        z3 = (m3 - 2.5) / (sd / math.sqrt(n3)) if n3 else 0
        b3 = sum(h >= pr["big_min"] for _, h in live)
        exp3 = p7 * n3
        if n3 < pr["n_target"]:
            v3 = f"Đang thu thập {n3:,}/{pr['n_target']:,} kỳ — số tạm thời, <b>chưa dùng để kết luận</b>."
        else:
            ok1, ok2 = z3 >= pr["z_threshold"], b3 >= pr["big_needed"]
            v3 = ("ĐẠT" if ok1 or ok2 else "KHÔNG ĐẠT") + f" sau {n3:,} kỳ (TB: {'đạt' if ok1 else 'không'} · trúng lớn: {'đạt' if ok2 else 'không'})."
        h3 = f"""<div class="head"><b>Giả thuyết 3 (ghi {e_(pr['registered'])}):</b> {e_(en)} đoán tốt hơn may rủi. Mẫu {pr['n_target']:,} kỳ (vé khóa trước kỳ quay: live + walk-forward) tính từ kỳ đầu tiên sau {e_(pr['start_time'])} PT.
Đạt nếu (a) TB trúng vé 10 số có z ≥ {pr['z_threshold']} <b>hoặc</b> (b) có ≥ {pr['big_needed']} lần trúng ≥{pr['big_min']}/10 (may rủi cho ~{p7*pr['n_target']:.1f}).</div>
<div style="background:var(--line);border-radius:4px;height:10px;margin:8px 0"><div style="width:{min(100, n3 / pr['n_target'] * 100):.1f}%;height:10px;border-radius:4px;background:var(--green)"></div></div>
<div class="kpis"><div><span>H3 · TB trúng</span><b>{m3:.3f}</b><em>{n3:,} kỳ · z {z3:+.2f}</em></div>
<div><span>H3 · trúng ≥{pr['big_min']}/10</span><b>{b3}</b><em>may rủi ~{exp3:.1f} · cần ≥ {pr['big_needed']} khi đủ mẫu</em></div></div>
<p class="note">{v3}</p>"""
    return f"""<h2>Engine theo dõi · {e_(en)} · vé hiện tại (khóa từ kỳ #{tk[1] if tk else '—'})</h2>
<div class="sug">{balls}</div>
{h3}
<h2>Toàn bộ thời gian live</h2>
<div class="kpis"><div><span>TB trúng / 10</span><b>{mean:.3f}</b><em>{n:,} kỳ · z {z:+.2f} · may rủi 2.500</em></div>
<div><span>Ngày gần nhất · {last_day}</span><b>{sum(today)/len(today):.3f}</b><em>{len(today)} kỳ · cao nhất {max(today)}/10</em></div>
<div><span>Trúng ≥5/10</span><b>{sum(h >= 5 for h in hits)/n*100:.2f}%</b><em>may rủi {sum(P[5:])*100:.2f}%</em></div>
<div><span>Trúng ≥7/10</span><b>{big}</b><em>may rủi ~{p7*n:.1f}</em></div></div>
<div class="zgrid">
<div><h2>Theo ngày</h2><div class="scroll"><table><tr><th>Ngày</th><th>Kỳ</th><th>TB</th><th>z</th><th>≥5</th><th>≥7</th><th>Cao nhất</th></tr>{drows}</table></div></div>
<div><h2>Lần trúng ≥6 gần nhất</h2><div class="scroll"><table><tr><th>Kỳ</th><th>Giờ</th><th>Trúng</th><th>BE</th></tr>{blist}</table></div></div>
</div>
<h2>Phân bố số trúng (vé 10 số) · thật so với may rủi</h2><div class="scroll"><table>{dist}</table></div>
{labrow}"""



BIG_PAY = 40   # a "big hit" = a $1 ticket that pays $40 or more (10-spot 7/10, 5-spot 5/5, 6-spot 5/6, ...)


def archive(con):
    """Full history of scored tickets, grouped by day / week / month, plus every big hit.
    Writes logs/days/YYYY-MM-DD.json (+ .csv) and logs/archive/*.csv; returns what the page shows."""
    names = list(engines.ENGINES)
    when = {}
    for did, ds, nums, be in con.execute("""SELECT d.draw_id, d.draw_date, d.numbers, d.bullseye FROM draws d
                                             WHERE d.draw_id IN (SELECT DISTINCT target_draw FROM results WHERE valid=1)"""):
        ms = draw_epoch(ds)
        if ms:
            when[did] = (dt.datetime.fromtimestamp(ms / 1000, _PT), json.loads(nums), be)
    per_draw, big = {}, []
    W = {}   # wallet aggregates: W[day][engine][spot] = [tickets, $ back without BE, $ back with BE]
    for t, en, sp, h, beh, hs, bp, kind in con.execute(
            """SELECT r.target_draw, r.engine, r.spot, r.hits, r.bullseye_hit, r.pay_hotspot, r.pay_bullseye, p.kind
               FROM results r LEFT JOIN predictions p ON p.target_draw=r.target_draw AND p.engine=r.engine
               WHERE r.valid=1"""):
        if t not in when:
            continue
        hs, bp = hs or 0, bp or 0
        wd = W.setdefault(when[t][0].date().isoformat(), {}).setdefault(en, {}).setdefault(str(sp), [0, 0, 0])
        wd[0] += 1
        wd[1] += hs
        wd[2] += bp if beh else hs          # $2 ticket with Bulls-Eye: BE prize replaces the base prize when it hits
        if sp == 10:
            d = per_draw.setdefault(t, {"k": kind or "", "h": {}, "be": {}, "p": {}, "pb": {}})
            d["h"][en] = h
            d["be"][en] = beh
            d["p"][en] = hs
            d["pb"][en] = bp if beh else hs
        if (hs or 0) >= BIG_PAY:
            big.append((t, en, sp, h, beh, hs, bp))
    # group
    def keys(w):
        d = w.date()
        mon = d - dt.timedelta(days=d.weekday())
        return d.isoformat(), mon.isoformat(), d.strftime("%Y-%m")
    groups = {"day": {}, "week": {}, "month": {}}
    for t, d in per_draw.items():
        w = when[t][0]
        for g, k in zip(("day", "week", "month"), keys(w)):
            G = groups[g].setdefault(k, {"n": 0, "sum": {}, "cnt": {}, "big7": 0, "best": (0, "", 0)})
            G["n"] += 1
            for en, h in d["h"].items():
                G["sum"][en] = G["sum"].get(en, 0) + h
                G["cnt"][en] = G["cnt"].get(en, 0) + 1
                if h >= 7:
                    G["big7"] += 1
                if h > G["best"][0]:
                    G["best"] = (h, en, t)
    # files
    logdir = os.path.join(HERE, "logs")
    os.makedirs(os.path.join(logdir, "days"), exist_ok=True)
    os.makedirs(os.path.join(logdir, "archive"), exist_ok=True)
    by_day = {}
    for t in sorted(per_draw):
        by_day.setdefault(when[t][0].date().isoformat(), []).append(t)
    for day, ts in by_day.items():
        rows = [{"d": t, "t": when[t][0].strftime("%I:%M %p").lstrip("0"), "k": per_draw[t]["k"][:1],
                 "n": when[t][1], "b": when[t][2], "h": [per_draw[t]["h"].get(en) for en in names],
                 "e": [per_draw[t]["be"].get(en, 0) for en in names],
                 "p": [per_draw[t]["p"].get(en, 0) for en in names],
                 "q": [per_draw[t]["pb"].get(en, 0) for en in names]} for t in ts]
        with open(os.path.join(logdir, "days", f"{day}.json"), "w", encoding="utf-8") as f:
            json.dump({"day": day, "engines": names, "rows": rows}, f, separators=(",", ":"))
        with open(os.path.join(logdir, "days", f"{day}.csv"), "w", encoding="utf-8") as f:
            f.write("draw_id,time_pt,ticket,numbers,bullseye," + ",".join(names) + ",best_prize_$1_10spot,best_engine\n")
            for r in rows:
                bi = max(range(len(names)), key=lambda i: r["p"][i] or 0)
                f.write(f"{r['d']},{r['t']},{'live' if r['k'] == 'l' else 'walk-forward'},{' '.join(map(str, r['n']))},{r['b'] or ''},"
                        + ",".join("" if x is None else str(x) for x in r["h"]) + f",{r['p'][bi] or 0:.0f},{names[bi] if r['p'][bi] else ''}\n")
    for g, G in groups.items():
        with open(os.path.join(logdir, "archive", f"{ {'day': 'daily', 'week': 'weekly', 'month': 'monthly'}[g] }.csv"), "w", encoding="utf-8") as f:
            f.write("period,draws,hits_ge7," + ",".join(names) + ",best_hit,best_engine,best_draw\n")
            for k in sorted(G):
                v = G[k]
                f.write(f"{k},{v['n']},{v['big7']}," + ",".join(f"{v['sum'][e]/v['cnt'][e]:.3f}" if v["cnt"].get(e) else "" for e in names)
                        + f",{v['best'][0]},{v['best'][1]},{v['best'][2]}\n")
    big.sort(key=lambda x: (-x[0], x[1], x[2]))
    with open(os.path.join(logdir, "archive", "big_hits.csv"), "w", encoding="utf-8") as f:
        f.write("draw_id,date_pt,time_pt,engine,spot,hits,bullseye_hit,prize_per_$1,bullseye_prize\n")
        for t, en, sp, h, beh, hs, bp in big:
            w = when[t][0]
            f.write(f"{t},{w:%Y-%m-%d},{w:%I:%M %p},{en},{sp},{h},{beh},{hs:.0f},{(bp or 0):.0f}\n")
    return {"groups": groups, "big": big, "when": when, "days": sorted(by_day), "names": names, "wallet": W,
            "today": by_day and json.load(open(os.path.join(logdir, "days", f"{max(by_day)}.json"), encoding="utf-8"))}


def archive_html(A):
    e_ = html.escape
    names = A["names"]
    sd = math.sqrt(10 * .25 * .75 * 70 / 79)

    def table(g, label, fmt):
        G = A["groups"][g]
        if not G:
            return "<p class='muted'>Chưa có dữ liệu.</p>"
        head = f"<tr><th>{label}</th><th>Kỳ</th><th>≥7/10</th>" + "".join(f"<th style='color:{COLORS.get(n)}'>{n}</th>" for n in names) + "<th>Cao nhất</th></tr>"
        body = []
        for k in sorted(G, reverse=True):
            v = G[k]
            cells = []
            avgs = {n: v["sum"][n] / v["cnt"][n] for n in names if v["cnt"].get(n)}
            top = max(avgs, key=avgs.get) if avgs else None
            for n in names:
                if n not in avgs:
                    cells.append("<td class='muted'>—</td>")
                    continue
                z = (avgs[n] - 2.5) / (sd / math.sqrt(v["cnt"][n]))
                cls = "good" if z >= 2 else ("bad" if z <= -2 else "")
                cells.append(f"<td class='{cls}'>{'<b>' if n == top else ''}{avgs[n]:.2f}{'</b>' if n == top else ''}</td>")
            b = v["best"]
            body.append(f"<tr><td>{fmt(k)}</td><td>{v['n']:,}</td><td>{v['big7']}</td>{''.join(cells)}"
                        f"<td>{b[0]}/10 <span style='color:{COLORS.get(b[1])}'>{e_(b[1])}</span></td></tr>")
        return f"<div class='scroll tall'><table>{head}{''.join(body)}</table></div>"

    def wk(k):
        a = dt.date.fromisoformat(k)
        return f"{a:%d/%m}–{a + dt.timedelta(days=6):%d/%m}"
    days_opts = "".join(f"<option value='{d}'>{d[8:10]}/{d[5:7]}/{d[:4]}</option>" for d in reversed(A["days"]))
    big_rows = "".join(
        f"<tr data-e='{e_(en)}' data-s='{sp}'><td>#{t}</td><td>{A['when'][t][0]:%d/%m %I:%M %p}</td>"
        f"<td style='color:{COLORS.get(en)}'>{e_(en)}</td><td>{sp} số</td><td class='{'big' if h == sp or (sp == 10 and h >= 8) else ''}'>{h}/{sp}</td>"
        f"<td>${hs:,.0f}</td><td>{'✓ $' + format(bp, ',.0f') if beh else ''}</td></tr>"
        for t, en, sp, h, beh, hs, bp in A["big"][:1500])
    eng_opts = "".join(f"<option>{n}</option>" for n in names)
    n_big = len(A["big"])
    return f"""<div class="subtabs" id="lt">
<button data-v="draws" class="on">Từng kỳ</button><button data-v="day">Ngày</button><button data-v="week">Tuần</button>
<button data-v="month">Tháng</button><button data-v="big">Trúng cao ({n_big:,})</button><button data-v="dl">Tải về</button></div>
<div class="lv" id="lv-draws">
<div class="row"><label>Ngày <select id="logday">{days_opts}</select></label><span class="muted" id="loginfo"></span></div>
<div class="scroll tall" id="logbox"></div>
<p class="note"><b>Thắng cao nhất</b> = giải lớn nhất trong 14 vé 10 số $1 của kỳ đó · <b>Gợi ý</b> = vé $1 của engine gợi ý thắng bao nhiêu (dòng cuối: tổng thắng / tiền bỏ ra cả ngày). Các cột engine = số trúng / 10. Ô xanh = trúng ≥ 6/10 · ★ = trúng cả Bulls-Eye. L = vé khóa trực tiếp, W = walk-forward (vé tạo từ các kỳ trước, chấm khi dữ liệu về theo lô).</p></div>
<div class="lv" id="lv-day">{table('day', 'Ngày', lambda k: f"{k[8:10]}/{k[5:7]}")}<p class="note">TB số trúng vé 10 số theo ngày (kỳ vọng 2.50). Số đậm = engine tốt nhất ngày đó · xanh/đỏ = z ≥ 2 / ≤ −2.</p></div>
<div class="lv" id="lv-week">{table('week', 'Tuần (T2–CN)', wk)}<p class="note">Tuần tính từ thứ Hai đến Chủ nhật, giờ Pacific.</p></div>
<div class="lv" id="lv-month">{table('month', 'Tháng', lambda k: f"{k[5:7]}/{k[:4]}")}</div>
<div class="lv" id="lv-big">
<div class="row"><label>Engine <select id="bige"><option value="">Tất cả</option>{eng_opts}</select></label>
<label>Vé <select id="bigs"><option value="">Mọi loại</option>{''.join(f'<option value="{k}">{k} số</option>' for k in range(5, 11))}</select></label><span class="muted" id="bigcount"></span></div>
<div class="scroll tall"><table id="bigt"><tr><th>Kỳ</th><th>Giờ (PT)</th><th>Engine</th><th>Vé</th><th>Trúng</th><th>Thắng/$1</th><th>Bulls-Eye</th></tr>{big_rows}</table></div>
<p class="note">Mọi vé $1 thắng từ ${BIG_PAY} trở lên, tất cả engine và loại vé 5–10 số, mới nhất ở trên.</p></div>
<div class="lv" id="lv-dl"><h2>Tải dữ liệu (CSV, mở bằng Excel)</h2>
<div class="dl"><a href="{RAW}logs/archive/daily.csv">📅 Tổng kết theo ngày</a><a href="{RAW}logs/archive/weekly.csv">🗓️ Theo tuần</a>
<a href="{RAW}logs/archive/monthly.csv">📆 Theo tháng</a><a href="{RAW}logs/archive/big_hits.csv">🏆 Tất cả lần trúng cao</a><a id="daycsv" href="#" data-base="{RAW}">📒 Nhật ký từng kỳ của ngày đang chọn</a>
<a href="{DRAWS}">🎱 Kết quả quay mỗi ngày (thư mục)</a></div>
<p class="note">Các file được tạo lại sau mỗi lần GitHub chạy, từ toàn bộ dữ liệu trong keno.db — không mất ngày nào.</p></div>
<script type="application/json" id="logtoday">{json.dumps(A['today'] or {}, separators=(',', ':'))}</script>"""


def every_round_html(A, cfg, sug=None):
    """Fake wallet: bet $1 (or $2 with Bulls-Eye) on the chosen engine's ticket in EVERY scored round.
    Uses only tickets locked before each draw. The page lets you switch engine / ticket size / Bulls-Eye / start day."""
    names = A["names"]
    focus = next((x for x in (cfg.get("focus_engine"), sug) if x in names), "RANDOM")
    opts = "".join(f"<option value='{n}'{' selected' if n == focus else ''}>{n}</option>" for n in names)
    days = sorted(A["wallet"])
    dopts = "".join(f"<option value='{d}'>{d[8:10]}/{d[5:7]}/{d[:4]}</option>" for d in days)
    return f"""<div class="row"><label>Engine <select id="wv-e"><option value="*">Tất cả {len(names)} engine (1 vé mỗi engine)</option>{opts}</select></label>
<label>Từ ngày <select id="wv-d">{dopts}</select></label>
<label><input type="checkbox" id="wv-b"> Bulls-Eye (+$1/vé)</label></div>
<div class="spots" id="wv-s">{''.join(f'<button data-k="{k}" class="{"on" if k == 10 else ""}">{k} số</button>' for k in range(5, 11))}</div>
<div class="kpis" id="wv-k"></div>
<svg id="wv-c" class="chart" viewBox="0 0 800 170" preserveAspectRatio="none" style="height:170px"></svg>
<div class="scroll" style="max-height:34dvh;margin-top:8px"><table id="wv-t"></table></div>
<p class="note">Ví ảo: đặt tiền vào <b>mọi kỳ</b> được chấm, theo đúng vé đã khóa trước kỳ quay (không biết trước kết quả). Không giới hạn tiền mỗi ngày.
Thắng tính theo bảng trả thưởng Hot Spot cho vé $1; có Bulls-Eye thì vé $2 và giải Bulls-Eye thay cho giải thường khi trúng Bulls-Eye.</p>
<script type="application/json" id="wvdata">{json.dumps(A['wallet'], separators=(',', ':'))}</script>"""



def lab_html(con, cfg):
    """Engine lab: 10 versions per family, scored like everything else."""
    if not cfg.get("lab", True):
        return ""
    import lab
    tab = lab.table(con)
    if not tab:
        return "<h2>Phòng thí nghiệm · 10 phiên bản mỗi engine</h2><p class='muted'>Đang chờ kỳ đầu tiên được chấm.</p>"
    allz = [r[4] for rows in tab.values() for r in rows]
    n_big = sum(abs(z) >= 2 for z in allz)
    head = "".join(f"<th>v{v}</th>" for v in range(1, 11))
    body = ""
    for fam, rows in tab.items():
        best = max(rows, key=lambda r: r[3])
        cells = ""
        for v, prm, n, a, z, r5 in rows:
            cls = " class='big'" if (n >= 200 and z >= 2) else (" class='warnt'" if (n >= 200 and z <= -2) else "")
            star = " ★" if (v, prm) == (best[0], best[1]) else ""
            cells += f"<td{cls} title='{e_(str(prm))} · n={n} · z={z:+.2f}'>{a:.3f}{star}</td>"
        pv = lab.paired_vs_v1(con, fam, best[0]) if best[0] != 1 else None
        cmp_txt = ("v1 là tốt nhất" if best[0] == 1 else
                   f"v{best[0]} − v1 = {pv[0]:+.3f} (z {pv[1]:+.2f})" if pv else "chưa đủ 30 kỳ để so")
        body += (f"<tr><td><b style='color:{COLORS.get(fam, '#ccc')}'>{fam}</b><br><span class='muted'>{e_(lab.FAMILIES[fam][0])}</span></td>"
                 f"{cells}<td>{cmp_txt}</td></tr>")
    rnd = tab.get("RANDOM", [])
    spread = (f"{min(r[3] for r in rnd):.3f} – {max(r[3] for r in rnd):.3f}" if rnd else "—")
    params = "".join(f"<tr><td>{fam}</td>" + "".join(f"<td>{e_(str(p))}</td>" for p in lab.FAMILIES[fam][1]) + "</tr>"
                     for fam in tab)
    return (f"<h2>Phòng thí nghiệm · 10 phiên bản mỗi engine (vé 10 số, kỳ vọng 2.500)</h2>"
            f"<div class='scroll'><table><tr><th>Họ engine</th>{head}<th>Bản tốt nhất so với v1 (cùng kỳ)</th></tr>{body}</table></div>"
            f"<p class='note'>★ = bản cao nhất của họ. Ô xanh/vàng = z ≥ 2 / ≤ −2 (khi ≥ 200 kỳ). Hiện có <b>{n_big}/{len(allz)}</b> ô như vậy — "
            f"may rủi tự tạo ra khoảng {len(allz) * 0.0455:.1f}. <b>Thước đo may rủi:</b> 10 vé RANDOM độc lập đang chênh nhau từ {spread}; "
            f"một bản engine chỉ đáng chú ý nếu vượt xa khoảng đó và giữ được qua thời gian.</p>"
            f"<details><summary class='muted'>Thông số từng phiên bản</summary><div class='scroll'><table><tr><th>Họ</th>{head}</tr>{params}</table></div></details>")


TAB_CSS = r"""
html{-webkit-text-size-adjust:100%}
.top{position:sticky;top:0;z-index:20;background:rgba(7,9,11,.96);backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:10px 16px 0;padding-top:max(10px,env(safe-area-inset-top))}
.top>*{max-width:1180px;margin-left:auto;margin-right:auto}
.brand{display:flex;align-items:center;gap:10px;justify-content:space-between}.brand h1{margin:0}
.dev{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:10px;padding:1px 8px;white-space:nowrap}
.meta{margin:4px auto 6px;font-size:12px}.meta summary{cursor:pointer;color:var(--muted);list-style:none}.meta summary::before{content:"▸ "}.meta[open] summary::before{content:"▾ "}
.tabs{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}.tabs::-webkit-scrollbar{display:none}
.tabs button{flex:0 0 auto;background:none;border:0;border-bottom:2px solid transparent;color:var(--muted);font:inherit;font-size:13px;padding:8px 12px;cursor:pointer;white-space:nowrap}
.tabs button.on{color:var(--green);border-bottom-color:var(--green)}
main{padding-top:6px}
.pane{display:none;animation:fade .15s ease}.pane.on{display:block}.pane>h2:first-child,.pane>.head:first-child{margin-top:10px}
@keyframes fade{from{opacity:.4}to{opacity:1}}
.scroll{max-height:calc(100dvh - 210px)}
/* ---- phone (Fold 6 gập, iPhone, Android thường) ---- */
html[data-dev=phone] body{font-size:13px}
html[data-dev=phone] main{padding:6px 10px 40px}
html[data-dev=phone] .top{padding-left:10px;padding-right:10px}
html[data-dev=phone] h1{font-size:15px}
html[data-dev=phone] h2{font-size:11.5px;margin:18px 0 8px}
html[data-dev=phone] .tabs button{font-size:12.5px;padding:8px 9px}
html[data-dev=phone] .kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:6px}
html[data-dev=phone] .kpis div{padding:8px}html[data-dev=phone] .kpis b{font-size:16px}
html[data-dev=phone] .ball{width:34px;height:34px;font-size:14px}
html[data-dev=phone] .others{grid-template-columns:1fr}
html[data-dev=phone] table{font-size:11.5px}html[data-dev=phone] th,html[data-dev=phone] td{padding:5px 6px}
html[data-dev=phone] .scroll td:first-child,html[data-dev=phone] .scroll th:first-child{position:sticky;left:0;background:var(--panel);z-index:1}
html[data-dev=phone] .picks{grid-template-columns:1fr}
html[data-dev=phone] .note{font-size:11.5px}
html[data-dev=phone] .grp{padding:10px}
html[data-dev=phone] .chips{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;margin-bottom:8px}html[data-dev=phone] .chips::-webkit-scrollbar{display:none}
html[data-dev=phone] .chip{flex:0 0 auto;font-size:11px;padding:2px 8px}
html[data-dev=phone] .sug{gap:6px}
html[data-dev=phone] .spots{flex-wrap:nowrap}html[data-dev=phone] .spots button{flex:1 1 0;min-width:0;padding:5px 0;text-align:center;font-size:12px}
html[data-dev=phone] .others{font-size:11.5px;gap:2px}html[data-dev=phone] .others b{width:84px}
/* ---- Fold mở / tablet ---- */
html[data-dev=fold] main,html[data-dev=tablet] main{padding:8px 14px 40px}
html[data-dev=fold] .kpis{grid-template-columns:repeat(3,minmax(0,1fr))}
html[data-dev=fold] .ball{width:40px;height:40px;font-size:16px}
html[data-dev=fold] .others{grid-template-columns:repeat(2,minmax(0,1fr))}
html[data-dev=fold] table{font-size:12px}
.win{color:var(--amber);font-weight:700}.subtabs{display:flex;gap:4px;flex-wrap:wrap;margin:10px 0 8px}.subtabs button{background:var(--panel);color:var(--muted);border:1px solid var(--line);border-radius:14px;padding:4px 12px;font:inherit;font-size:12.5px;cursor:pointer}.subtabs button.on{color:var(--green);border-color:var(--green)}.lv{display:none}.row{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:8px;font-size:12.5px}.row select{background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:3px 6px;font:inherit}.scroll.tall{max-height:calc(100dvh - 250px)}td.good{color:var(--green)}td.bad{color:var(--red)}tr.sum td{font-weight:700;border-top:2px solid var(--line)}.dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px}.dl a{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px;color:var(--fg);text-decoration:none}.dl a:hover{border-color:var(--green)}.zgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:0 14px}.zgrid>div{min-width:0}.up{grid-template-columns:minmax(0,1fr)}.stale{font-size:11.5px;color:var(--amber);margin:4px 0 2px}.grp{min-width:0}.pane{min-width:0}
html[data-dev=phone] .ball{width:27px;height:27px;font-size:12.5px;border-width:1.5px}
html[data-dev=phone] .sug{gap:3px}html[data-dev=phone] .scroll{max-height:56dvh}
/* fallback khi chưa chạy JS */
@media (max-width:599px){body{font-size:13px}main{padding:6px 10px 40px}.ball{width:34px;height:34px;font-size:14px}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}
"""

TAB_JS = r"""<script>
(function(){
 var H=document.documentElement,tabs=[].slice.call(document.querySelectorAll('#tabs button')),dev=document.getElementById('dev');
 function store(k,v){try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){return null}}
 function show(t,push){var ok=false;tabs.forEach(function(b){var on=b.dataset.t===t;b.classList.toggle('on',on);if(on){ok=true;b.scrollIntoView({block:'nearest',inline:'center'})}});
  if(!ok)return show('sug');
  document.querySelectorAll('.pane').forEach(function(p){p.classList.toggle('on',p.id==='t-'+t)});
  store('keno-tab',t);if(push!==false&&location.hash!=='#'+t)history.replaceState(null,'','#'+t);window.scrollTo(0,0)}
 tabs.forEach(function(b){b.onclick=function(){show(b.dataset.t)}});
 show((location.hash||'').slice(1)||store('keno-tab')||'sug',false);
 window.addEventListener('hashchange',function(){show(location.hash.slice(1),false)});
 // nhận biết loại màn hình: phone / Fold mở / tablet / desktop — chạy lại khi gập/mở máy hoặc xoay
 function classify(){
  var w=window.innerWidth,h=window.innerHeight,r=Math.max(w,h)/Math.min(w,h),touch=matchMedia('(pointer:coarse)').matches,ua=navigator.userAgent;
  var fold=/SM-F9[0-9]{2}/.test(ua),d,label;
  if(w<600){d='phone';label=fold?'Fold · gập':(touch?'Điện thoại':'Cửa sổ hẹp')}
  else if(touch&&w<1100&&r<1.45){d='fold';label=fold?'Fold · mở':'Màn vuông'}
  else if(touch&&w<1100){d='tablet';label='Tablet'}
  else{d='desktop';label='Máy tính'}
  H.dataset.dev=d;if(dev)dev.textContent=label+' · '+w+'px'}
 classify();window.addEventListener('resize',classify);window.addEventListener('orientationchange',classify);
 // vuốt trái/phải để đổi tab trên điện thoại
 var x0=null,y0=null;document.querySelector('main').addEventListener('touchstart',function(e){
  if(e.target.closest('.scroll,.spots,.chips')){x0=null;return}x0=e.touches[0].clientX;y0=e.touches[0].clientY},{passive:true});
 document.querySelector('main').addEventListener('touchend',function(e){if(x0===null)return;
  var dx=e.changedTouches[0].clientX-x0,dy=e.changedTouches[0].clientY-y0;x0=null;
  if(Math.abs(dx)<70||Math.abs(dy)>Math.abs(dx)*0.6)return;
  var i=tabs.findIndex(function(b){return b.classList.contains('on')}),j=i+(dx<0?1:-1);
  if(j>=0&&j<tabs.length)show(tabs[j].dataset.t)},{passive:true});
 // tự tải lại mỗi 3 phút khi đang mở trang (giữ nguyên tab đang xem)
 setInterval(function(){if(!document.hidden)location.reload()},180000);
})();
</script>"""

LOG_JS = r"""<script>
(function(){
 var C = JSON.parse(document.getElementById('colors').textContent);
 document.querySelectorAll('#lt button').forEach(function(b){ b.onclick = function(){
   document.querySelectorAll('#lt button').forEach(function(x){ x.classList.toggle('on', x === b); });
   document.querySelectorAll('.lv').forEach(function(v){ v.style.display = v.id === 'lv-' + b.dataset.v ? 'block' : 'none'; });
   try { localStorage.setItem('keno-logview', b.dataset.v); } catch(e) {} }; });
 var saved = null; try { saved = localStorage.getItem('keno-logview'); } catch(e) {}
 var sb = document.querySelector('#lt button[data-v="' + (saved || 'draws') + '"]') || document.querySelector('#lt button');
 sb.click();
 function render(D){
   var box = document.getElementById('logbox'), info = document.getElementById('loginfo');
   if (!D || !D.rows) { box.innerHTML = '<p class="muted" style="padding:10px">Không tải được ngày này.</p>'; return; }
   var F = (document.getElementById('focuseng') || {}).textContent || '', fi = D.engines.indexOf(F), tot = 0;
   function usd(x){ return x ? '$' + Math.round(x).toLocaleString('en-US') : '—'; }
   var h = '<table><tr><th>Kỳ</th><th>Giờ</th><th></th><th>Thắng cao nhất / $1</th>' + (fi >= 0 ? '<th style="color:' + (C[F]||'') + '">Gợi ý ' + F + '</th>' : '')
         + D.engines.map(function(e){ return '<th style="color:' + (C[e]||'') + '">' + e + '</th>'; }).join('') + '</tr>';
   var rows = D.rows.slice().reverse(), sums = {}, cnt = {};
   rows.forEach(function(r){
     var bi = 0, P = r.p || []; P.forEach(function(v, i){ if ((v || 0) > (P[bi] || 0)) bi = i; });
     h += '<tr><td>#' + r.d + '</td><td>' + r.t + '</td><td class="muted">' + (r.k === 'l' ? 'L' : 'W') + '</td>';
     h += '<td class="' + (P[bi] >= 40 ? 'win' : 'muted') + '">' + (P[bi] ? usd(P[bi]) + ' <span style="color:' + (C[D.engines[bi]]||'') + '">' + D.engines[bi] + '</span>' : '—') + '</td>';
     if (fi >= 0) { var v = P[fi] || 0; tot += v; h += '<td class="' + (v >= 40 ? 'win' : v ? '' : 'muted') + '">' + usd(v) + '</td>'; }
     h += r.h.map(function(x, i){
       if (x === null || x === undefined) return '<td class="muted">—</td>';
       sums[i] = (sums[i] || 0) + x; cnt[i] = (cnt[i] || 0) + 1;
       return '<td' + (x >= 6 ? ' class="big"' : '') + '>' + x + (r.e[i] ? '★' : '') + '</td>'; }).join('') + '</tr>'; });
   h += '<tr class="sum"><td colspan="3">Cả ngày</td><td></td>' + (fi >= 0 ? '<td>' + usd(tot) + ' / $' + rows.length + '</td>' : '')
      + D.engines.map(function(e, i){ return '<td>' + (cnt[i] ? (sums[i]/cnt[i]).toFixed(2) : '—') + '</td>'; }).join('') + '</tr></table>';
   box.innerHTML = h; info.textContent = D.rows.length + ' kỳ';
   var dc = document.getElementById('daycsv'); dc.href = (dc.dataset.base || '') + 'logs/days/' + D.day + '.csv';
 }
 var today = JSON.parse(document.getElementById('logtoday').textContent || '{}'), sel = document.getElementById('logday');
 render(today);
 sel.onchange = function(){
   if (today.day === sel.value) return render(today);
   document.getElementById('logbox').innerHTML = '<p class="muted" style="padding:10px">Đang tải…</p>';
   fetch((document.getElementById('daycsv').dataset.base || '') + 'logs/days/' + sel.value + '.json', {cache: 'no-store'}).then(function(r){ return r.json(); }).then(render).catch(function(){ render(null); }); };
 function filt(){ var e = document.getElementById('bige').value, s = document.getElementById('bigs').value, n = 0;
   document.querySelectorAll('#bigt tr[data-e]').forEach(function(tr){ var ok = (!e || tr.dataset.e === e) && (!s || tr.dataset.s === s); tr.style.display = ok ? '' : 'none'; if (ok) n++; });
   document.getElementById('bigcount').textContent = n + ' lần'; }
 document.getElementById('bige').onchange = filt; document.getElementById('bigs').onchange = filt; filt();
})();
</script>"""

WALLET_JS = r"""<script>
(function(){
 var W; try { W = JSON.parse(document.getElementById('wvdata').textContent); } catch(e) { return; }
 var E = document.getElementById('wv-e'), Dd = document.getElementById('wv-d'), B = document.getElementById('wv-b'), spot = 10;
 try { var sv = JSON.parse(localStorage.getItem('keno-wv') || '{}'); if (sv.e) E.value = sv.e; if (sv.d && W[sv.d]) Dd.value = sv.d; if (sv.s) spot = sv.s; B.checked = !!sv.b; } catch(e) {}
 function usd(x){ return (x < 0 ? '−$' : '$') + Math.abs(Math.round(x)).toLocaleString('en-US'); }
 function render(){
   try { localStorage.setItem('keno-wv', JSON.stringify({e: E.value, d: Dd.value, s: spot, b: B.checked})); } catch(e) {}
   document.querySelectorAll('#wv-s button').forEach(function(b){ b.classList.toggle('on', +b.dataset.k === spot); });
   var cost = B.checked ? 2 : 1, col = B.checked ? 2 : 1, days = Object.keys(W).sort().filter(function(d){ return d >= Dd.value; });
   var bal = 0, put = 0, back = 0, n = 0, rows = [], pts = [0], best = 0, up = 0;
   days.forEach(function(d){
     var tk = 0, bk = 0;
     Object.keys(W[d]).forEach(function(en){ if (E.value !== '*' && en !== E.value) return; var a = (W[d][en] || {})[spot]; if (!a) return; tk += a[0]; bk += a[col]; });
     if (!tk) return;
     n += tk; put += tk * cost; back += bk; bal += bk - tk * cost; pts.push(bal); if (bk - tk * cost > 0) up++;
     best = Math.max(best, bk - tk * cost);
     rows.push('<tr><td>' + d.slice(8) + '/' + d.slice(5, 7) + '</td><td>' + tk.toLocaleString() + '</td><td>' + usd(tk * cost) + '</td><td>' + usd(bk) + '</td><td class="' + (bk - tk * cost >= 0 ? 'good' : 'bad') + '">' + (bk - tk * cost >= 0 ? '+' : '') + usd(bk - tk * cost) + '</td><td class="' + (bal >= 0 ? 'good' : 'bad') + '">' + (bal >= 0 ? '+' : '') + usd(bal) + '</td><td>' + (tk ? Math.round(bk / (tk * cost) * 100) : 0) + '%</td></tr>');
   });
   var roi = put ? back / put * 100 : 0;
   document.getElementById('wv-k').innerHTML =
     '<div><span>Lời / lỗ</span><b style="color:' + (bal >= 0 ? 'var(--green)' : 'var(--red)') + '">' + (bal >= 0 ? '+' : '') + usd(bal) + '</b><em>' + rows.length + ' ngày · ' + up + ' ngày lời</em></div>' +
     '<div><span>Tiền bỏ ra</span><b>' + usd(put) + '</b><em>' + n.toLocaleString() + ' vé × $' + cost + '</em></div>' +
     '<div><span>Tiền thắng về</span><b>' + usd(back) + '</b><em>trả về ' + roi.toFixed(1) + '% (lý thuyết ~63%)</em></div>' +
     '<div><span>Ngày tốt nhất</span><b>' + (best > 0 ? '+' + usd(best) : '—') + '</b><em>trung bình ' + (bal >= 0 ? '+' : '') + usd(rows.length ? bal / rows.length : 0) + '/ngày</em></div>';
   document.getElementById('wv-t').innerHTML = '<tr><th>Ngày</th><th>Vé</th><th>Bỏ ra</th><th>Thắng về</th><th>Lời/lỗ ngày</th><th>Số dư cộng dồn</th><th>Trả về</th></tr>' + rows.reverse().join('');
   var svg = document.getElementById('wv-c'), lo = Math.min.apply(null, pts.concat([0])), hi = Math.max.apply(null, pts.concat([0])), r = (hi - lo) || 1;
   function X(i){ return 10 + i * (780 / Math.max(1, pts.length - 1)); } function Y(v){ return 10 + (hi - v) / r * 150; }
   svg.innerHTML = '<line x1="10" x2="790" y1="' + Y(0) + '" y2="' + Y(0) + '" class="zero"/>' +
     '<polyline fill="none" stroke="' + (bal >= 0 ? '#3ddc97' : '#ff6b5b') + '" stroke-width="2.5" points="' + pts.map(function(v, i){ return X(i) + ',' + Y(v); }).join(' ') + '"/>' +
     '<text x="14" y="' + (Y(0) - 4) + '" class="ax">$0</text><text x="14" y="' + (Y(bal) + (bal < 0 ? 14 : -4)) + '" class="ax">' + usd(bal) + '</text>';
 }
 E.onchange = Dd.onchange = B.onchange = render;
 document.querySelectorAll('#wv-s button').forEach(function(b){ b.onclick = function(){ spot = +b.dataset.k; render(); }; });
 render();
 document.querySelectorAll('#wt button').forEach(function(b){ b.onclick = function(){
   document.querySelectorAll('#wt button').forEach(function(x){ x.classList.toggle('on', x === b); });
   document.getElementById('wv-all').style.display = b.dataset.v === 'all' ? 'block' : 'none';
   document.getElementById('wv-old').style.display = b.dataset.v === 'old' ? 'block' : 'none'; }; });
})();
</script>"""

UP_JS = r"""<script>
function asc(list, k){ return list.slice(0, k).map(Number).sort(function(a, b){ return a - b; }); }
(function(){
  var D; try { D = JSON.parse(document.getElementById('updata').textContent); } catch(e) { return; }
  var spot = 10; try { spot = +localStorage.getItem('kenoSpot') || 10; } catch(e) {}
  var box = document.getElementById('up');
  function tm(ms){ return new Date(ms).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',timeZone:'America/Los_Angeles'}); }
  function render(){
    var now = Date.now();
    var live = D.targets.filter(function(t){ return !t.eta || t.eta > now - 30000; }).slice(0, 5), stale = false;
    if (!live.length && D.fallback) {
      // các kỳ đã khóa đều đã quay: vẫn hiện bộ số mới nhất cho các kỳ kế tiếp (ước tính)
      stale = true;
      var k0 = D.latest_ms ? Math.max(1, Math.ceil((now - D.latest_ms) / 240000)) : 1;
      for (var i = 0; i < 5; i++) {
        var id = D.latest + k0 + i;
        live.push({draw: id, basis: D.fallback.basis, picks: D.fallback.picks, eta: D.latest_ms ? D.latest_ms + (id - D.latest) * 240000 : null});
      }
    }
    if (!live.length) { box.innerHTML = '<div class="grp muted">Chưa có bộ số nào được khóa.</div>'; return; }
    var groups = {}; live.forEach(function(t){ (groups[t.basis] = groups[t.basis] || []).push(t); });
    var html = '';
    Object.keys(groups).forEach(function(b){
      var ts = groups[b], p = ts[0].picks, s = p[D.suggested] ? D.suggested : Object.keys(p)[0];
      html += '<div class="grp"><div class="chips">' + ts.map(function(t, i){
        var left = t.eta ? Math.max(0, Math.round((t.eta - now) / 1000)) : null;
        var lab = '#' + t.draw + (t.eta ? ' · ~' + tm(t.eta) + (left !== null && left < 600 ? ' · còn ' + Math.floor(left/60) + ':' + ('0'+left%60).slice(-2) : '') : '');
        return '<span class="chip' + (i === 0 ? ' now' : '') + '">' + lab + '</span>'; }).join('') + '</div>';
      html += '<div class="muted" style="font-size:12px">GỢI Ý · <b style="color:' + D.colors[s] + '">' + s + '</b> · vé ' + spot + ' số · khóa từ kỳ #' + b + '</div>';
      if (stale) html += '<div class="stale">Bộ số mới nhất · dùng tiếp cho các kỳ kế (số kỳ &amp; giờ là ước tính). Bộ mới sẽ thay sau lần chạy GitHub kế tiếp.</div>';
      html += '<div class="sug">' + asc(p[s], spot).map(function(n){ return '<div class="ball">' + n + '</div>'; }).join('') + '</div>';
      html += '<div class="spots">' + [5,6,7,8,9,10].map(function(k){ return '<button data-k="' + k + '" class="' + (k === spot ? 'on' : '') + '">' + k + ' số</button>'; }).join('') + '</div>';
      html += '<div class="others">' + D.order.filter(function(e){ return e !== s && p[e]; }).map(function(e){
        return '<div><b style="color:' + D.colors[e] + '">' + e + '</b>' + asc(p[e], spot).join(' ') + '</div>'; }).join('') + '</div></div>';
    });
    box.innerHTML = html;
    box.querySelectorAll('.spots button').forEach(function(bt){ bt.onclick = function(){ spot = +bt.dataset.k; try { localStorage.setItem('kenoSpot', spot); } catch(e) {} render(); }; });
  }
  render(); setInterval(render, 1000);
})();
</script>
"""


def build(con, cfg, archive_day=None):
    os.makedirs(OUT, exist_ok=True)
    alpha = cfg.get("significance_alpha", 0.05)
    tab = engine_table(con, alpha)
    rnd = randomness(con)
    days = daily(con)
    model = kdb.meta_get(con, "pattern_model")
    status = {}
    try:
        with open(os.path.join(HERE, "data", "status.json"), encoding="utf-8") as f:
            status = json.load(f)
    except Exception:
        pass
    live = con.execute("SELECT COUNT(*), MAX(captured_at) FROM draws WHERE source IN ('page','api')").fetchone()
    hist_n = con.execute("SELECT COUNT(*), (SELECT draw_date FROM draws WHERE source='history' ORDER BY draw_id LIMIT 1) "
                         "FROM draws WHERE source='history'").fetchone()
    imported = con.execute("SELECT COUNT(*) FROM draws WHERE source='import'").fetchone()[0]
    rounds = con.execute("SELECT COUNT(DISTINCT target_draw) FROM results WHERE valid=1").fetchone()[0]
    try:
        kinds = dict(con.execute("""SELECT p.kind, COUNT(DISTINCT r.target_draw) FROM results r JOIN predictions p
                                     ON p.target_draw=r.target_draw AND p.engine=r.engine WHERE r.valid=1 GROUP BY p.kind""").fetchall())
    except Exception:
        kinds = {}
    kinds_txt = f"vé khóa trực tiếp: {kinds.get('live', 0):,} kỳ · vé walk-forward (dữ liệu về theo lô): {kinds.get('walkforward', 0):,} kỳ"
    nxt = con.execute("SELECT target_draw, engine, ranked, created_at FROM predictions WHERE target_draw=(SELECT MAX(target_draw) FROM predictions)").fetchall()

    e = html.escape
    # headline verdict
    big = [r for r in tab if r["engine"] != "RANDOM" and r["n"] >= 500]
    good = [r for r in big if r["cls"] == "good"]
    if not big:
        head = f"Đang thu thập — mới có {rounds} kỳ được chấm. Cần ≥ 500 kỳ để kết luận."
    elif good:
        head = (f"{len(good)} tổ hợp vượt ngẫu nhiên sau hiệu chỉnh. Hãy coi đây là giả thuyết: "
                "xóa kết quả cũ trong đầu và xem nó có lặp lại trên 2.000 kỳ tiếp theo không.")
    else:
        head = (f"Sau {rounds:,} kỳ được chấm: không engine nào vượt ngẫu nhiên. "
                "Đây đúng là điều một máy quay số công bằng sẽ cho ra.")

    rows_html = []
    for r in tab:
        rows_html.append(
            f"<tr class='{r['cls']}'><td><b style='color:{COLORS.get(r['engine'])}'>{r['engine']}</b></td><td>{r['spot']}</td>"
            f"<td>{r['n']:,}</td><td>{r['mean']:.3f}</td><td>{r['exp']:.2f}</td><td>{r['z']:+.2f}</td>"
            f"<td>{fmt_p(r['p'])}</td><td>{fmt_p(r['p_adj'])}</td>"
            f"<td>{r['ret']*100:.1f}% <span class='muted'>/ {r['theo']*100:.1f}%</span></td>"
            f"<td>{r['perfect']} <span class='muted'>/ {r['perfect_exp']:.2f}</span></td><td>{e(r['verdict'])}</td></tr>")

    day_rows = []
    names = list(engines.ENGINES)
    for d in days:
        cells = "".join(f"<td>{d['eng'][n]:.2f}</td>" if n in d["eng"] else "<td class='muted'>—</td>" for n in names)
        miss = d["span"] - d["draws"]
        day_rows.append(f"<tr><td>{d['date']}</td><td>{d['draws']}</td><td>{'<span class=warnt>'+str(miss)+'</span>' if miss else 0}</td>"
                        f"<td>{d['rounds']}</td>{cells}</tr>")

    rnd_html = "<p class='muted'>Cần ≥ 50 kỳ.</p>"
    if rnd:
        rnd_html = (
            f"<div class='kpis'>"
            f"<div><span>Chi-square 80 số</span><b>p = {fmt_p(rnd['chi_p'])}</b><em>{rnd['draws']:,} kỳ · p &gt; 0.05 = phân bố đều</em></div>"
            f"<div><span>Bulls-Eye đều?</span><b>p = {fmt_p(rnd['be_p'])}</b><em>{rnd['be_n']:,} kỳ có Bulls-Eye</em></div>"
            f"<div><span>Trùng với kỳ trước</span><b>{(rnd['overlap'] or 0):.3f}</b><em>kỳ vọng 5.000 · z = {(rnd['overlap_z'] or 0):+.2f}</em></div>"
            f"<div><span>Số ra nhiều nhất</span><b>{', '.join(str(t[0]) for t in rnd['top'])}</b><em>{', '.join(str(t[2])+'%' for t in rnd['top'])} (kỳ vọng 25%)</em></div>"
            f"</div>")

    model_html = "<p class='muted'>Chưa train (cần ≥ 200 kỳ).</p>"
    if model:
        mrows = "".join(f"<tr><td>{e(f)}</td><td>{w:+.4f}</td><td><div class='bar' style='width:{min(abs(w)*800,100):.0f}%;background:{'#3ddc97' if w>0 else '#ff6b5b'}'></div></td></tr>"
                        for f, w in zip(engines.FEATURES, model["w"]))
        model_html = (f"<p class='muted'>Train lại lúc {e(model.get('trained_at',''))} trên {model['trained_on']:,} kỳ. "
                      "Trọng số gần 0 nghĩa là tín hiệu đó không giúp đoán kỳ sau.</p>"
                      f"<table class='small'><tr><th>Tín hiệu</th><th>Trọng số</th><th></th></tr>{mrows}</table>")

    next_html = ""
    if nxt:
        cards = "".join(f"<div class='pick'><b style='color:{COLORS.get(en)}'>{en}</b><span>{' '.join(str(n) for n in json.loads(rk))}</span></div>"
                        for _, en, rk, _ in nxt)
        next_html = (f"<h2>Vé đã khóa cho kỳ #{nxt[0][0]}</h2><p class='muted'>Khóa lúc {e(nxt[0][3])}, trước khi kỳ quay diễn ra. "
                     f"Vé k số = k số đầu tiên.</p><div class='picks'>{cards}</div>")

    sug, sug_row = suggested_engine(tab)
    focus = cfg.get("focus_engine")
    if focus and focus in engines.ENGINES:
        sug, sug_row = focus, next((r for r in tab if r["engine"] == focus and r["spot"] == 10), None)
    up = upcoming(con)
    up["suggested"] = sug
    up["order"] = list(engines.ENGINES)
    up["colors"] = COLORS
    sug_txt = (f"Gợi ý = engine bạn chọn theo dõi: <b>{sug}</b> (z = {sug_row['z']:+.2f}, {sug_row['n']:,} kỳ — {e(sug_row['verdict'])})." if focus and sug_row and sug == focus else
               f"Gợi ý = engine có thành tích vé 10 số tốt nhất tới giờ: <b>{sug}</b> "
               f"(z = {sug_row['z']:+.2f}, {sug_row['n']:,} kỳ — {e(sug_row['verdict'])})." if sug_row else
               "Chưa đủ 200 kỳ để xếp hạng — tạm gợi ý HOT.")
    upcoming_html = f"""<h2>Số gợi ý · các kỳ sắp tới</h2>
<div id="up" class="up"></div>
<p class="note">{sug_txt} Mỗi bộ số chỉ dùng dữ liệu tới kỳ được ghi trên thẻ và dùng chung cho tối đa {cfg.get('lock_horizon', 8)} kỳ kế tiếp
(giống vé Hot Spot nhiều kỳ). Giờ quay là ước tính (4 phút/kỳ). Kỳ đã qua tự ẩn. Không engine nào được chứng minh là đoán tốt hơn ngẫu nhiên —
mỗi vé $1 trung bình chỉ trả lại khoảng 62¢.</p>
<script type="application/json" id="updata">{json.dumps(up)}</script>"""
    log_rows = []
    for L in prediction_log(con):
        cells = "".join(
            (f"<td class='big'>{L['hits'][n]}</td>" if L['hits'].get(n, 0) >= 6 else f"<td>{L['hits'].get(n, '—')}</td>")
            for n in engines.ENGINES)
        b = L["best"]
        best_txt = f"{b[0]} {b[2]}/{b[1]} · ${b[3] + b[4]:,.0f}" if b and (b[3] + b[4]) > 0 else "—"
        log_rows.append(f"<tr><td>#{L['draw']}</td><td>{e(str(L['date'] or ''))}</td><td class='muted'>{'trực tiếp' if L['kind']=='live' else 'walk-fwd'}</td>"
                        f"{cells}<td>{best_txt}</td></tr>")
    log_html = (f"<h2>Nhật ký dự đoán · 40 kỳ gần nhất (số trúng trên vé 10 số)</h2>"
                f"<div class='scroll'><table><tr><th>Kỳ</th><th>Giờ quay</th><th>Loại vé</th>"
                + "".join(f"<th>{n}</th>" for n in engines.ENGINES) +
                f"<th>Thắng lớn nhất / $1</th></tr>{''.join(log_rows)}</table></div>"
                "<p class='note'>Ô xanh = trúng ≥ 6/10. Kỳ vọng ngẫu nhiên 2.5/10.</p>")

    A = archive(con)
    tt_html = time_html(con, list(engines.ENGINES))
    wallet = wallet_html(con, cfg)
    lm = kdb.meta_get(con, "learned_model")
    learned_html = ""
    if lm:
        import learn
        lrows = "".join(f"<tr><td>{e(f)}</td><td>{w:+.3f}</td><td><div class='bar' style='width:{min(abs(w)*120,100):.0f}%;background:{'#3ddc97' if w>0 else '#ff6b5b'}'></div></td></tr>"
                        for f, w in sorted(zip(learn.SIGNALS, lm["w"]), key=lambda x: -abs(x[1])))
        learned_html = (f"<h2>LEARNED · công thức tự học</h2><p class='muted'>Học lại lúc {e(lm.get('trained_at',''))} trên "
                        f"{lm['trained_on']:,} kỳ thật. Điểm trên dữ liệu đã học: {lm['train_score']:.3f} — con số này luôn đẹp vì "
                        "vòng lặp chọn đúng cái khớp quá khứ nhất; chỉ bảng điểm live phía trên mới cho biết nó có thật sự đoán được không.</p>"
                        f"<table class='small'><tr><th>Tín hiệu</th><th>Trọng số</th><th></th></tr>{lrows}</table>")

    theo = " · ".join(f"{k} số: {theo_return(k)*100:.1f}%" for k in cfg.get("spots", [5, 6, 7, 8, 9, 10]))
    page = f"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>KenoAuto Report</title><meta name="theme-color" content="#07090b">
<style>
:root{{--bg:#07090b;--panel:#0d1116;--line:#1b2127;--fg:#d7dee4;--muted:#7d8a95;--green:#3ddc97;--amber:#f5a524;--red:#ff6b5b}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-monospace,Consolas,monospace}}
main{{max-width:1180px;margin:0 auto;padding:20px 16px 60px}}h1{{font-size:20px;margin:0 0 4px;color:var(--green);letter-spacing:.06em}}
h2{{font-size:13px;letter-spacing:.14em;color:var(--muted);margin:30px 0 10px;text-transform:uppercase}}
.head{{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--green);padding:14px 16px;border-radius:6px;font-size:15px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}}.kpis div{{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:12px}}
.kpis span{{display:block;color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase}}.kpis b{{display:block;font-size:20px;margin:4px 0}}.kpis em{{font-style:normal;color:var(--muted);font-size:12px}}
.scroll{{overflow-x:auto;border:1px solid var(--line);border-radius:6px}}table{{border-collapse:collapse;width:100%;font-size:12.5px}}
th,td{{padding:6px 9px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}}th{{color:var(--muted);font-weight:500;background:var(--panel);position:sticky;top:0}}
td:first-child,th:first-child,td:last-child,th:last-child{{text-align:left}}tr.good td:last-child{{color:var(--green)}}tr.warn td:last-child{{color:var(--amber)}}tr.flat td:last-child{{color:var(--muted)}}
.muted{{color:var(--muted)}}.warnt{{color:var(--amber)}}.chart{{width:100%;height:auto;background:var(--panel);border:1px solid var(--line);border-radius:6px}}
.chart .band{{stroke:#5a4415;stroke-dasharray:4 4}}.chart .zero{{stroke:#2a323a}}.chart .ax{{fill:var(--muted);font-size:11px}}
.legend{{display:flex;flex-wrap:wrap;gap:14px;margin-top:8px;font-size:12px}}.legend i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}}
.picks{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px}}.pick{{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:9px 12px}}.pick span{{display:block;margin-top:3px;letter-spacing:.04em}}
.bar{{height:8px;border-radius:2px}}
td.big{{color:#05100b;background:var(--green);font-weight:700}}
.up{{display:grid;gap:10px}}.grp{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}}
.chips{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}}.chip{{border:1px solid var(--line);border-radius:14px;padding:3px 10px;font-size:12px}}.chip.now{{border-color:var(--green);color:var(--green)}}
.sug{{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 4px}}.ball{{width:44px;height:44px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:17px;font-weight:700;background:#132a20;color:var(--green);border:2px solid var(--green)}}
.spots{{display:flex;flex-wrap:wrap;gap:4px;margin:10px 0}}.spots button{{white-space:nowrap;background:var(--bg);color:var(--muted);border:1px solid var(--line);border-radius:4px;padding:4px 10px;font:inherit;cursor:pointer}}.spots button.on{{color:var(--green);border-color:var(--green)}}
.others{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:4px 16px;margin-top:10px;font-size:12.5px}}.others b{{display:inline-block;width:92px}}table.small td:last-child{{width:40%}}.note{{color:var(--muted);font-size:12.5px;max-width:880px}}
{TAB_CSS}
</style></head><body>
<header class="top">
<div class="brand"><h1>KENOAUTO · HOT SPOT</h1><span id="dev" class="dev"></span></div>
<details class="meta"><summary>Cập nhật {kdb.now_str()} · {live[0] + hist_n[0]:,} kỳ</summary>
<div class="muted">{live[0]:,} kỳ log tự động (mới nhất {e(str(live[1]))}) · {imported:,} kỳ cũ nhập · {hist_n[0]:,} kỳ lịch sử tải ngược{(' (từ ' + e(str(hist_n[1])) + ')') if hist_n[0] else ''} · trạng thái: {e(str(status.get('state','?')))} ({e(str(status.get('mode','')))}{' · GitHub' if status.get('runner')=='github' else ''})<br>{e(kinds_txt)}</div></details>
<nav class="tabs" id="tabs">
<button data-t="sug">🎯 Gợi ý</button><button data-t="zf">🔲 {e(cfg.get('focus_engine') or 'Theo dõi')}</button><button data-t="log">📒 Nhật ký & lưu trữ</button><button data-t="eng">🏁 Engine</button>
<button data-t="pre">🧪 Kiểm định</button><button data-t="wal">💵 Ví ảo</button><button data-t="rnd">🎲 Ngẫu nhiên</button>
<button data-t="lab">🔬 Lab</button><button data-t="time">🕒 Giờ & ngày</button><button data-t="ai">🧠 AI học</button>
</nav></header>
<main>
<section class="pane" id="t-sug">{upcoming_html}</section>
<section class="pane" id="t-zf">{zfocus_html(con, cfg)}</section>
<section class="pane" id="t-log">{archive_html(A)}</section>
<section class="pane" id="t-eng">
<div class="head">{e(head)}</div>
<h2>Bảng điểm engine (chỉ vé khóa trước kỳ quay)</h2>
<div class="scroll"><table><tr><th>Engine</th><th>Số</th><th>Kỳ</th><th>TB trúng</th><th>Kỳ vọng</th><th>z</th><th>p</th><th>p hiệu chỉnh</th><th>Trả về / $1 (lý thuyết)</th><th>Trúng hết (kỳ vọng)</th><th>Đánh giá</th></tr>{''.join(rows_html)}</table></div>
<p class="note">p hiệu chỉnh dùng Holm-Bonferroni vì đang thử {len([r for r in tab if r['engine']!='RANDOM'])} tổ hợp cùng lúc. Tỷ lệ trả về lý thuyết của vé Hot Spot $1: {theo}.</p>
<h2>z-score tích lũy · vé 10 số</h2>{zchart(con)}
<p class="note">Đường nằm trong dải ±1.96 là bình thường cho may rủi. Một engine có lợi thế thật sẽ đi lên đều đặn và ở lại phía trên.</p>
</section>
<section class="pane" id="t-pre">{prereg_html(con, cfg)}</section>
<section class="pane" id="t-wal"><div class="subtabs" id="wt"><button data-v="all" class="on">Đánh mọi kỳ theo gợi ý</button><button data-v="old">3 ví $100/ngày</button></div>
<div id="wv-all">{every_round_html(A, cfg, sug)}</div><div id="wv-old" style="display:none">{wallet}</div></section>
<section class="pane" id="t-rnd"><h2>Máy quay có ngẫu nhiên không?</h2>{rnd_html}</section>
<section class="pane" id="t-lab">{lab_html(con, cfg)}</section>
<section class="pane" id="t-time">{tt_html}
<h2>Theo ngày · TB trúng vé 10 số (kỳ vọng 2.50)</h2>
<div class="scroll"><table><tr><th>Ngày</th><th>Kỳ log</th><th>Bỏ lỡ</th><th>Kỳ chấm</th>{''.join(f'<th>{n}</th>' for n in names)}</tr>{''.join(day_rows)}</table></div>
</section>
<section class="pane" id="t-ai">{learned_html}<h2>PATTERN · AI học được gì</h2>{model_html}</section>
<script type="application/json" id="colors">{json.dumps(COLORS)}</script>
<script type="application/json" id="focuseng">{e(sug or '')}</script>
{UP_JS}
{LOG_JS}
{WALLET_JS}
{TAB_JS}
</main></body></html>"""
    import gate
    gm = gate.mode()
    if gm != "full":
        # Người xem thường chỉ thấy tab Gợi ý; toàn bộ báo cáo được mã hóa, chỉ admin có mật khẩu mới mở được
        admin_page = page.replace('<span id="dev" class="dev"></span>',
                                  f'<span style="display:flex;gap:8px;align-items:center"><span id="dev" class="dev"></span>{gate.LOGOUT_BTN}</span>', 1)
        payload = gate.encrypt(admin_page, gate.password()) if gm == "locked" else None
        extra = (f"<script type=\"application/json\" id=\"colors\">{json.dumps(COLORS)}</script>\n"
                 f"<script type=\"application/json\" id=\"focuseng\">{e(sug or '')}</script>\n{UP_JS}\n{TAB_JS}")
        page = gate.public_page(page, upcoming_html, extra, kdb.now_str(), payload)
        if gm == "public":
            print("WARNING: KENO_ADMIN_PASSWORD is not set — published the Suggestion tab only (admin tabs left out).")
    path = os.path.join(OUT, "latest.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": kdb.now_str(), "rounds": rounds, "engines": tab, "randomness": rnd, "days": days},
                  f, indent=1, default=str)
    if archive_day:
        shutil.copy(path, os.path.join(OUT, f"report-{archive_day:%Y-%m-%d}.html"))
    return path
