#!/usr/bin/env python3
"""Backtest strategi hl_testnet_bot.py (sinyal futures_scan.py) di data Binance.

Contoh:
    python3 crypto/backtest.py                          # 365 hari, setelan bot sekarang + expiry 12 candle
    python3 crypto/backtest.py --tp-r 3 --expiry 0      # TP 3R, entry tidak pernah dibatalkan
    python3 crypto/backtest.py --days 180 --coins BTC ETH SOL --trades
    python3 crypto/backtest.py --tp-r 0 --trail 3       # tanpa TP, trailing stop 3 ATR

Simulasi meniru bot yang dijalankan setiap candle 4H close:
    - Sinyal & filter sama dengan bot: skor >= --min-score, bukan GAGAL, jauh <= --max-ext ATR,
      SL <= 12%, harga masih di sisi aman entry, margin <= 30% saldo per trade dan <= 90%
      total, maks --max-pos koin aktif.
    - Entry: limit di level +/- --entry-atr x ATR (retest), atau --market untuk masuk di open.
    - SL = level -/+ --sl-atr x ATR; TP = --tp-r x jarak SL (0 = tanpa TP, keluar lewat trailing).
    - Trailing (--trail N): stop ikut naik ke (high tertinggi sejak entry - N x ATR).
    - Limit entry terisi kalau candle menyentuh entry (gap = terisi di open).
    - Kalau SL & TP tersentuh di candle yang sama dianggap SL (konservatif). Di candle saat
      entry terisi hanya SL yang dicek. Gap melewati SL/TP = keluar di harga open.
    - Fee: maker 0,015% (limit entry), taker 0,045% (market entry & semua exit). Funding diabaikan.
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
from dataclasses import dataclass

from futures_scan import evaluate, liquid_pairs
from levels import binance

H4_MS = 4 * 3600 * 1000
D1_MS = 24 * 3600 * 1000
H4_WINDOW = 259  # = jumlah candle 4H close yang dipakai bot live (260 - 1 yang berjalan)
D1_WINDOW = 59
MAKER_FEE = 0.00015
TAKER_FEE = 0.00045
MIN_NOTIONAL = 10.0
MAX_MARGIN_USE = 0.9  # total margin semua order/posisi maks 90% saldo (sama dengan bot)


@dataclass
class Params:
    tp_r: float = 1.5          # 0 = tanpa TP
    sl_atr: float = 1.2
    entry_atr: float = 0.2
    market: bool = False       # True = masuk di open candle berikutnya (taker)
    trail: float = 0.0         # 0 = tanpa trailing stop
    expiry: int = 12           # candle 4H; 0 = tidak pernah
    min_score: int = 2
    max_ext: float = 4.0
    side: str = "both"         # both / long / short
    tf: str = "all"            # all / 1D / 4H (4H+1D ikut keduanya)
    trend_only: bool = False
    max_pos: int = 2
    risk: float = 1.0
    leverage: float = 3.0
    capital: float = 1000.0


# ------------------------------------------------------------------ data

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
    return [c for c in out if c["t"] + step_ms <= now]  # buang candle yang belum close


def load(base, days):
    start = int(time.time() * 1000) - days * D1_MS
    h4 = fetch(base + "USDT", "4h", start - (H4_WINDOW + 5) * H4_MS, H4_MS)
    d1 = fetch(base + "USDT", "1d", start - (D1_WINDOW + 5) * D1_MS, D1_MS)
    return base, h4, d1


def load_all(coins, days):
    with ThreadPoolExecutor(max_workers=6) as ex:
        return [d for d in ex.map(lambda b: _safe(load, b, days), coins) if d]


def precompute(data):
    """Sinyal mentah per candle 4H (tidak tergantung parameter). Return (h4s, idx, signals_by_time)."""
    h4s = {b: h4 for b, h4, _ in data}
    idx = {b: {c["t"]: i for i, c in enumerate(h4)} for b, h4, _ in data}
    by_time = {}
    for b, h4, d1 in data:
        p = 0
        for i in range(H4_WINDOW, len(h4)):
            t = h4[i]["t"]
            while p < len(d1) and d1[p]["t"] + D1_MS <= t:
                p += 1
            if p < 25:
                continue
            s = evaluate(h4[i - H4_WINDOW:i], d1[max(0, p - D1_WINDOW):p], h4[i]["o"])
            if s and not s["failed"]:
                s["coin"] = b
                by_time.setdefault(t, []).append(s)
    return h4s, idx, by_time


# ------------------------------------------------------------ simulasi

def plan_trade(s, p):
    """Entry/SL/TP sesuai parameter. Return None kalau tidak lolos filter."""
    return check_trade(s, p)[0]


def check_trade(s, p):
    """Return ((entry, sl, tp), None) kalau lolos filter, atau (None, alasan)."""
    long = s["side"] == "LONG"
    if p.side != "both" and s["side"].lower() != p.side:
        return None, f"arah {s['side']} (bot hanya {p.side})"
    if p.tf != "all" and p.tf not in s["tf"]:
        return None, f"timeframe {s['tf']}"
    if s["score"] < p.min_score:
        return None, f"skor {s['score']} < {p.min_score}"
    if s["ext"] > p.max_ext:
        return None, f"harga sudah {s['ext']:.1f} ATR dari level (maks {p.max_ext:g})"
    if p.trend_only and not s["trend_ok"]:
        return None, "tidak searah tren"
    a, lvl, price, sign = s["atr"], s["level"], s["price"], (1 if long else -1)
    entry = price if p.market else lvl + sign * p.entry_atr * a
    sl = lvl - sign * p.sl_atr * a
    r = sign * (entry - sl)
    if r <= 0:
        return None, "harga sudah melewati SL"
    if r / entry > 0.12:
        return None, f"jarak SL {r / entry * 100:.1f}% > 12%"
    if not p.market and not (sign * (price - entry) > 0):
        return None, "harga belum di sisi aman entry (limit akan langsung tereksekusi)"
    tp = entry + sign * p.tp_r * r if p.tp_r else None
    return (entry, sl, tp), None


def simulate(pre, p, t_start=0, t_end=None):
    h4s, idx, by_time = pre
    times = sorted({c["t"] for h4 in h4s.values() for c in h4
                    if c["t"] >= t_start and (t_end is None or c["t"] < t_end)})
    equity = peak = p.capital
    max_dd = 0.0
    state, trades = {}, []
    cancelled = 0

    def close(coin, st, px, t, why):
        nonlocal equity
        pnl = st["sign"] * (px - st["fill"]) * st["sz"]
        fee = st["fill"] * st["sz"] * st["entry_fee"] + px * st["sz"] * TAKER_FEE
        equity += pnl - fee
        trades.append({"coin": coin, "side": st["side"], "tf": st["tf"], "score": st["score"],
                       "placed": st["placed_t"], "filled": st["fill_t"], "closed": t, "exit": why,
                       "entry": st["fill"], "sl": st["sl0"], "tp": st["tp"], "exit_px": px,
                       "pnl": pnl - fee, "r": (pnl - fee) / st["risk"]})
        del state[coin]

    for t in times:
        # 1) keputusan bot di awal candle t (semua candle sebelum t sudah close)
        slots = p.max_pos - len(state)
        if slots > 0 and t in by_time:
            cands = []
            for s in by_time[t]:
                if s["coin"] in state:
                    continue
                pl = plan_trade(s, p)
                if pl:
                    cands.append((s, pl))
            cands.sort(key=lambda x: (-x[0]["score"], x[0]["ext"]))
            for s, (entry, sl, tp) in cands[:slots]:
                risk = equity * p.risk / 100
                sz = risk / abs(entry - sl)
                notional = sz * entry
                margin = notional / p.leverage
                used = sum(st["margin"] for st in state.values())
                if notional < MIN_NOTIONAL or margin > equity * 0.3 or used + margin > equity * MAX_MARGIN_USE:
                    continue
                b = s["coin"]
                state[b] = {"side": s["side"], "sign": 1 if s["side"] == "LONG" else -1,
                            "tf": s["tf"], "score": s["score"], "atr": s["atr"],
                            "entry": entry, "sl": sl, "sl0": sl, "tp": tp, "sz": sz, "risk": risk, "margin": margin,
                            "placed_t": t, "placed_i": idx[b][t], "fill": None, "fill_t": None,
                            "entry_fee": TAKER_FEE if p.market else MAKER_FEE}

        # 2) proses candle t untuk order/posisi aktif
        for b in list(state):
            i = idx[b].get(t)
            if i is None:
                continue
            c = h4s[b][i]
            st = state[b]
            sg = st["sign"]
            hi, lo = (c["h"], c["l"]) if sg == 1 else (-c["l"], -c["h"])  # dicerminkan utk short
            op = sg * c["o"]
            just = False
            if st["fill"] is None:
                if p.market:
                    st["fill"] = c["o"]
                elif lo <= sg * st["entry"]:
                    st["fill"] = sg * min(op, sg * st["entry"])
                elif p.expiry and i - st["placed_i"] + 1 >= p.expiry:
                    del state[b]
                    cancelled += 1
                    continue
                else:
                    continue
                st["fill_t"], st["best"] = t, sg * st["fill"]
                just = True
            sl = sg * st["sl"]
            tp = sg * st["tp"] if st["tp"] is not None else None
            if not just and op <= sl:
                close(b, st, sg * op, t, "SL")
            elif not just and tp is not None and op >= tp:
                close(b, st, sg * op, t, "TP")
            elif lo <= sl:
                close(b, st, sg * sl, t, "SL")
            elif not just and tp is not None and hi >= tp:
                close(b, st, sg * tp, t, "TP")
            elif p.trail:
                st["best"] = max(st["best"], hi)
                new = st["best"] - p.trail * st["atr"]
                if new > sl:
                    st["sl"] = sg * new

        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)

    for tr in trades:
        if tr["exit"] == "SL" and tr["r"] > -0.9:
            tr["exit"] = "TRAIL"
    open_pos = sum(1 for st in state.values() if st["fill"] is not None)
    return {"trades": trades, "equity": equity, "max_dd": max_dd, "cancelled": cancelled,
            "open_pos": open_pos, "pending": len(state) - open_pos, "capital": p.capital}


def stats(res):
    tr = res["trades"]
    n = len(tr)
    if not n:
        return {"n": 0, "wr": 0, "avg_r": 0, "total_r": 0, "pf": 0, "ret": 0, "dd": res["max_dd"]}
    gw = sum(t["pnl"] for t in tr if t["pnl"] > 0)
    gl = -sum(t["pnl"] for t in tr if t["pnl"] <= 0)
    rs = [t["r"] for t in tr]
    return {"n": n, "wr": sum(1 for t in tr if t["pnl"] > 0) / n, "avg_r": sum(rs) / n,
            "total_r": sum(rs), "pf": gw / gl if gl else float("inf"),
            "ret": res["equity"] / res["capital"] - 1, "dd": res["max_dd"]}


# -------------------------------------------------------------- output

def ts(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M")


def report(res, p, show_trades=False):
    tr, s = res["trades"], stats(res)
    print(f"\nHASIL (TP {p.tp_r or 'tidak ada'}R, SL {p.sl_atr} ATR, "
          f"entry {'market' if p.market else f'retest {p.entry_atr} ATR'}, trailing {p.trail or '-'}, "
          f"expiry {p.expiry or 'tidak ada'}, skor>={p.min_score}, arah {p.side}, TF {p.tf}"
          f"{', hanya searah tren' if p.trend_only else ''})")
    if not s["n"]:
        print("  Tidak ada trade.")
        return
    streak = worst = 0
    for t in tr:
        streak = streak + 1 if t["pnl"] <= 0 else 0
        worst = max(worst, streak)
    print(f"  Trade selesai      : {s['n']}  (menang {round(s['wr'] * s['n'])}, kalah {s['n'] - round(s['wr'] * s['n'])})")
    print(f"  Win rate           : {s['wr'] * 100:.1f}%")
    print(f"  Rata-rata per trade: {s['avg_r']:+.3f} R")
    print(f"  Total              : {s['total_r']:+.1f} R")
    print(f"  Profit factor      : {s['pf']:.2f}")
    print(f"  Kalah beruntun     : {worst}")
    print(f"  Saldo              : ${p.capital:,.0f} -> ${res['equity']:,.2f} ({s['ret'] * 100:+.1f}%)")
    print(f"  Max drawdown       : {s['dd'] * 100:.1f}%")
    print(f"  Order kedaluwarsa  : {res['cancelled']} | masih terbuka di akhir: "
          f"posisi {res['open_pos']}, order {res['pending']}")
    for label, key in (("Arah", "side"), ("Timeframe", "tf"), ("Skor", "score"), ("Exit", "exit")):
        groups = {}
        for t in tr:
            groups.setdefault(t[key], []).append(t)
        parts = [f"{k}: {len(g)}, WR {sum(1 for t in g if t['pnl'] > 0) / len(g) * 100:.0f}%, "
                 f"{sum(t['r'] for t in g):+.1f}R" for k, g in sorted(groups.items(), key=lambda x: str(x[0]))]
        print(f"  Per {label:<10}: " + " | ".join(parts))
    if show_trades:
        print(f"\n{'Koin':<8}{'Arah':<6}{'TF':<6}{'Skor':>4}  {'Terisi':<17}{'Tutup':<17}{'Exit':<6}{'R':>7}")
        for t in tr:
            print(f"{t['coin']:<8}{t['side']:<6}{t['tf']:<6}{t['score']:>4}  {ts(t['filled']):<17}"
                  f"{ts(t['closed']):<17}{t['exit']:<6}{t['r']:>+7.2f}")


def add_param_args(ap):
    d = Params()
    ap.add_argument("--tp-r", type=float, default=d.tp_r, help="TP dalam R (0 = tanpa TP)")
    ap.add_argument("--sl-atr", type=float, default=d.sl_atr)
    ap.add_argument("--entry-atr", type=float, default=d.entry_atr)
    ap.add_argument("--market", action="store_true", help="entry market, bukan limit retest")
    ap.add_argument("--trail", type=float, default=d.trail, help="trailing stop N x ATR (0 = mati)")
    ap.add_argument("--expiry", type=int, default=d.expiry,
                    help="batalkan entry yang belum terisi setelah N candle 4H (0 = tidak pernah)")
    ap.add_argument("--min-score", type=int, default=d.min_score)
    ap.add_argument("--max-ext", type=float, default=d.max_ext)
    ap.add_argument("--side", choices=["both", "long", "short"], default=d.side)
    ap.add_argument("--tf", choices=["all", "1D", "4H"], default=d.tf)
    ap.add_argument("--trend-only", action="store_true")
    ap.add_argument("--max-pos", type=int, default=d.max_pos)
    ap.add_argument("--risk", type=float, default=d.risk)
    ap.add_argument("--leverage", type=float, default=d.leverage)
    ap.add_argument("--capital", type=float, default=d.capital)


def params_from(args):
    return Params(**{k: getattr(args, k) for k in Params.__dataclass_fields__})


def universe(args):
    if args.coins:
        return [c.upper() for c in args.coins]
    return [b for b, _, _ in liquid_pairs(args.top, args.min_vol)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--top", type=int, default=80)
    ap.add_argument("--min-vol", type=float, default=20)
    ap.add_argument("--coins", nargs="*", help="daftar koin manual (default: top by volume)")
    ap.add_argument("--trades", action="store_true", help="tampilkan daftar trade")
    ap.add_argument("--csv", help="simpan daftar trade ke file CSV")
    add_param_args(ap)
    args = ap.parse_args()
    p = params_from(args)

    coins = universe(args)
    print(f"Mengambil data {len(coins)} koin ({args.days} hari)...", file=sys.stderr)
    data = load_all(coins, args.days)
    print(f"Data siap: {len(data)} koin. Simulasi...", file=sys.stderr)
    start = int(time.time() * 1000) - args.days * D1_MS
    res = simulate(precompute(data), p, t_start=start)
    report(res, p, args.trades)
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(res["trades"][0]) if res["trades"] else ["coin"])
            w.writeheader()
            w.writerows(res["trades"])
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
