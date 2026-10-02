#!/usr/bin/env python3
"""Backtest strategi support/resisten (S/R) 4H dengan limit order.

Contoh:
    python3 crypto/sr_backtest.py --cache /tmp/hl3y.pkl              # grid + latih/uji
    python3 crypto/sr_backtest.py --cache /tmp/hl3y.pkl --only-default

Aturan (dievaluasi tiap candle 4H close, per koin yang belum punya posisi/order):
    - Garis S/R = swing high/low (fractal 3 candle kiri-kanan) dari --lookback candle terakhir,
      dikelompokkan bila jaraknya < 0,6 ATR. Swing baru dipakai setelah TERKONFIRMASI
      (3 candle sesudahnya sudah close), jadi tidak ada intip data masa depan.
      Kekuatan garis = jumlah sentuhan; dipakai bila >= --touches.
    - LONG : limit buy di support + entry_off x ATR, SL di support - sl_off x ATR.
      SHORT: limit sell di resisten - entry_off x ATR, SL di resisten + sl_off x ATR.
      TP = rr x jarak SL. Support terdekat di bawah harga & resisten terdekat di atas harga.
    - Kedua order (buy & sell) boleh dipasang bersamaan; kalau satu terisi, yang lain batal.
      Order belum terisi dibatalkan setelah --expiry candle.
    - Eksekusi seperti backtest.py: SL & TP di candle yang sama = SL; di candle entry hanya SL
      yang dicek; gap = keluar di open. Fee: entry maker 0,015%, exit taker 0,045%.
    - Risk 1%/trade, maks --max-pos koin aktif, leverage 5x (SL dekat -> posisi besar),
      margin <= 30% saldo per trade dan <= 90% total.
"""

import argparse
import itertools
import pickle
import sys
import time
from dataclasses import dataclass, replace
from multiprocessing import Pool

import backtest as bt
from levels import atr

D1, H4 = bt.D1_MS, bt.H4_MS
WIDTH = 3


@dataclass(frozen=True)
class SR:
    rr: float = 2.0
    entry_off: float = 0.1
    sl_off: float = 0.3
    touches: int = 2
    side: str = "both"
    expiry: int = 6
    lookback: int = 180
    max_pos: int = 4
    risk: float = 1.0
    leverage: float = 5.0


def precompute_levels(data, lookback=180):
    """Per koin per candle k: (atr, [(level, sentuhan), ...]) dari swing yang terkonfirmasi sebelum k."""
    out = {}
    for b, h4, _ in data:
        swings = []  # (index swing, harga)
        for i in range(WIDTH, len(h4) - WIDTH):
            win = h4[i - WIDTH:i + WIDTH + 1]
            if h4[i]["h"] == max(c["h"] for c in win):
                swings.append((i, h4[i]["h"]))
            if h4[i]["l"] == min(c["l"] for c in win):
                swings.append((i, h4[i]["l"]))
        lv, s0 = {}, 0
        for k in range(lookback + WIDTH + 15, len(h4)):
            while s0 < len(swings) and swings[s0][0] < k - lookback:
                s0 += 1
            pts = sorted(px for i, px in swings[s0:] if i + WIDTH < k)  # terkonfirmasi sebelum k
            a = atr(h4[k - 15:k])
            tol = 0.6 * a
            groups = []
            for px in pts:
                if groups and px - groups[-1][-1] <= tol:
                    groups[-1].append(px)
                else:
                    groups.append([px])
            lv[k] = (a, [(sum(g) / len(g), len(g)) for g in groups])
        out[b] = lv
    return out


