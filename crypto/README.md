# Crypto levels

Script `levels.py` menghitung support/resisten dan metrik on-chain.
Hanya butuh Python 3 (tanpa `pip install`).

```bash
python3 crypto/levels.py BTC
python3 crypto/levels.py LINK ONDO TAO
python3 crypto/levels.py BTC --price 83746   # pakai harga manual
python3 crypto/levels.py TAO --no-onchain
```

## Cara hitung
- **Support/resisten**: titik swing high/low (fractal) di candle 4H dan 1D
  Binance, lalu dikelompokkan kalau jaraknya < 0,6x ATR 4H. Kolom
  "kekuatan" = jumlah sentuhan (swing harian dihitung 2x).
- **MA & volume**: SMA50/SMA200/EMA200 harian, EMA200 4H, dan POC
  (harga dengan volume terbesar) dari 180 candle 4H terakhir.
- **On-chain** (Coin Metrics Community, hanya koin yang tersedia, mis. BTC/ETH):
  realized price = harga / MVRV, lalu band harga di MVRV 1,0 / 1,2 / 1,42 /
  1,5 / 1,62 / 2 / 2,4 / 3.

## Akses jaringan
Script butuh akses ke domain berikut. Di Claude Code cloud, tambahkan
di pengaturan environment -> Network access -> allowed domains:

```
api.binance.com
data-api.binance.vision
community-api.coinmetrics.io
```

API key (kalau nanti pakai BGeometrics/CryptoQuant) disimpan sebagai
environment variable `BGEOMETRICS_API_KEY` / `CRYPTOQUANT_API_KEY`,
jangan di-commit.

## Scanner breakout (`breakout.py`)
Mencari token L1/RWA yang belum atau baru keluar dari tren bearish
berdasarkan posisi harga terhadap SMA200/SMA50 harian.

```bash
python3 crypto/breakout.py              # semua (L1 + RWA)
python3 crypto/breakout.py --group rwa
python3 crypto/breakout.py SOL SUI ONDO
```

Kolom: `Hari>200` = hari sejak close di atas SMA200, `Bear%` = porsi hari
di bawah SMA200 selama 180 hari sebelum breakout, `GC` = golden cross
SMA50/SMA200 (berapa hari lalu), `Vol20/90` = volume 20 hari vs 90 hari.

## Scanner futures (`futures_scan.py`)
Mencari pair USDT likuid yang baru breakout (close > high 20 hari di 1D,
atau > high 7 hari di 4H dengan volume > 1,5x) atau breakdown, lalu
membuat rencana trade berbasis ATR 4H (entry retest, SL 1,2 ATR di bawah
level, TP 1,5R/3R) dan ukuran posisi dari risk per trade.

```bash
python3 crypto/futures_scan.py --capital 3634 --risk 1
```
Breakout yang harganya sudah kembali ke bawah level ditandai `GAGAL`.

## Bot Hyperliquid TESTNET (`hl_testnet_bot.py`)
Menjalankan sinyal `futures_scan.py` di **Hyperliquid testnet** (uang mainan)
dengan SL + TP otomatis. Terkunci ke testnet.

```bash
pip install -r crypto/requirements-hl.txt
export HL_ACCOUNT_ADDRESS=0x...   # wallet utama testnet
export HL_AGENT_KEY=0x...         # private key API wallet (agent), bukan wallet utama
python3 crypto/hl_testnet_bot.py plan      # lihat rencana, tidak mengirim apa pun
python3 crypto/hl_testnet_bot.py run       # kirim setelah konfirmasi "YA"
python3 crypto/hl_testnet_bot.py status
python3 crypto/hl_testnet_bot.py cancel-all
```
Batas: risk maks 2%/trade, leverage maks 5x (default 1% dan 3x isolated),
maks 2 posisi. Order dicatat di `crypto/hl_journal.csv` (tidak di-commit).
