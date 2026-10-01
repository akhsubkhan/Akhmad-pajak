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

Aturan risiko (bisa diubah lewat argumen):
    --risk 1        risk per trade = 1% saldo
    --max-pos 2     maksimal 2 posisi/order aktif
    --leverage 3    leverage isolated
    --tp 1          target: 1 = TP1 (1,5R), 2 = TP2 (3R)
Setiap order entry dikirim bersama SL dan TP (grouping normalTpsl).
"""

import argparse
import csv
import datetime as dt
import math
import os
import sys
from pathlib import Path

from futures_scan import analyze, liquid_pairs

TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
HERE = Path(__file__).resolve().parent
JOURNAL = HERE / "hl_journal.csv"
MIN_NOTIONAL = 10.0  # minimal nilai order Hyperliquid (USD)


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


def build_orders(sig, sz_decimals, account_value, risk_pct, tp_choice):
    """Ubah sinyal futures_scan jadi [entry, tp, sl]. Return (orders, info) atau (None, alasan)."""
    long = sig["side"] == "LONG"
    entry = round_px(sig["entry"], sz_decimals)
    sl = round_px(sig["sl"], sz_decimals)
    tp = round_px(sig["tp1"] if tp_choice == 1 else sig["tp2"], sz_decimals)
    risk_usd = account_value * risk_pct / 100
    per_unit = abs(entry - sl)
    if per_unit <= 0:
        return None, "jarak SL nol"
    sz = round_sz_down(risk_usd / per_unit, sz_decimals)
    notional = sz * entry
    if sz <= 0 or notional < MIN_NOTIONAL:
        return None, f"nilai order ${notional:.2f} < minimum ${MIN_NOTIONAL:.0f}"
    orders = [
        {"coin": sig["coin"], "is_buy": long, "sz": sz, "limit_px": entry,
         "order_type": {"limit": {"tif": "Gtc"}}, "reduce_only": False},
        {"coin": sig["coin"], "is_buy": not long, "sz": sz, "limit_px": tp,
         "order_type": {"trigger": {"triggerPx": tp, "isMarket": True, "tpsl": "tp"}}, "reduce_only": True},
        {"coin": sig["coin"], "is_buy": not long, "sz": sz, "limit_px": sl,
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
    if value == 0:
        # Akun terpadu (unified): USDC tercatat di spot tapi langsung jadi margin perp.
        spot = info.spot_user_state(address)["balances"]
        value = sum(float(b["total"]) - float(b.get("hold", 0)) for b in spot if b["coin"] == "USDC")
    positions = [p["position"] for p in st["assetPositions"] if float(p["position"]["szi"]) != 0]
    orders = info.open_orders(address)
    return value, positions, orders


def make_plan(info, address, args):
    meta = info.meta()
    sz_dec = {a["name"]: a["szDecimals"] for a in meta["universe"] if not a.get("isDelisted")}
    value, positions, orders = account_snapshot(info, address)
    busy = {p["coin"] for p in positions} | {o["coin"] for o in orders}
    slots = args.max_pos - len(busy)

    pairs = liquid_pairs(args.top, args.min_vol)
    sigs = [s for s in (_safe(analyze, b) for b, _, _ in pairs) if s]
    sigs = [s for s in sigs if s["score"] >= 2 and not s["failed"] and s["ext"] <= 4 and s["sl_pct"] <= 0.12]
    sigs.sort(key=lambda s: (-s["score"], s["ext"]))

    plan, skipped = [], []
    for s in sigs:
        if s["coin"] not in sz_dec:
            skipped.append((s["coin"], "tidak ada di Hyperliquid"))
            continue
        if s["coin"] in busy:
            skipped.append((s["coin"], "sudah ada posisi/order"))
            continue
        above = s["price"] > s["entry"] if s["side"] == "LONG" else s["price"] < s["entry"]
        if not above:
            skipped.append((s["coin"], "harga belum di sisi aman entry (limit akan langsung tereksekusi)"))
            continue
        if len(plan) >= max(slots, 0):
            skipped.append((s["coin"], "slot posisi penuh"))
            continue
        orders_, info_ = build_orders(s, sz_dec[s["coin"]], value, args.risk, args.tp)
        if not orders_:
            skipped.append((s["coin"], info_))
            continue
        margin = info_["notional"] / args.leverage
        if margin > value * 0.3:
            skipped.append((s["coin"], f"margin ${margin:.0f} > 30% saldo"))
            continue
        plan.append((s, orders_, info_))
    return value, positions, orders, plan, skipped


def print_plan(value, positions, orders, plan, skipped, args):
    print(f"TESTNET | saldo ${value:,.2f} | posisi {len(positions)} | order terbuka {len(orders)} "
          f"| risk {args.risk}%/trade | leverage {args.leverage}x isolated\n")
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
    print_plan(*make_plan(info, address, args)[:5], args)


def cmd_run(args):
    info, ex, address = clients(True)
    value, positions, orders, plan, skipped = make_plan(info, address, args)
    print_plan(value, positions, orders, plan, skipped, args)
    if not plan:
        return
    if input("\nKirim order di atas ke TESTNET? Ketik YA untuk lanjut: ").strip() != "YA":
        print("Dibatalkan.")
        return
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    rows = []
    for s, orders_, i in plan:
        lev = ex.update_leverage(args.leverage, s["coin"], is_cross=False)
        res = ex.bulk_orders(orders_, grouping="normalTpsl")
        ok = res.get("status") == "ok"
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
        print(f"  {o['coin']:<8} {'BUY' if o['side'] == 'B' else 'SELL':<5} {o['sz']:<10} @ {o['limitPx']}  oid {o['oid']}")
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
    ap.add_argument("--max-pos", type=int, default=2)
    ap.add_argument("--leverage", type=int, default=3)
    ap.add_argument("--tp", type=int, choices=[1, 2], default=1)
    ap.add_argument("--top", type=int, default=80)
    ap.add_argument("--min-vol", type=float, default=20)
    args = ap.parse_args()
    if args.risk > 2 or args.leverage > 5:
        sys.exit("Ditolak: risk maks 2% dan leverage maks 5x.")
    {"plan": cmd_plan, "run": cmd_run, "status": cmd_status, "cancel-all": cmd_cancel_all}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
