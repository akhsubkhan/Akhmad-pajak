#!/usr/bin/env python3
"""Eksperimen ML: menyaring sinyal breakout bot dengan model prediksi (meta-labeling).

Contoh:
    python3 crypto/ml_experiment.py --cache /tmp/hl3y.pkl      # data 3 tahun (dibuat research.py --cache)

Cara kerja:
    1. Semua sinyal LONG (skor >= 2) yang lolos filter strategi dikumpulkan, lalu masing-masing
       disimulasikan sendiri (entry di open, SL 1,2 ATR, TP 3R, fee taker) -> hasil dalam R.
    2. Fitur dihitung HANYA dari data sebelum sinyal (skor, jarak dari level, volatilitas,
       volume, momentum koin & BTC, jumlah sinyal serentak, jam, dll).
    3. Walk-forward: model dilatih ulang tiap 90 hari memakai trade yang SUDAH selesai sebelum
       periode uji, lalu memprediksi periode 90 hari berikutnya. Tahun pertama hanya untuk latih.
    4. Hasil uji (out-of-sample) dibandingkan dengan bot tanpa ML, per sinyal dan di simulasi
       portofolio (maks 4 posisi, risk 1%) memakai backtest.simulate.
Model: regresi logistik (pembanding sederhana), LightGBM (gradient boosting), dan MLP
(neural network kecil, mewakili deep learning).
"""

import argparse
import math
import pickle
import sys
import warnings

import numpy as np

import backtest as bt

warnings.filterwarnings("ignore")
H4 = bt.H4_MS
D1 = bt.D1_MS
MAX_HOLD = 180  # candle 4H (30 hari); setelah itu ditutup di close
FEATURES = ["score", "ext", "tf_1d", "tf_both", "trend_ok", "rsi4", "vol_x", "sl_pct", "atr_pct", "ago",
            "ret_1d", "ret_7d", "ret_30d", "rvol_7d", "dist_hi30", "ema200_gap", "rs_btc_30d",
            "btc_ret_1d", "btc_ret_7d", "btc_ret_30d", "btc_ema200_gap", "n_signals", "hour", "weekday"]


def ema_series(xs, n):
    out, k, e = [None] * len(xs), 2 / (n + 1), None
    for i, x in enumerate(xs):
        if i + 1 == n:
            e = sum(xs[:n]) / n
        elif i + 1 > n:
            e = x * k + e * (1 - k)
        out[i] = e
    return out


def simulate_one(h4, i, entry, sl, tp):
    """Trade tunggal: masuk di open candle i. Return (R bersih, index keluar)."""
    r = entry - sl
    for j in range(i, min(len(h4), i + MAX_HOLD)):
        c = h4[j]
        if j > i and c["o"] <= sl:
            px = c["o"]
        elif j > i and c["o"] >= tp:
            px = c["o"]
        elif c["l"] <= sl:
            px = sl
        elif j > i and c["h"] >= tp:
            px = tp
        else:
            continue
        return ((px - entry) - (entry + px) * bt.TAKER_FEE) / r, j
    j = min(len(h4), i + MAX_HOLD) - 1
    if j <= i:
        return None, None
    px = h4[j]["c"]
    return ((px - entry) - (entry + px) * bt.TAKER_FEE) / r, j