def simulate(data, levels, p, t_start=0, t_end=None, capital=1000.0):
    h4s = {b: h4 for b, h4, _ in data}
    idx = {b: {c["t"]: i for i, c in enumerate(h4)} for b, h4 in h4s.items()}
    times = sorted({c["t"] for h4 in h4s.values() for c in h4
                    if c["t"] >= t_start and (t_end is None or c["t"] < t_end)})
    equity = peak = capital
    max_dd = 0.0
    state, trades = {}, []

    def close(b, st, px, t, why):
        nonlocal equity
        pnl = st["sign"] * (px - st["fill"]) * st["sz"]
        fee = st["fill"] * st["sz"] * bt.MAKER_FEE + px * st["sz"] * bt.TAKER_FEE
        equity += pnl - fee
        trades.append({"coin": b, "side": st["side"], "placed": st["placed_t"], "closed": t,
                       "exit": why, "pnl": pnl - fee, "r": (pnl - fee) / st["risk"]})
        del state[b]

    for t in times:
        # 1) pasang order baru
        slots = p.max_pos - len(state)
        if slots > 0:
            cands = []
            for b, h4 in h4s.items():
                k = idx[b].get(t)
                if b in state or k is None or k not in levels[b]:
                    continue
                a, lv = levels[b][k]
                price = h4[k]["o"]
                if a <= 0:
                    continue
                orders = []
                if p.side in ("both", "long"):
                    sup = [(l, n) for l, n in lv if l < price and n >= p.touches]
                    if sup:
                        l, n = max(sup)
                        e, sl = l + p.entry_off * a, l - p.sl_off * a
                        if price > e and 0.002 < (e - sl) / e <= 0.12:
                            orders.append(("LONG", 1, e, sl, e + p.rr * (e - sl), n))
                if p.side in ("both", "short"):
                    res = [(l, n) for l, n in lv if l > price and n >= p.touches]
                    if res:
                        l, n = min(res)
                        e, sl = l - p.entry_off * a, l + p.sl_off * a
                        if price < e and 0.002 < (sl - e) / e <= 0.12:
                            orders.append(("SHORT", -1, e, sl, e - p.rr * (sl - e), n))
                if orders:
                    cands.append((max(o[5] for o in orders), b, k, orders))
            cands.sort(key=lambda x: -x[0])
            for _, b, k, orders in cands[:slots]:
                risk = equity * p.risk / 100
                used = sum(st["margin"] for st in state.values())
                ok = []
                for side, sg, e, sl, tp, _ in orders:
                    sz = risk / abs(e - sl)
                    margin = sz * e / p.leverage
                    if sz * e >= bt.MIN_NOTIONAL and margin <= equity * 0.3:
                        ok.append({"side": side, "sign": sg, "entry": e, "sl": sl, "tp": tp, "sz": sz,
                                   "margin": margin})
                if not ok or used + max(o["margin"] for o in ok) > equity * bt.MAX_MARGIN_USE:
                    continue
                state[b] = {"orders": ok, "placed_t": t, "placed_k": k, "fill": None,
                            "margin": max(o["margin"] for o in ok), "risk": risk}

        # 2) proses candle t
        for b in list(state):
            k = idx[b].get(t)
            if k is None:
                continue
            c = h4s[b][k]
            st = state[b]
            just = False
            if st["fill"] is None:
                filled = None
                for o in st["orders"]:
                    sg = o["sign"]
                    if (c["l"] <= o["entry"]) if sg == 1 else (c["h"] >= o["entry"]):
                        filled = o
                        break
                if filled is None:
                    if k - st["placed_k"] + 1 >= p.expiry:
                        del state[b]
                    continue
                o = filled
                st.update(side=o["side"], sign=o["sign"], sl=o["sl"], tp=o["tp"], sz=o["sz"],
                          margin=o["margin"],
                          fill=min(c["o"], o["entry"]) if o["sign"] == 1 else max(c["o"], o["entry"]))
                just = True
            sg = st["sign"]
            hi, lo, op = (c["h"], c["l"], c["o"]) if sg == 1 else (-c["l"], -c["h"], -c["o"])
            sl, tp = sg * st["sl"], sg * st["tp"]
            if not just and op <= sl:
                close(b, st, sg * op, t, "SL")
            elif not just and op >= tp:
                close(b, st, sg * op, t, "TP")
            elif lo <= sl:
                close(b, st, sg * sl, t, "SL")
            elif not just and hi >= tp:
                close(b, st, sg * tp, t, "TP")

        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    return {"trades": trades, "equity": equity, "max_dd": max_dd, "capital": capital}


