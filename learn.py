"""LEARNED engine: searches for the best combination of signals on past draws, in a loop.

Each candidate = a weight vector over 14 signals. Score of number n before draw t+1 = sum(w_k * signal_k).
Ticket = the 10 highest scores. Fitness = average hits on the NEXT draw, measured on training draws only.
The loop: random search -> keep the best -> hill-climb around it -> repeat. Then the winner is checked on
held-out draws it has never seen, which is the only number that tells whether it learned anything real.
"""
import math

import numpy as np

ROWS, COLS = 8, 10
SIGNALS = ["freq 5", "freq 10", "freq 30", "freq 100", "freq 300", "gap", "gap²",
           "lag-1", "lag-2", "lag-3", "pair w/ last", "row heat", "col heat", "neighbor heat"]


def _zrow(a):
    return (a - a.mean(1, keepdims=True)) / (a.std(1, keepdims=True) + 1e-9)


def signals(X):
    """S[t, n, k]: signal k for number n, using draws 0..t only (row t describes the state AFTER draw t)."""
    X = X.astype(np.float32)
    T = X.shape[0]
    cs = np.vstack([np.zeros((1, 80), np.float32), np.cumsum(X, 0)])
    idx = np.arange(T) + 1

    def win(w):
        lo = np.maximum(idx - w, 0)
        return (cs[idx] - cs[lo]) / np.maximum(idx - lo, 1)[:, None]

    out = [win(w) for w in (5, 10, 30, 100, 300)]
    g = np.zeros((T, 80), np.float32)
    run = np.full(80, 10.0, np.float32)
    for t in range(T):
        run = np.where(X[t] > 0, 0, run + 1)
        g[t] = run
    out += [np.log1p(g), np.minimum(g, 40) ** 2]
    out += [X, np.vstack([np.zeros((1, 80)), X[:-1]]), np.vstack([np.zeros((2, 80)), X[:-2]])[:T]]
    # pair affinity with the last draw, from co-occurrence counted only up to t
    pair = np.zeros((T, 80), np.float32)
    C = np.zeros((80, 80), np.float32)
    for t in range(T):
        C += np.outer(X[t], X[t])
        pair[t] = C @ X[t] / (t + 1)
    out.append(pair)
    h6 = win(6).reshape(T, ROWS, COLS)
    out.append(np.repeat(h6.sum(2), COLS, axis=1))
    out.append(np.tile(h6.sum(1), (1, ROWS)))
    nb = np.zeros_like(h6)
    nb[:, 1:, :] += h6[:, :-1, :]
    nb[:, :-1, :] += h6[:, 1:, :]
    nb[:, :, 1:] += h6[:, :, :-1]
    nb[:, :, :-1] += h6[:, :, 1:]
    out.append(nb.reshape(T, 80))
    S = np.stack([_zrow(o) for o in out], axis=2).astype(np.float32)
    return S


def fitness(S, Y, w, k=10):
    """Average hits of the top-k ticket. S: (N,80,K) signals at t, Y: (N,80) the draw at t+1."""
    sc = S @ w
    top = np.argpartition(-sc, k, axis=1)[:, :k]
    return float(np.take_along_axis(Y, top, axis=1).sum(1).mean())


def search(S, Y, rounds=6, pop=150, seed=0, log=None):
    """The loop: random candidates + hill-climbing around the best, several rounds."""
    rng = np.random.default_rng(seed)
    K = S.shape[2]
    best_w = np.zeros(K, np.float32)
    best_w[2] = 1.0  # start from plain HOT(30)
    best = fitness(S, Y, best_w)
    history = [best]
    for r in range(rounds):
        cands = [rng.normal(0, 1, K).astype(np.float32) for _ in range(pop // 2)]
        step = 0.6 / (r + 1)
        cands += [(best_w + rng.normal(0, step, K)).astype(np.float32) for _ in range(pop // 2)]
        for w in cands:
            f = fitness(S, Y, w)
            if f > best:
                best, best_w = f, w
        history.append(best)
        if log:
            log(f"round {r + 1}: best train score {best:.4f}")
    n = np.linalg.norm(best_w) or 1.0
    return best_w / n, best, history


def predict(X, model):
    """Ranked 10 for the draw after the last row of X."""
    S = signals(X[-400:])
    sc = S[-1] @ np.array(model["w"], np.float32)
    return [int(i) + 1 for i in np.argsort(-sc)[:10]]


def train(X, rounds=6, pop=150, seed=0):
    """Fit on all of X (used live). Returns model dict."""
    S = signals(X)
    w, f, _ = search(S[:-1], X[1:].astype(np.float32), rounds=rounds, pop=pop, seed=seed)
    return {"w": [float(x) for x in w], "train_score": f, "trained_on": int(X.shape[0])}
