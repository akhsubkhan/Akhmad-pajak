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
