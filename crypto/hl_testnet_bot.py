#!/usr/bin/env python3
"""Bot futures Hyperliquid TESTNET (uang mainan) berbasis sinyal futures_scan.py.

Terkunci ke testnet: tidak ada opsi mainnet di script ini.

Setup (sekali):
    pip install -r crypto/requirements-hl.txt
    1. Buka https://app.hyperliquid-testnet.xyz, connect wallet, klaim USDC
       testnet dari faucet (menu Drip / Faucet).
    2. Menu More -> API: buat API wallet (agent). Simpan private key-nya.
    3. Set environment variable (atau tulis di crypto/.env, jangan di-commit):
         HL_ACCOUNT_ADDRESS=0x...   # alamat wallet utama
         HL_AGENT_KEY=0x...         # private key API wallet (bukan wallet utama)

Pakai:
    python3 crypto/hl_testnet_bot.py plan               # lihat rencana order (tidak kirim apa pun)
    python3 crypto/hl_testnet_bot.py run                # kirim order setelah konfirmasi "YA"
    python3 crypto/hl_testnet_bot.py status             # saldo, posisi, order terbuka
    python3 crypto/hl_testnet_bot.py cancel-all         # batalkan semua order terbuka

Strategi default (hasil riset crypto/research.py, lihat README):
    --side long       hanya LONG (breakout ke atas)
    --min-score 3     skor sinyal minimal 3
    --entry market    masuk langsung di harga sekarang (IOC, slippage maks 1%);
                      --entry retest = limit di level + --entry-atr x ATR
    --sl-atr 1.2      SL = level breakout - 1,2 ATR 4H
    --tp-r 3          TP = 3 x jarak SL
Aturan risiko:
    --risk 1          risk per trade = 1% saldo
    --max-pos 4       maksimal 4 koin aktif (posisi/order)
    --leverage 3      leverage isolated; margin per trade <= 30%, total <= 90% saldo
    --expiry-hours 24 entry limit yang belum terisi > 24 jam dibatalkan saat `run`
Setiap order entry dikirim bersama SL dan TP (grouping normalTpsl). Saat `run`, TP/SL
sisa (koin tanpa posisi dan tanpa order entry) juga dibersihkan.
Sinyal dihitung dari candle 4H yang sudah close: jalankan `run` sesaat setelah candle 4H
close (00/04/08/12/16/20 UTC = 07/11/15/19/23/03 WIB).
"""

import argparse
import csv
import datetime as dt
import math
import os
import sys
from pathlib import Path

from backtest import Params, plan_trade
from futures_scan import analyze, liquid_pairs

TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
HERE = Path(__file__).resolve().parent
JOURNAL = HERE / "hl_journal.csv"
MIN_NOTIONAL = 10.0  # minimal nilai order Hyperliquid (USD)
MAX_MARGIN_USE = 0.9  # total margin semua posisi/order maks 90% saldo
SLIPPAGE = 0.01       # batas harga order market (IOC)


def load_env():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def round_px(px, sz_decimals):
    """Aturan harga perp Hyperliquid: maks 5 angka penting & maks (6 - szDecimals) desimal."""
    px = float(f"{px:.5g}")
    return round(px, max(0, 6 - sz_decimals))


def round_sz_down(sz, sz_decimals):
    f = 10 ** sz_decimals
    return math.floor(sz * f) / f


