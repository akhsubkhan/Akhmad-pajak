#!/usr/bin/env python3
"""Backtest strategi hl_testnet_bot.py (sinyal futures_scan.py) di data Binance.

Contoh:
    python3 crypto/backtest.py                          # 365 hari, top 80, TP1, order kedaluwarsa 12 candle
    python3 crypto/backtest.py --tp 2 --expiry 0        # TP 3R, order entry tidak pernah dibatalkan (= bot sekarang)
    python3 crypto/backtest.py --days 180 --coins BTC ETH SOL --trades

Simulasi meniru bot yang dijalankan setiap candle 4H close:
    - Sinyal & filter sama dengan bot: skor >= 2, bukan GAGAL, jauh <= 4 ATR, SL <= 12%,
      harga masih di sisi aman entry, margin <= 30% saldo, maks --max-pos koin aktif.
    - Limit entry terisi kalau low/high candle menyentuh entry (gap = terisi di open).
    - Setelah terisi, SL/TP dicek per candle 4H. Kalau SL & TP tersentuh di candle yang
      sama, dianggap SL (konservatif). Di candle saat entry terisi hanya SL yang dicek.
    - Gap melewati SL/TP = keluar di harga open.
    - Fee: entry maker 0,015%, exit taker 0,045% (tarif dasar Hyperliquid). Funding diabaikan.
    - Size = risk% x saldo (saldo realisasi, compounding) / jarak SL.

Keterbatasan: universe = koin dengan volume terbesar HARI INI (bias survivorship),
harga Binance (bukan Hyperliquid), resolusi 4H (urutan harga di dalam candle tidak diketahui).
"""

import argparse
import csv
import datetime as dt
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from futures_scan import evaluate, liquid_pairs
from levels import binance

H4_MS = 4 * 3600 * 1000
D1_MS = 24 * 3600 * 1000
H4_WINDOW = 259  # = jumlah candle 4H close yang dipakai bot live (260 - 1 yang berjalan)
D1_WINDOW = 59
MAKER_FEE = 0.00015
TAKER_FEE = 0.00045
MIN_NOTIONAL = 10.0


def fetch(symbol, interval, start_ms, step_ms):
    out = []
    while True:
        rows = binance("/api/v3/klines", {"symbol": symbol, "interval": interval,
                                          "startTime": start_ms, "limit": 1000})
        out += [{"t": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                 "c": float(r[4]), "v": float(r[5])} for r in rows]
        if len(rows) < 1000:
            break
        start_ms = rows[-1][0] + step_ms
    now = int(time.time() * 1000)
    # buang candle yang belum close
    return [c for c in out if c["t"] + step_ms <= now]


def load(base, days):
    start = int(time.time() * 1000) - days * D1_MS
    h4 = fetch(base + "USDT", "4h", start - (H4_WINDOW + 5) * H4_MS, H4_MS)
    d1 = fetch(base + "USDT", "1d", start - (D1_WINDOW + 5) * D1_MS, D1_MS)
    return base, h4, d1


def tradeable(s):
    return s["score"] >= 2 and not s["failed"] and s["ext"] <= 4 and s["sl_pct"] <= 0.12


