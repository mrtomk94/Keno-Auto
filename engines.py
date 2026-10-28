"""Prediction engines. Each returns a ranked list of 10 numbers; a k-spot ticket = the first k.

Every engine only receives draws up to and including the basis draw, so a ticket can
never "see" the draw it is scored against.
"""
import math
import random
import secrets

import numpy as np

ROWS, COLS = 8, 10
FEATURES = ["freq z (30)", "gap (log)", "lag-1", "lag-2", "lag-3", "pair affinity",
            "row momentum", "col momentum", "10-draw burst", "gap dueness"]
_rng = random.Random(secrets.randbits(64))


def to_matrix(hist):
    X = np.zeros((len(hist), 80), dtype=np.float32)
    for i, (_, nums, _) in enumerate(hist):
        X[i, [n - 1 for n in nums]] = 1
    return X


def rc(n):
    return (n - 1) // COLS, (n - 1) % COLS


def neighbors(n):
    r, c = rc(n)
    out = []
    for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        rr, cc = r + dr, c + dc
        if 0 <= rr < ROWS and 0 <= cc < COLS:
            out.append(rr * COLS + cc + 1)
    return out


def rank(scores, k=10):
    """Top-k numbers by score, random tie-break."""
    jitter = np.array([_rng.random() * 1e-6 for _ in range(80)])
    order = np.argsort(-(np.asarray(scores, dtype=float) + jitter))
    return [int(i) + 1 for i in order[:k]]


def gaps(X):
    T = X.shape[0]
    g = np.full(80, T, dtype=float)
    for n in range(80):
        idx = np.nonzero(X[:, n])[0]
        if len(idx):
            g[n] = T - 1 - idx[-1]
    return g


# ---------------------------------------------------------------- engines
def e_random(X, ctx):
    return _rng.sample(range(1, 81), 10)


def e_hot(X, ctx):
    w = ctx.get("hot_window", 30)
    return rank(X[-w:].sum(0))


def e_gap(X, ctx):
    return rank(gaps(X))


def heat(X):
    return X[-6:].sum(0) + 0.5 * X[-30:].sum(0) / 5.0


def e_cluster(X, ctx):
    h = heat(X)
    seed = rank(h, 1)[0]
    chosen = [seed]
    while len(chosen) < 10:
        frontier = {m for c in chosen for m in neighbors(c)} - set(chosen)
        best = max(frontier, key=lambda m: (h[m - 1], _rng.random()))
        chosen.append(best)
    return chosen


def e_block(X, ctx):
    h = heat(X)
    best, best_s = None, -1
    for c in range(COLS - 1):
        for r in range(ROWS - 4):
            cells = [(r + i) * COLS + c + j + 1 for i in range(5) for j in range(2)]
            s = sum(h[m - 1] for m in cells)
            if s > best_s:
                best, best_s = cells, s
    return sorted(best, key=lambda m: (-h[m - 1], _rng.random()))


def e_markov(X, ctx):
    if X.shape[0] < 50:
        return e_random(X, ctx)
    A, B = X[:-1], X[1:]
    trans = (A.T @ B) / np.maximum(A.sum(0)[:, None], 1)  # P(j next | i now)
    last = X[-1].astype(bool)
    return rank(trans[last].sum(0))


def e_deviation(X, ctx):
    p = 0.25
    score = np.zeros(80)
    signs = []
    for w, wt in ((30, 0.5), (90, 0.3), (X.shape[0], 0.2)):
        n = min(w, X.shape[0])
        c = X[-n:].sum(0)
        z = (c - n * p) / math.sqrt(n * p * (1 - p))
        z = z * n / (n + 30)  # shrink small windows toward 0
        score += wt * z
        signs.append(np.sign(z))
    agree = (signs[0] == signs[1]) & (signs[1] == signs[2])
    score = np.where(agree, score * 1.3, score)
    # greedy pick with co-occurrence penalty
    co = X[-300:].T @ X[-300:]
    np.fill_diagonal(co, 0)
    co = co / max(co.max(), 1)
    chosen = []
    s = score.copy()
    for _ in range(10):
        pick = rank(s, 1)[0]
        chosen.append(pick)
        s = s - 0.5 * co[pick - 1]
        s[pick - 1] = -1e9
    return chosen