def build_orders(coin, long, entry, sl, tp, market, sz_decimals, account_value, risk_pct):
    """Order [entry, tp, sl]. Return (orders, info) atau (None, alasan)."""
    entry = round_px(entry, sz_decimals)
    sl = round_px(sl, sz_decimals)
    tp = round_px(tp, sz_decimals)
    risk_usd = account_value * risk_pct / 100
    per_unit = abs(entry - sl)
    if per_unit <= 0:
        return None, "jarak SL nol"
    sz = round_sz_down(risk_usd / per_unit, sz_decimals)
    notional = sz * entry
    if sz <= 0 or notional < MIN_NOTIONAL:
        return None, f"nilai order ${notional:.2f} < minimum ${MIN_NOTIONAL:.0f}"
    if market:
        px = round_px(entry * (1 + SLIPPAGE if long else 1 - SLIPPAGE), sz_decimals)
        entry_order = {"coin": coin, "is_buy": long, "sz": sz, "limit_px": px,
                       "order_type": {"limit": {"tif": "Ioc"}}, "reduce_only": False}
    else:
        entry_order = {"coin": coin, "is_buy": long, "sz": sz, "limit_px": entry,
                       "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": False}
    orders = [
        entry_order,
        {"coin": coin, "is_buy": not long, "sz": sz, "limit_px": tp,
         "order_type": {"trigger": {"triggerPx": tp, "isMarket": True, "tpsl": "tp"}}, "reduce_only": True},
        {"coin": coin, "is_buy": not long, "sz": sz, "limit_px": sl,
         "order_type": {"trigger": {"triggerPx": sl, "isMarket": True, "tpsl": "sl"}}, "reduce_only": True},
    ]
    return orders, {"entry": entry, "tp": tp, "sl": sl, "sz": sz, "notional": notional, "risk": risk_usd}


def clients(need_exchange):
    from hyperliquid.info import Info

    load_env()
    address = os.getenv("HL_ACCOUNT_ADDRESS")
    if not address:
        sys.exit("HL_ACCOUNT_ADDRESS belum di-set (lihat bagian Setup di atas file ini).")
    info = Info(TESTNET_URL, skip_ws=True)
    ex = None
    if need_exchange:
        import eth_account
        from hyperliquid.exchange import Exchange

        key = os.getenv("HL_AGENT_KEY")
        if not key:
            sys.exit("HL_AGENT_KEY belum di-set.")
        wallet = eth_account.Account.from_key(key)
        ex = Exchange(wallet, TESTNET_URL, account_address=address)
    return info, ex, address


def account_snapshot(info, address):
    st = info.user_state(address)
    value = float(st["marginSummary"]["accountValue"])
    try:
        unified = info.post("/info", {"type": "userAbstraction", "user": address}) == "unifiedAccount"
    except Exception:
        unified = False
    if unified or value == 0:
        # Akun terpadu (unified): USDC tercatat di spot; bagian yang dipakai sebagai margin
        # perp muncul sebagai "hold" dan sudah terhitung di accountValue perp, jadi yang
        # ditambahkan hanya USDC spot yang bebas (total - hold).
        spot = info.spot_user_state(address)["balances"]
        value += sum(float(b["total"]) - float(b.get("hold", 0)) for b in spot if b["coin"] == "USDC")
    positions = [p["position"] for p in st["assetPositions"] if float(p["position"]["szi"]) != 0]
    orders = info.frontend_open_orders(address)  # punya reduceOnly & timestamp
    return value, positions, orders


def stale_orders(positions, orders, expiry_hours):
    """Order yang perlu dibatalkan: [(coin, [order...], alasan)].

    - Entry limit yang belum terisi lebih dari expiry_hours (beserta TP/SL-nya).
    - TP/SL sisa: koin tanpa posisi dan tanpa order entry.
    Koin yang punya posisi tidak disentuh (TP/SL-nya melindungi posisi).
    """
    now = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    in_pos = {p["coin"] for p in positions}
    by_coin = {}
    for o in orders:
        by_coin.setdefault(o["coin"], []).append(o)
    out = []
    for coin, os_ in by_coin.items():
        if coin in in_pos:
            continue
        entries = [o for o in os_ if not o.get("reduceOnly")]
        if not entries:
            out.append((coin, os_, "TP/SL sisa tanpa posisi"))
        elif expiry_hours and all(now - o["timestamp"] > expiry_hours * 3600 * 1000 for o in entries):
            age = (now - max(o["timestamp"] for o in entries)) / 3600000
            out.append((coin, os_, f"entry belum terisi {age:.0f} jam"))
    return out


def strategy(args):
    return Params(tp_r=args.tp_r, sl_atr=args.sl_atr, entry_atr=args.entry_atr,
                  market=args.entry == "market", side=args.side, min_score=args.min_score,
                  trend_only=args.trend_only, max_ext=4.0)