def run(data, args):
    start = int(time.time() * 1000) - args.days * D1_MS
    times = sorted({c["t"] for _, h4, _ in data for c in h4 if c["t"] >= start})
    idx = {b: {c["t"]: i for i, c in enumerate(h4)} for b, h4, _ in data}
    h4s = {b: h4 for b, h4, _ in data}
    d1s = {b: d1 for b, _, d1 in data}
    d1_ptr = {b: 0 for b in h4s}

    equity = args.capital
    peak, max_dd = equity, 0.0
    state = {}  # coin -> dict(order/posisi aktif)
    trades = []
    cancelled = 0

    def close(coin, st, px, t, why):
        nonlocal equity
        sign = 1 if st["side"] == "LONG" else -1
        pnl = sign * (px - st["fill"]) * st["sz"]
        fee = st["fill"] * st["sz"] * MAKER_FEE + px * st["sz"] * TAKER_FEE
        equity += pnl - fee
        trades.append({
            "coin": coin, "side": st["side"], "tf": st["tf"], "score": st["score"],
            "placed": st["placed_t"], "filled": st["fill_t"], "closed": t, "exit": why,
            "entry": st["fill"], "sl": st["sl"], "tp": st["tp"], "exit_px": px,
            "pnl": pnl - fee, "r": (pnl - fee) / st["risk"],
        })
        del state[coin]

    for t in times:
        # 1) keputusan bot di awal candle t (semua candle sebelum t sudah close)
        slots = args.max_pos - len(state)
        if slots > 0:
            cands = []
            for b, h4 in h4s.items():
                if b in state or t not in idx[b]:
                    continue
                i = idx[b][t]
                if i < H4_WINDOW:
                    continue
                d1 = d1s[b]
                p = d1_ptr[b]
                while p < len(d1) and d1[p]["t"] + D1_MS <= t:
                    p += 1
                d1_ptr[b] = p
                if p < 25:
                    continue
                price = h4[i]["o"]
                s = evaluate(h4[i - H4_WINDOW:i], d1[max(0, p - D1_WINDOW):p], price)
                if not s or not tradeable(s):
                    continue
                above = price > s["entry"] if s["side"] == "LONG" else price < s["entry"]
                if above:
                    cands.append((b, s))
            cands.sort(key=lambda x: (-x[1]["score"], x[1]["ext"]))
            for b, s in cands[:slots]:
                tp = s["tp1"] if args.tp == 1 else s["tp2"]
                risk = equity * args.risk / 100
                sz = risk / abs(s["entry"] - s["sl"])
                notional = sz * s["entry"]
                if notional < MIN_NOTIONAL or notional / args.leverage > equity * 0.3:
                    continue
                state[b] = {"side": s["side"], "tf": s["tf"], "score": s["score"],
                            "entry": s["entry"], "sl": s["sl"], "tp": tp, "sz": sz, "risk": risk,
                            "placed_t": t, "placed_i": idx[b][t], "fill": None, "fill_t": None}

        # 2) proses candle t untuk order/posisi aktif
        for b in list(state):
            if t not in idx[b]:
                continue
            i = idx[b][t]
            c = h4s[b][i]
            st = state[b]
            long = st["side"] == "LONG"
            just_filled = False
            if st["fill"] is None:
                hit = c["l"] <= st["entry"] if long else c["h"] >= st["entry"]
                if hit:
                    st["fill"] = min(c["o"], st["entry"]) if long else max(c["o"], st["entry"])
                    st["fill_t"] = t
                    just_filled = True
                elif args.expiry and i - st["placed_i"] + 1 >= args.expiry:
                    del state[b]
                    cancelled += 1
                    continue
                else:
                    continue
            sl, tp = st["sl"], st["tp"]
            if long:
                if not just_filled and c["o"] <= sl:
                    close(b, st, c["o"], t, "SL")
                elif not just_filled and c["o"] >= tp:
                    close(b, st, c["o"], t, "TP")
                elif c["l"] <= sl:
                    close(b, st, sl, t, "SL")
                elif not just_filled and c["h"] >= tp:
                    close(b, st, tp, t, "TP")
            else:
                if not just_filled and c["o"] >= sl:
                    close(b, st, c["o"], t, "SL")
                elif not just_filled and c["o"] <= tp:
                    close(b, st, c["o"], t, "TP")
                elif c["h"] >= sl:
                    close(b, st, sl, t, "SL")
                elif not just_filled and c["l"] <= tp:
                    close(b, st, tp, t, "TP")

        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)

    open_pos = sum(1 for st in state.values() if st["fill"] is not None)
    return trades, equity, max_dd, cancelled, open_pos, len(state) - open_pos