# ---------------------------------------------------------------- PATTERN (learning engine)
def features(X, cooc=None):
    """Feature tensor F[t, n, f] describing number n using draws 0..t (inclusive)."""
    T = X.shape[0]
    cs = np.vstack([np.zeros((1, 80), np.float32), np.cumsum(X, 0)])

    def win(w):
        lo = np.maximum(np.arange(T) + 1 - w, 0)
        return cs[np.arange(T) + 1] - cs[lo], (np.arange(T) + 1 - lo)[:, None]

    c30, n30 = win(30)
    c10, n10 = win(10)
    c5, n5 = win(5)
    fz = (c30 - n30 * 0.25) / np.sqrt(n30 * 0.1875)
    burst = (c10 - n10 * 0.25) / np.sqrt(n10 * 0.1875)
    # gap since last appearance, computed forward
    g = np.zeros((T, 80), np.float32)
    run = np.full(80, 20.0, np.float32)
    for t in range(T):
        run = np.where(X[t] > 0, 0, run + 1)
        g[t] = run
    lag1 = X
    lag2 = np.vstack([np.zeros((1, 80)), X[:-1]])
    lag3 = np.vstack([np.zeros((2, 80)), X[:-2]])[:T]
    if cooc is None:
        cooc = X.T @ X
        np.fill_diagonal(cooc, 0)
        cooc = cooc / max(cooc.sum(1).mean(), 1)
    pair = X @ cooc
    pair = (pair - pair.mean(1, keepdims=True)) / (pair.std(1, keepdims=True) + 1e-9)
    rowsum = c5.reshape(T, ROWS, COLS).sum(2)  # per-row count last 5 draws
    colsum = c5.reshape(T, ROWS, COLS).sum(1)
    rowm = np.repeat(rowsum, COLS, axis=1)
    rowm = (rowm - n5 * 2.5) / np.sqrt(n5 * 2.5 + 1e-9)
    colm = np.tile(colsum, (1, ROWS))
    colm = (colm - n5 * 2.0) / np.sqrt(n5 * 2.0 + 1e-9)
    F = np.stack([fz, np.log1p(g), lag1, lag2, lag3, pair, rowm, colm, burst,
                  np.clip((g - 4) / 4, -1, 5)], axis=2).astype(np.float32)
    return F, cooc


def train_pattern(X, iters=300, lr=0.5):
    """Logistic regression: will number n appear in the NEXT draw? Trained on X only."""
    if X.shape[0] < 200:
        return None
    X = X[-2000:]
    F, cooc = features(X)
    Fx = F[:-1].reshape(-1, len(FEATURES))
    y = X[1:].reshape(-1)
    mu, sd = Fx.mean(0), Fx.std(0) + 1e-9
    Z = (Fx - mu) / sd
    w = np.zeros(Z.shape[1])
    b = math.log(0.25 / 0.75)
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(Z @ w + b)))
        gw = Z.T @ (p - y) / len(y) + 1e-3 * w
        gb = (p - y).mean()
        w -= lr * gw
        b -= lr * gb
    return {"w": w.tolist(), "b": float(b), "mu": mu.tolist(), "sd": sd.tolist(),
            "trained_on": int(X.shape[0])}


def e_pattern(X, ctx):
    model = ctx.get("pattern_model")
    if not model or X.shape[0] < 50:
        return e_random(X, ctx)
    Xw = X[-400:]
    F, _ = features(Xw)
    f = (F[-1] - np.array(model["mu"])) / np.array(model["sd"])
    return rank(f @ np.array(model["w"]) + model["b"])


def e_learned(X, ctx):
    model = ctx.get("learned_model")
    if not model or X.shape[0] < 400:
        return e_hot(X, ctx)
    import learn
    return learn.predict(X, model)


# ---------------------------------------------------------------- 5 board zones (20 numbers each)
# 1 = top-left, 2 = top-right, 3 = bottom-left, 4 = bottom-right (rows 1-4 / 5-8 × cols 1-5 / 6-10),
# 5 = centre (rows 3-6 × cols 3-7: 23-27, 33-37, 43-47, 53-57) — overlaps the other four.
def _zone(r0, c0, rows=4, cols=5):
    return np.array([r * COLS + c for r in range(r0, r0 + rows) for c in range(c0, c0 + cols)])


ZONES = [_zone(0, 0), _zone(0, 5), _zone(4, 0), _zone(4, 5), _zone(2, 2)]
ZONE_NAMES = ["vùng 1 trên-trái", "vùng 2 trên-phải", "vùng 3 dưới-trái", "vùng 4 dưới-phải", "vùng 5 giữa"]


