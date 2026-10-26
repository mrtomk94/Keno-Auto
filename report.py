"""Builds reports/latest.html — the honest scoreboard: every engine vs pure chance."""
import datetime as dt
import html
import json
import math
import os
import shutil

import engines
import kdb

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "reports")
COLORS = {"RANDOM": "#8b98a3", "HOT": "#ff6b5b", "GAP": "#f5a524", "CLUSTER": "#3ddc97",
          "BLOCK": "#2196c9", "MARKOV": "#a78bfa", "PATTERN": "#f472b6", "DEVIATION": "#e5e7eb"}


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
    return dict(draws=T, chi=chi, chi_p=chi2_p(chi, 79), be_n=len(be), be_chi=be_chi, be_p=be_p,
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
    return {"latest": base_id, "latest_ms": base_ms, "targets": list(targets.values())}


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


UP_JS = r"""<script>
(function(){
  var D; try { D = JSON.parse(document.getElementById('updata').textContent); } catch(e) { return; }
  var spot = 10; try { spot = +localStorage.getItem('kenoSpot') || 10; } catch(e) {}
  var box = document.getElementById('up');
  function tm(ms){ return new Date(ms).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',timeZone:'America/Los_Angeles'}); }
  function render(){
    var now = Date.now();
    var live = D.targets.filter(function(t){ return !t.eta || t.eta > now - 30000; }).slice(0, 5);
    if (!live.length) { box.innerHTML = '<div class="grp muted">Các kỳ đã khóa đều đã quay xong. Bộ số mới sẽ có sau lần cập nhật kế tiếp (thường trong ~15 phút) — trang tự tải lại.</div>'; return; }
    var groups = {}; live.forEach(function(t){ (groups[t.basis] = groups[t.basis] || []).push(t); });
    var html = '';
    Object.keys(groups).forEach(function(b){
      var ts = groups[b], p = ts[0].picks, s = D.suggested;
      html += '<div class="grp"><div class="chips">' + ts.map(function(t, i){
        var left = t.eta ? Math.max(0, Math.round((t.eta - now) / 1000)) : null;
        var lab = '#' + t.draw + (t.eta ? ' · ~' + tm(t.eta) + (left !== null && left < 600 ? ' · còn ' + Math.floor(left/60) + ':' + ('0'+left%60).slice(-2) : '') : '');
        return '<span class="chip' + (i === 0 ? ' now' : '') + '">' + lab + '</span>'; }).join('') + '</div>';
      html += '<div class="muted" style="font-size:12px">GỢI Ý · <b style="color:' + D.colors[s] + '">' + s + '</b> · vé ' + spot + ' số · khóa từ kỳ #' + b + '</div>';
      html += '<div class="sug">' + p[s].slice(0, spot).map(function(n){ return '<div class="ball">' + n + '</div>'; }).join('') + '</div>';
      html += '<div class="spots">' + [5,6,7,8,9,10].map(function(k){ return '<button data-k="' + k + '" class="' + (k === spot ? 'on' : '') + '">' + k + ' số</button>'; }).join('') + '</div>';
      html += '<div class="others">' + D.order.filter(function(e){ return e !== s && p[e]; }).map(function(e){
        return '<div><b style="color:' + D.colors[e] + '">' + e + '</b>' + p[e].slice(0, spot).join(' ') + '</div>'; }).join('') + '</div></div>';
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
    up = upcoming(con)
    up["suggested"] = sug
    up["order"] = list(engines.ENGINES)
    up["colors"] = COLORS
    sug_txt = (f"Gợi ý = engine có thành tích vé 10 số tốt nhất tới giờ: <b>{sug}</b> "
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

    theo = " · ".join(f"{k} số: {theo_return(k)*100:.1f}%" for k in cfg.get("spots", [5, 6, 7, 8, 9, 10]))
    page = f"""<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>KenoAuto Report</title><meta http-equiv="refresh" content="180">
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
.others{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:4px 16px;margin-top:10px;font-size:12.5px}}.others b{{display:inline-block;width:92px}}table.small td:last-child{{width:40%}}.note{{color:var(--muted);font-size:12.5px;max-width:880px}}
</style></head><body><main>
<h1>KENOAUTO · HOT SPOT</h1>
<div class="muted">Cập nhật {kdb.now_str()} · {live[0]:,} kỳ log tự động (mới nhất {e(str(live[1]))}) · {imported:,} kỳ cũ nhập · {hist_n[0]:,} kỳ lịch sử tải ngược{(' (từ ' + e(str(hist_n[1])) + ')') if hist_n[0] else ''} · trạng thái: {e(str(status.get('state','?')))} ({e(str(status.get('mode','')))}{' · GitHub' if status.get('runner')=='github' else ''})<br>{e(kinds_txt)}</div>
{upcoming_html}
<h2>Kết luận</h2><div class="head">{e(head)}</div>
<h2>Máy quay có ngẫu nhiên không?</h2>{rnd_html}
<h2>z-score tích lũy · vé 10 số</h2>{zchart(con)}
<p class="note">Đường nằm trong dải ±1.96 là bình thường cho may rủi. Một engine có lợi thế thật sẽ đi lên đều đặn và ở lại phía trên; đường chạm dải rồi quay về 0 là nhiễu.</p>
<h2>Bảng điểm engine (chỉ vé khóa trước kỳ quay)</h2>
<div class="scroll"><table><tr><th>Engine</th><th>Số</th><th>Kỳ</th><th>TB trúng</th><th>Kỳ vọng</th><th>z</th><th>p</th><th>p hiệu chỉnh</th><th>Trả về / $1 (lý thuyết)</th><th>Trúng hết (kỳ vọng)</th><th>Đánh giá</th></tr>{''.join(rows_html)}</table></div>
<p class="note">p hiệu chỉnh dùng Holm-Bonferroni vì đang thử {len([r for r in tab if r['engine']!='RANDOM'])} tổ hợp cùng lúc — thử càng nhiều, càng dễ có cái "trông nóng" do may. Tỷ lệ trả về lý thuyết của vé Hot Spot $1: {theo}.</p>
<h2>Theo ngày · TB trúng vé 10 số (kỳ vọng 2.50)</h2>
<div class="scroll"><table><tr><th>Ngày</th><th>Kỳ log</th><th>Bỏ lỡ</th><th>Kỳ chấm</th>{''.join(f'<th>{n}</th>' for n in names)}</tr>{''.join(day_rows)}</table></div>
{log_html}
<h2>PATTERN · AI học được gì</h2>{model_html}
{UP_JS}
</main></body></html>"""
    path = os.path.join(OUT, "latest.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"generated": kdb.now_str(), "rounds": rounds, "engines": tab, "randomness": rnd, "days": days},
                  f, indent=1, default=str)
    if archive_day:
        shutil.copy(path, os.path.join(OUT, f"report-{archive_day:%Y-%m-%d}.html"))
    return path