def ts(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def report(trades, equity, max_dd, cancelled, open_pos, pending, args):
    n = len(trades)
    print(f"\nHASIL ({args.days} hari, TP{args.tp} = {'1,5R' if args.tp == 1 else '3R'}, "
          f"risk {args.risk}%, maks {args.max_pos} koin, "
          f"expiry {'tidak ada' if not args.expiry else str(args.expiry) + ' candle 4H'})")
    if not n:
        print("  Tidak ada trade.")
        return
    wins = [t for t in trades if t["pnl"] > 0]
    rs = [t["r"] for t in trades]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    streak = worst = 0
    for t in trades:
        streak = streak + 1 if t["pnl"] <= 0 else 0
        worst = max(worst, streak)
    print(f"  Trade selesai     : {n}  (menang {len(wins)}, kalah {n - len(wins)})")
    print(f"  Win rate          : {len(wins) / n * 100:.1f}%")
    print(f"  Rata-rata per trade: {sum(rs) / n:+.3f} R")
    print(f"  Total             : {sum(rs):+.1f} R")
    print(f"  Profit factor     : {gross_w / gross_l:.2f}" if gross_l else "  Profit factor     : -")
    print(f"  Kalah beruntun    : {worst}")
    print(f"  Saldo             : ${args.capital:,.0f} -> ${equity:,.2f} "
          f"({(equity / args.capital - 1) * 100:+.1f}%)")
    print(f"  Max drawdown      : {max_dd * 100:.1f}%")
    print(f"  Order dibatalkan (expiry): {cancelled} | masih terbuka di akhir: posisi {open_pos}, order {pending}")
    for label, key in (("Arah", "side"), ("Timeframe", "tf"), ("Skor", "score")):
        groups = {}
        for t in trades:
            groups.setdefault(t[key], []).append(t)
        parts = []
        for k in sorted(groups, key=str):
            g = groups[k]
            w = sum(1 for t in g if t["pnl"] > 0)
            parts.append(f"{k}: {len(g)} trade, WR {w / len(g) * 100:.0f}%, {sum(t['r'] for t in g):+.1f}R")
        print(f"  Per {label:<10}: " + " | ".join(parts))
    if args.trades:
        print(f"\n{'Koin':<8}{'Arah':<6}{'TF':<6}{'Skor':>4}  {'Terisi':<17}{'Tutup':<17}{'Exit':<5}{'R':>7}")
        for t in trades:
            print(f"{t['coin']:<8}{t['side']:<6}{t['tf']:<6}{t['score']:>4}  {ts(t['filled']):<17}"
                  f"{ts(t['closed']):<17}{t['exit']:<5}{t['r']:>+7.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--top", type=int, default=80)
    ap.add_argument("--min-vol", type=float, default=20)
    ap.add_argument("--coins", nargs="*", help="daftar koin manual (default: top by volume)")
    ap.add_argument("--capital", type=float, default=1000)
    ap.add_argument("--risk", type=float, default=1.0)
    ap.add_argument("--leverage", type=float, default=3)
    ap.add_argument("--max-pos", type=int, default=2)
    ap.add_argument("--tp", type=int, choices=[1, 2], default=1)
    ap.add_argument("--expiry", type=int, default=12,
                    help="batalkan entry yang belum terisi setelah N candle 4H (0 = tidak pernah)")
    ap.add_argument("--trades", action="store_true", help="tampilkan daftar trade")
    ap.add_argument("--csv", help="simpan daftar trade ke file CSV")
    args = ap.parse_args()

    coins = [c.upper() for c in args.coins] if args.coins else [b for b, _, _ in liquid_pairs(args.top, args.min_vol)]
    print(f"Mengambil data {len(coins)} koin ({args.days} hari)...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=6) as ex:
        data = [d for d in ex.map(lambda b: _safe(load, b, args.days), coins) if d]
    print(f"Data siap: {len(data)} koin. Simulasi...", file=sys.stderr)

    trades, equity, max_dd, cancelled, open_pos, pending = run(data, args)
    report(trades, equity, max_dd, cancelled, open_pos, pending, args)
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(trades[0]) if trades else ["coin"])
            w.writeheader()
            w.writerows(trades)
    print("\nHasil historis tidak menjamin hasil ke depan. Bukan saran finansial.")
    return 0


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as err:
        print(f"  lewati {a[0]}: {err}", file=sys.stderr)
        return None


if __name__ == "__main__":
    sys.exit(main())