def make_plan(info, address, args):
    meta = info.meta()
    sz_dec = {a["name"]: a["szDecimals"] for a in meta["universe"] if not a.get("isDelisted")}
    value, positions, orders = account_snapshot(info, address)
    stale = stale_orders(positions, orders, args.expiry_hours)
    stale_coins = {c for c, _, _ in stale}
    live_orders = [o for o in orders if o["coin"] not in stale_coins]
    busy = {p["coin"] for p in positions} | {o["coin"] for o in live_orders}
    slots = args.max_pos - len(busy)
    used = sum(float(p.get("marginUsed", 0)) for p in positions) + sum(
        float(o["sz"]) * float(o["limitPx"]) / args.leverage
        for o in live_orders if not o.get("reduceOnly") and o["coin"] not in {p["coin"] for p in positions})

    p = strategy(args)
    pairs = liquid_pairs(args.top, args.min_vol)
    sigs = [s for s in (_safe(analyze, b) for b, _, _ in pairs) if s and not s["failed"]]
    sigs = [(s, plan_trade(s, p)) for s in sigs]
    sigs = [(s, pl) for s, pl in sigs if pl]
    sigs.sort(key=lambda x: (-x[0]["score"], x[0]["ext"]))
    mids = info.all_mids() if p.market else {}

    plan, skipped = [], []
    for s, (entry, sl, tp) in sigs:
        coin, long = s["coin"], s["side"] == "LONG"
        if coin not in sz_dec:
            skipped.append((coin, "tidak ada di Hyperliquid"))
            continue
        if coin in busy:
            skipped.append((coin, "sudah ada posisi/order"))
            continue
        if len(plan) >= max(slots, 0):
            skipped.append((coin, "slot posisi penuh"))
            continue
        if p.market:
            # entry di harga Hyperliquid; SL/TP tetap dari level Binance, jadi cek harga masih di antaranya
            entry = float(mids.get(coin, 0))
            sign = 1 if long else -1
            if not entry or sign * (entry - sl) <= 0 or sign * (tp - entry) <= 0 \
                    or abs(entry - sl) / entry > 0.12:
                skipped.append((coin, f"harga Hyperliquid {entry:g} di luar rentang SL {sl:.5g} / TP {tp:.5g}"))
                continue
        orders_, info_ = build_orders(coin, long, entry, sl, tp, p.market, sz_dec[coin], value, args.risk)
        if not orders_:
            skipped.append((coin, info_))
            continue
        margin = info_["notional"] / args.leverage
        if margin > value * 0.3:
            skipped.append((coin, f"margin ${margin:.0f} > 30% saldo"))
            continue
        if used + margin > value * MAX_MARGIN_USE:
            skipped.append((coin, f"total margin akan > {MAX_MARGIN_USE:.0%} saldo"))
            continue
        used += margin
        plan.append((s, orders_, info_))
    return value, positions, live_orders, plan, skipped, stale


def print_plan(value, positions, orders, plan, skipped, stale, args):
    print(f"TESTNET | saldo ${value:,.2f} | posisi {len(positions)} | order terbuka {len(orders)} "
          f"| risk {args.risk}%/trade | leverage {args.leverage}x isolated | entry {args.entry} "
          f"| SL {args.sl_atr} ATR | TP {args.tp_r}R | arah {args.side} | skor>={args.min_score}\n")
    for coin, os_, why in stale:
        print(f"  BATALKAN {coin:<8} {len(os_)} order ({why})")
    if stale:
        print()
    if not plan:
        print("Tidak ada order baru yang lolos filter.")
    for s, _, i in plan:
        print(f"  {s['coin']:<8}{s['side']:<6} entry {i['entry']:<10g} SL {i['sl']:<10g} TP {i['tp']:<10g} "
              f"size {i['sz']:<10g} nilai ${i['notional']:,.0f}  risk ${i['risk']:.2f}  ({s['tf']}, skor {s['score']})")
    if skipped:
        print("\nDilewati: " + "; ".join(f"{c} ({why})" for c, why in skipped))