def _zone_heat(X, window):
    w = X[-window:].sum(0)
    return np.array([w[z].sum() for z in ZONES]) + np.array([_rng.random() * 1e-3 for _ in ZONES])


def _rank_in(z, X):
    h = X[-30:].sum(0)
    return [int(i) + 1 for i in sorted(z, key=lambda i: (-h[i], _rng.random()))][:10]


def e_zone_hot(X, ctx):      # zone with the most numbers over the last 10 draws
    return _rank_in(ZONES[int(np.argmax(_zone_heat(X, 10)))], X)


def e_zone_cold(X, ctx):     # zone with the fewest numbers over the last 10 draws ("its turn")
    return _rank_in(ZONES[int(np.argmin(_zone_heat(X, 10)))], X)


def e_zone_last(X, ctx):     # zone that had the most numbers in the last draw
    return _rank_in(ZONES[int(np.argmax(_zone_heat(X, 1)))], X)


def e_zone_block(X, ctx):    # solid 2-row × 5-column block inside the hottest zone
    z = ZONES[int(np.argmax(_zone_heat(X, 10)))]
    h = X[-10:].sum(0)
    blocks = [z[i * 5:(i + 2) * 5] for i in range(3)]          # rows 1-2, 2-3, 3-4 of the zone
    best = max(blocks, key=lambda b: h[b].sum() + _rng.random() * 1e-3)
    return _rank_in(best, X)


def e_zone_mid(X, ctx):      # always the centre zone, its 10 hottest numbers
    return _rank_in(ZONES[4], X)


ENGINES = {
    "RANDOM": e_random,
    "HOT": e_hot,
    "GAP": e_gap,
    "CLUSTER": e_cluster,
    "BLOCK": e_block,
    "MARKOV": e_markov,
    "PATTERN": e_pattern,
    "DEVIATION": e_deviation,
    "LEARNED": e_learned,
    "Z-HOT": e_zone_hot,
    "Z-COLD": e_zone_cold,
    "Z-LAST": e_zone_last,
    "Z-BLOCK": e_zone_block,
    "Z-MID": e_zone_mid,
}

DESCRIPTIONS = {
    "RANDOM": "Chọn ngẫu nhiên — nhóm đối chứng.",
    "HOT": "Số ra nhiều nhất trong 30 kỳ gần nhất.",
    "GAP": "Số lâu chưa ra nhất.",
    "CLUSTER": "Một cụm ô liền nhau trên bảng 10x8 có 'nhiệt' cao nhất.",
    "BLOCK": "Khối 2 cột x 5 hàng nóng nhất.",
    "MARKOV": "Xác suất chuyển: số nào hay ra ngay sau các số của kỳ trước.",
    "PATTERN": "AI học: hồi quy logistic trên 10 tín hiệu, tự train lại định kỳ.",
    "DEVIATION": "Z-score lệch chuẩn ở 3 khung 30/90/toàn bộ, phạt các số hay đi cùng nhau.",
    "LEARNED": "Tự học: vòng lặp thử hàng nghìn cách kết hợp 14 tín hiệu trên dữ liệu thật, học lại mỗi 500 kỳ.",
    "Z-HOT": "Vùng (trong 5 vùng) ra nhiều số nhất 10 kỳ qua, 10 số nóng nhất trong vùng.",
    "Z-COLD": "Vùng ra ít số nhất 10 kỳ qua (\"đến lượt\"), 10 số nóng nhất trong vùng.",
    "Z-LAST": "Vùng ra nhiều số nhất ở kỳ vừa rồi.",
    "Z-BLOCK": "Khối liền 2 hàng × 5 cột nóng nhất trong vùng nóng nhất.",
    "Z-MID": "Luôn đánh vùng giữa (23–27, 33–37, 43–47, 53–57), 10 số nóng nhất.",
}


def predict_all(hist, ctx):
    """hist: draws up to the basis draw. Returns {engine: ranked10}."""
    X = to_matrix(hist[-2000:])
    out = {}
    for name, fn in ENGINES.items():
        try:
            out[name] = [int(n) for n in fn(X, ctx)][:10]
        except Exception:
            out[name] = e_random(X, ctx)
        assert len(set(out[name])) == 10
    return out