def build_candidates(data, p):
    h4s, idx, by_time = bt.precompute(data)
    closes = {b: [c["c"] for c in h4] for b, h4 in h4s.items()}
    ema200 = {b: ema_series(cl, 200) for b, cl in closes.items()}
    btc = "BTC"
    rows = []
    for t in sorted(by_time):
        sigs = by_time[t]
        n_sig = sum(1 for s in sigs if s["side"] == "LONG")
        bi = idx[btc].get(t)
        if bi is None or bi < 200:
            continue
        bc = closes[btc]
        for s in sigs:
            pl, _ = bt.check_trade(s, p)
            if not pl:
                continue
            b, i = s["coin"], idx[s["coin"]][t]
            if i < 200:
                continue
            entry, sl, tp = pl
            r, j = simulate_one(h4s[b], i, entry, sl, tp)
            if r is None:
                continue
            cl = closes[b]
            win = cl[i - 42:i]
            lr = [math.log(y / x) for x, y in zip(win, win[1:])]
            dt_ = np.datetime64(t, "ms").astype(object)
            rows.append({
                "coin": b, "t": t, "i": i, "exit_t": h4s[b][j]["t"], "R": r,
                "score": s["score"], "ext": s["ext"], "tf_1d": int("1D" in s["tf"]),
                "tf_both": int(s["tf"] == "4H+1D"), "trend_ok": int(s["trend_ok"]), "rsi4": s["rsi4"] or 50,
                "vol_x": s["vol_x"], "sl_pct": (entry - sl) / entry, "atr_pct": s["atr"] / s["price"],
                "ago": s["ago"], "ret_1d": cl[i - 1] / cl[i - 7] - 1, "ret_7d": cl[i - 1] / cl[i - 43] - 1,
                "ret_30d": cl[i - 1] / cl[i - 181] - 1, "rvol_7d": float(np.std(lr)),
                "dist_hi30": cl[i - 1] / max(c["h"] for c in h4s[b][i - 180:i]) - 1,
                "ema200_gap": cl[i - 1] / ema200[b][i - 1] - 1,
                "rs_btc_30d": (cl[i - 1] / cl[i - 181]) - (bc[bi - 1] / bc[bi - 181]),
                "btc_ret_1d": bc[bi - 1] / bc[bi - 7] - 1, "btc_ret_7d": bc[bi - 1] / bc[bi - 43] - 1,
                "btc_ret_30d": bc[bi - 1] / bc[bi - 181] - 1, "btc_ema200_gap": bc[bi - 1] / ema200[btc][bi - 1] - 1,
                "n_signals": n_sig, "hour": dt_.hour, "weekday": dt_.weekday(),
            })
    return (h4s, idx, by_time), rows


def dedup_events(rows):
    """Satu trade per koin pada satu waktu (seperti bot) -> sampel latih tidak dobel."""
    busy, out = {}, []
    for r in sorted(rows, key=lambda r: r["t"]):
        if r["t"] < busy.get(r["coin"], 0):
            continue
        out.append(r)
        busy[r["coin"]] = r["exit_t"] + H4
    return out


def make_models():
    from lightgbm import LGBMClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return {
        "Logistik": lambda: make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=2000)),
        "LightGBM": lambda: LGBMClassifier(n_estimators=200, learning_rate=0.03, num_leaves=8,
                                           min_child_samples=30, subsample=0.8, subsample_freq=1,
                                           colsample_bytree=0.8, reg_lambda=1.0, verbose=-1),
        "MLP (neural net)": lambda: make_pipeline(StandardScaler(), MLPClassifier(
            hidden_layer_sizes=(32, 16), alpha=1e-2, early_stopping=True, max_iter=500, random_state=0)),
    }