def journal(rows):
    new = not JOURNAL.exists()
    with JOURNAL.open("a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["waktu_utc", "coin", "side", "entry", "sl", "tp", "size", "notional", "risk_usd", "hasil"])
        for r in rows:
            w.writerow(r)


def cmd_plan(args):
    info, _, address = clients(False)
    print_plan(*make_plan(info, address, args), args)


def cmd_run(args):
    info, ex, address = clients(True)
    value, positions, orders, plan, skipped, stale = make_plan(info, address, args)
    print_plan(value, positions, orders, plan, skipped, stale, args)
    if not plan and not stale:
        return
    if input("\nJalankan pembatalan/order di atas di TESTNET? Ketik YA untuk lanjut: ").strip() != "YA":
        print("Dibatalkan.")
        return
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    rows = []
    for coin, os_, why in stale:
        res = ex.bulk_cancel([{"coin": o["coin"], "oid": o["oid"]} for o in os_])
        print(f"{coin}: batalkan {len(os_)} order ({why}) -> {res.get('status')}")
        rows.append([now, coin, "", "", "", "", "", "", "", f"dibatalkan: {why}"])
    for s, orders_, i in plan:
        lev = ex.update_leverage(args.leverage, s["coin"], is_cross=False)
        res = ex.bulk_orders(orders_, grouping="normalTpsl")
        ok = res.get("status") == "ok"
        statuses = res.get("response", {}).get("data", {}).get("statuses", []) if ok else []
        if ok and statuses and "error" in statuses[0]:
            ok = False
        print(f"{s['coin']}: leverage {lev.get('status')} | order {'OK' if ok else 'GAGAL'} -> {res}")
        rows.append([now, s["coin"], s["side"], i["entry"], i["sl"], i["tp"], i["sz"],
                     round(i["notional"], 2), round(i["risk"], 2), "terkirim" if ok else f"gagal: {res}"])
    journal(rows)
    print(f"\nTercatat di {JOURNAL}")


def cmd_status(args):
    info, _, address = clients(False)
    value, positions, orders = account_snapshot(info, address)
    print(f"TESTNET | saldo ${value:,.2f}\n\nPOSISI")
    for p in positions or []:
        print(f"  {p['coin']:<8} size {p['szi']:<10} entry {p['entryPx']:<10} "
              f"uPnL ${float(p['unrealizedPnl']):+.2f}  likuidasi {p.get('liquidationPx')}")
    if not positions:
        print("  (tidak ada)")
    print("\nORDER TERBUKA")
    for o in orders or []:
        age = (dt.datetime.now(dt.timezone.utc).timestamp() * 1000 - o["timestamp"]) / 3600000
        print(f"  {o['coin']:<8} {'BUY' if o['side'] == 'B' else 'SELL':<5} {o['sz']:<10} @ {o['limitPx']:<10} "
              f"{o.get('orderType', ''):<18} {age:5.1f} jam  oid {o['oid']}")
    if not orders:
        print("  (tidak ada)")


def cmd_cancel_all(args):
    info, ex, address = clients(True)
    orders = info.open_orders(address)
    if not orders:
        print("Tidak ada order terbuka.")
        return
    res = ex.bulk_cancel([{"coin": o["coin"], "oid": o["oid"]} for o in orders])
    print(f"Membatalkan {len(orders)} order -> {res.get('status')}")


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["plan", "run", "status", "cancel-all"])
    ap.add_argument("--risk", type=float, default=1.0)
    ap.add_argument("--max-pos", type=int, default=4)
    ap.add_argument("--leverage", type=int, default=3)
    ap.add_argument("--entry", choices=["market", "retest"], default="market")
    ap.add_argument("--entry-atr", type=float, default=0.2, help="jarak limit retest dari level (ATR)")
    ap.add_argument("--sl-atr", type=float, default=1.2)
    ap.add_argument("--tp-r", type=float, default=3.0)
    ap.add_argument("--side", choices=["long", "short", "both"], default="long")
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--trend-only", action="store_true")
    ap.add_argument("--expiry-hours", type=float, default=24,
                    help="batalkan entry limit yang belum terisi setelah N jam (0 = tidak pernah)")
    ap.add_argument("--top", type=int, default=80)
    ap.add_argument("--min-vol", type=float, default=20)
    args = ap.parse_args()
    if args.risk > 2 or args.leverage > 5:
        sys.exit("Ditolak: risk maks 2% dan leverage maks 5x.")
    if args.tp_r <= 0 or args.sl_atr <= 0:
        sys.exit("Ditolak: --tp-r dan --sl-atr harus > 0.")
    {"plan": cmd_plan, "run": cmd_run, "status": cmd_status, "cancel-all": cmd_cancel_all}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
