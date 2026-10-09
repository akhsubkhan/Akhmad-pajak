# Contoh Alur n8n: Pengingat Jatuh Tempo Pajak Klien

Setiap hari pukul 08.00 WIB, alur ini membaca daftar klien dari Google Sheets,
menghitung tenggat pajak terdekat, lalu mengirim pengingat lewat WhatsApp dan
email ketika tenggat tinggal **7, 3, 1, atau 0 hari**.

```
[Setiap Hari 08.00] → [Baca Daftar Klien] → [Hitung Tenggat & Buat Pesan] ─┬→ [Kirim WhatsApp (Fonnte)]
     Schedule            Google Sheets              Code (JavaScript)          └→ [Kirim Email (Gmail)]
```

## Tenggat yang dihitung (PMK 81/2024)

| Jenis (kolom `Jenis`) | Kewajiban | Tenggat |
|---|---|---|
| `PPh Masa` | Setor PPh Masa | tanggal 15 bulan berikutnya |
| `PPh Masa` | Lapor SPT Masa PPh | tanggal 20 bulan berikutnya |
| `PPN` | Setor & Lapor SPT Masa PPN | akhir bulan berikutnya |
| `SPT Tahunan OP` | Lapor SPT Tahunan Orang Pribadi | 31 Maret |
| `SPT Tahunan Badan` | Lapor SPT Tahunan Badan | 30 April |

Tenggat yang jatuh di hari Sabtu atau Minggu otomatis digeser ke hari Senin.
**Libur nasional belum dihitung.** Selalu cek ulang dengan aturan DJP terbaru.

## Cara memakai

1. **Siapkan Google Sheet** dengan tab bernama `Klien`. Isi kolomnya sesuai
   [`template-klien.csv`](template-klien.csv): `Nama, Email, WhatsApp, Jenis, Aktif`.
   Klien yang punya beberapa kewajiban ditulis di beberapa baris, satu baris
   per jenis. Isi `Aktif` dengan `Ya` supaya klien diingatkan.
2. **Import alur:** di n8n buka *Workflows → Import from File*, lalu pilih
   [`pengingat-pajak.json`](pengingat-pajak.json).
3. **Node "Baca Daftar Klien":** hubungkan akun Google Anda, lalu ganti
   `GANTI_DENGAN_ID_GOOGLE_SHEET` dengan ID sheet. ID sheet adalah bagian URL di antara `/d/` dan `/edit`.
4. **Node "Kirim WhatsApp (Fonnte)":** daftar di [fonnte.com](https://fonnte.com),
   sambungkan nomor WA Anda, lalu ganti `GANTI_DENGAN_TOKEN_FONNTE` dengan token Anda.
   Penyedia WA lain juga bisa dipakai, cukup ubah URL dan isi body-nya.
5. **Node "Kirim Email (Gmail)":** hubungkan akun Gmail Anda.
6. Klik **Execute Workflow** untuk mencoba, lalu aktifkan alur dengan tombol **Active**.

Kedua node pengirim memakai *continue on error*. Jadi kalau seorang klien tidak punya email,
pengingat WhatsApp untuknya tetap terkirim.

## Mengubah pengaturan

Semua pengaturan ada di node **Hitung Tenggat & Buat Pesan**:

- `HARI_PENGINGAT`: kapan pengingat dikirim, misalnya `[14, 7, 3, 1, 0]`.
- `KEWAJIBAN`: tambahkan jenis pajak lain, misalnya PPh Final UMKM.
- Teks pesan: ubah di bagian `const pesan = [...]`.

## Ide pengembangan

- Tambahkan kolom `Sudah Lapor` supaya klien yang sudah selesai tidak diingatkan lagi.
- Kirim ringkasan harian berisi semua tenggat ke WhatsApp Anda sendiri.
- Kumpulkan dokumen klien lewat Google Form, simpan otomatis di folder Drive per klien.