def stats(rs):
    if not rs:
        return "   0 trade"
    rs = np.array(rs)
    return f"{len(rs):>4} trade  WR {np.mean(rs > 0) * 100:4.0f}%  rata2 {rs.mean():+.3f}R  total {rs.sum():+7.1f}R"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", required=True, help="pickle data candle (dari research.py --cache)")
    ap.add_argument("--fold-days", type=int, default=90)
    ap.add_argument("--warmup-days", type=int, default=365)
    args = ap.parse_args()
    from sklearn.metrics import roc_auc_score

    data = pickle.load(open(args.cache, "rb"))
    p = bt.Params(market=True, sl_atr=1.2, tp_r=3, side="long", min_score=2, max_pos=4)
    pre, rows = build_candidates(data, p)
    events = dedup_events(rows)
    print(f"Kandidat sinyal: {len(rows)} | trade unik (1 per koin): {len(events)}")

    t0 = min(r["t"] for r in rows)
    start = t0 + args.warmup_days * D1
    end = max(r["t"] for r in rows) + H4
    X = lambda rs: np.array([[r[f] for f in FEATURES] for r in rs], dtype=float)
    y = lambda rs: np.array([int(r["R"] > 0) for r in rs])

    preds = {m: {} for m in make_models()}      # (coin, t) -> prob
    thresholds = {m: [] for m in make_models()}  # (fold_start, fold_end, thr50, thr30)
    folds = 0
    fs = start
    while fs < end:
        fe = fs + args.fold_days * D1
        train = [e for e in events if e["exit_t"] < fs]
        test = [r for r in rows if fs <= r["t"] < fe]
        if len(train) >= 100 and test:
            folds += 1
            for name, mk in make_models().items():
                m = mk().fit(X(train), y(train))
                ptr = m.predict_proba(X(train))[:, 1]
                thresholds[name].append((fs, fe, np.quantile(ptr, 0.5), np.quantile(ptr, 0.7)))
                for r, pr in zip(test, m.predict_proba(X(test))[:, 1]):
                    preds[name][(r["coin"], r["t"])] = pr
        fs = fe
    print(f"Walk-forward: {folds} periode uji x {args.fold_days} hari, mulai "
          f"{np.datetime64(start, 'ms').astype('datetime64[D]')}\n")

    # ---- 1) per trade unik di periode uji
    test_ev = [e for e in events if e["t"] >= start]
    print("== Per trade (out-of-sample, tiap trade disimulasikan sendiri) ==")
    print(f"  Tanpa ML, skor >= 2        : {stats([e['R'] for e in test_ev])}")
    print(f"  Tanpa ML, skor >= 3 (bot)  : {stats([e['R'] for e in test_ev if e['score'] >= 3])}")
    for name in preds:
        pr = [(e, preds[name].get((e["coin"], e["t"]))) for e in test_ev]
        pr = [(e, q) for e, q in pr if q is not None]
        auc = roc_auc_score([int(e["R"] > 0) for e, _ in pr], [q for _, q in pr])
        thr = {}
        for fs_, fe_, t50, t30 in thresholds[name]:
            thr[(fs_, fe_)] = (t50, t30)
        def keep(e, q, k):
            for (a, b), v in thr.items():
                if a <= e["t"] < b:
                    return q >= v[k]
            return False
        print(f"  {name:<16} AUC {auc:.3f}")
        print(f"     ambil 50% teratas       : {stats([e['R'] for e, q in pr if keep(e, q, 0)])}")
        print(f"     ambil 30% teratas       : {stats([e['R'] for e, q in pr if keep(e, q, 1)])}")

    # ---- 2) simulasi portofolio (maks 4 posisi, risk 1%) di periode uji
    h4s, idx, by_time = pre
    print("\n== Simulasi portofolio periode uji (maks 4 posisi, risk 1%, compounding) ==")

    def run(label, prm):
        res = bt.simulate((h4s, idx, by_time), prm, t_start=start)
        s = bt.stats(res)
        print(f"  {label:<34} {s['n']:>4} trade  WR {s['wr'] * 100:4.0f}%  rata2 {s['avg_r']:+.3f}R  "
              f"hasil {s['ret'] * 100:+6.0f}%  max DD {s['dd'] * 100:4.0f}%")

    base3 = bt.Params(market=True, sl_atr=1.2, tp_r=3, side="long", min_score=3, max_pos=4)
    run("Bot sekarang (skor >= 3)", base3)
    run("Tanpa ML, skor >= 2", p)
    for name in preds:
        for k, lab in ((0, "50%"), (1, "30%")):
            ok = set()  # sinyal (koin, waktu) yang lolos ambang model di fold-nya
            for (coin, t), q in preds[name].items():
                for a, b, t50, t30 in thresholds[name]:
                    if a <= t < b and q >= (t50, t30)[k]:
                        ok.add((coin, t))
            filt = {t: [s for s in ss if (s["coin"], t) in ok] for t, ss in by_time.items()}
            res = bt.simulate((h4s, idx, filt), p, t_start=start)
            s = bt.stats(res)
            print(f"  {name + ' ' + lab + ' teratas':<34} {s['n']:>4} trade  WR {s['wr'] * 100:4.0f}%  "
                  f"rata2 {s['avg_r']:+.3f}R  hasil {s['ret'] * 100:+6.0f}%  max DD {s['dd'] * 100:4.0f}%")

    # ---- 3) fitur terpenting (LightGBM di semua data latih)
    m = make_models()["LightGBM"]().fit(X(events), y(events))
    imp = sorted(zip(FEATURES, m.booster_.feature_importance("gain")), key=lambda x: -x[1])
    tot = sum(v for _, v in imp)
    print("\nFitur terpenting (LightGBM): " + ", ".join(f"{f} {v / tot * 100:.0f}%" for f, v in imp[:8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