def summarize(res, t_split):
    def part(tr):
        n = len(tr)
        if not n:
            return {"n": 0, "wr": 0, "avg": 0, "tot": 0}
        rs = [t["r"] for t in tr]
        return {"n": n, "wr": sum(r > 0 for r in rs) / n, "avg": sum(rs) / n, "tot": sum(rs)}
    tr = res["trades"]
    return (part([t for t in tr if t["placed"] < t_split]), part([t for t in tr if t["placed"] >= t_split]),
            res["equity"] / res["capital"] - 1, res["max_dd"])


DATA = LEVELS = SPLIT = None


def work(p):
    return p, summarize(simulate(DATA, LEVELS, p, t_start=SPLIT[0]), SPLIT[1])


def fmt(s):
    return f"{s['n']:>4} {s['wr'] * 100:>3.0f}% {s['avg']:>+6.2f}R"


def main():
    global DATA, LEVELS, SPLIT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", required=True, help="pickle data candle (dari research.py --cache)")
    ap.add_argument("--test-days", type=int, default=365)
    ap.add_argument("--only-default", action="store_true")
    args = ap.parse_args()

    DATA = pickle.load(open(args.cache, "rb"))
    t0 = time.time()
    LEVELS = precompute_levels(DATA)
    first = min(h4[0]["t"] for _, h4, _ in DATA)
    now = max(h4[-1]["t"] for _, h4, _ in DATA) + H4
    SPLIT = (first + 40 * D1, now - args.test_days * D1)
    print(f"{len(DATA)} koin, garis S/R dihitung ({time.time() - t0:.0f} dtk). "
          f"Latih: s.d. {time.strftime('%Y-%m-%d', time.gmtime(SPLIT[1] / 1000))}, uji: {args.test_days} hari terakhir\n")

    hdr = f"{'n':>4} {'WR':>4} {'rata2':>7}"
    print(f"{'Setelan':<46} | LATIH {hdr} | UJI {hdr} | 3 thn: hasil / DD")
    base = SR()
    defaults = [replace(base, rr=2), replace(base, rr=3)]
    for p in defaults:
        _, (a, b, ret, dd) = work(p)
        print(f"{'USULAN rr ' + str(int(p.rr)) + ' (entry +0,1 ATR, SL -0,3 ATR, 2 sentuhan)':<46} | {fmt(a)} | {fmt(b)} | {ret * 100:+5.0f}% / {dd * 100:.0f}%")
    if args.only_default:
        return 0

    grid = [SR(rr=rr, entry_off=e, sl_off=s, touches=tc, side=sd, expiry=x)
            for rr, e, s, tc, sd, x in itertools.product((2, 3), (0.1, 0.25), (0.3, 0.6), (2, 3),
                                                         ("both", "long", "short"), (6, 12))]
    with Pool() as pool:
        res = pool.map(work, grid)
    res = [r for r in res if r[1][0]["n"] >= 60]
    res.sort(key=lambda r: -r[1][0]["avg"] * r[1][0]["n"] ** 0.5)
    print(f"\n{len(grid)} kombinasi diuji. 10 terbaik menurut periode LATIH:")
    for p, (a, b, ret, dd) in res[:10]:
        lab = f"rr{p.rr:g} entry{p.entry_off:g} sl{p.sl_off:g} {p.touches}x {p.side} exp{p.expiry}"
        print(f"{lab:<46} | {fmt(a)} | {fmt(b)} | {ret * 100:+5.0f}% / {dd * 100:.0f}%")
    pos_tr = sum(1 for _, (a, _, _, _) in res if a["avg"] > 0)
    pos_te = sum(1 for _, (_, b, _, _) in res if b["avg"] > 0)
    print(f"\nKombinasi dengan rata-rata R positif: latih {pos_tr}/{len(res)}, uji {pos_te}/{len(res)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
