# STREAM ROUTE — operasi

Halaman: [https://kiloview.cnn.co.id](https://kiloview.cnn.co.id)

Dua peran. Grup Keycloak `IT` mengurus input dan mesin. Grup `Transmission` (Feeddesk) menjalankan siaran: melihat preview dan menarik sumber ke SDI.

## Transmission / Feeddesk

Buka halaman, login dengan akun Transmission. Yang terlihat:

- Kiri: daftar input. Kartu hanya preview dan nama yang boleh diseret. Path ID, URL, dan tombol edit tidak ditampilkan.
- Kanan: **SDI 1** dan **SDI 2**.

### Memasang sumber ke SDI

1. Tunggu preview input bergerak. Kalau hitam, sumber belum masuk. Jangan diseret dulu.
2. Seret kartu input ke kotak SDI yang dituju.
3. Preview SDI mengikuti sumber yang sama dengan kabel SDI.
4. **Video mode** biarkan `1080i50`, kecuali diminta lain.
5. **Audio**
   - `1-2 · original stereo` — kiri tetap kiri, kanan tetap kanan
   - `1-1 · ch 1 to both` — channel 1 masuk ke kedua kanal
   - `2-2 · ch 2 to both` — channel 2 masuk ke kedua kanal
6. **Listen** dan **Monitor volume** hanya untuk speaker komputer ini. Tidak mengubah level di kabel SDI.
7. **Clear** melepas sumber dari slot itu. Kabel SDI berhenti mendapat gambar dari slot tersebut.

Dua soket BNC pada satu kartu menampilkan gambar yang sama. SDI 1 dan SDI 2 adalah dua kartu yang berbeda, jadi bisa dua gambar berbeda.

Kalau preview halaman hilang sebentar lalu kembali, halaman menyambung ulang sendiri. Gambar di kabel SDI tidak mengikuti volume browser.

Transmission tidak bisa menambah, mengubah, atau menghapus input. Itu tugas IT.

## IT admin

Akun harus ada di grup Keycloak `IT`. Di halaman, tombol **Add input** muncul, dan setiap kartu menampilkan display name, path ID, serta source URL.

### Menambah input

1. **Add input**.
2. **Display name** — nama yang dilihat Transmission, paling panjang 48 karakter.
3. **Path ID** — nama path MediaMTX. Huruf atau angka di depan. Boleh `_` dan `-`. Jangan pakai `sdi1` atau `sdi2`.
4. **Source URL** — URL lengkap, termasuk port kalau bukan port default.
   - SRT dari KiloLink: `srt://127.0.0.1:3001?mode=listener`
   - RTMP Univiso: `rtmp://IP/live/KODE?univiso_golivestream=1`
5. **Save**. Path langsung ditambahkan ke MediaMTX. Transmission bisa menyeretnya tanpa restart.

Mengganti display name tidak memutus path. Mengganti URL menulis ulang path itu. **Delete** ditolak selama input masih terpasang di SDI. Lepas dulu dengan **Clear**, baru hapus.

Port lokal ini tidak boleh dipakai di URL: `80`, `8090`, `8554`, `8888`, `8889`, `9997`, `1935`.

### Akun

Dibuat di Keycloak, realm `kiloview`.

- Masukkan user IT ke grup `IT`.
- Masukkan user Feeddesk ke grup `Transmission`.
- Jangan mengandalkan realm role `admin`. User Transmission yang juga punya role itu tetap tidak bisa mengedit input.

Login gagal `Invalid parameter: redirect_uri`: redirect URI client harus `https://kiloview.cnn.co.id/callback` tanpa slash. Logout gagal `Invalid redirect uri`: post logout harus `https://kiloview.cnn.co.id/` dengan slash.

### Setelah mengubah server

| Yang diubah | Perintah di Z420 |
| --- | --- |
| `operator/static` (tampilan) | `cd /storage/kiloview/operator && docker compose up -d --build` |
| `operator/config.env` di server | `docker restart operator` |
| Header `mediamtx.yml` (`webrtcAdditionalHosts`) | `docker restart mediamtx` |
| Input lewat halaman | Tidak perlu restart |
| Theme Keycloak | Copy folder `stream-route` ke host Keycloak. Jangan restart Keycloak kalau hanya CSS, kecuali nama theme baru belum muncul |

`config.env` yang diedit di laptop Windows tidak mengubah server. Edit file di `/storage/kiloview/operator/config.env`.

Jangan `docker compose down -v`. Volume `operator/data` menyimpan slot SDI dan daftar input.

### Cek cepat kalau preview kosong

1. Buka `http://172.17.20.34:8889` di jaringan kantor. Path sumber harus bisa diputar di sana.
2. Kalau langsung ke port itu jalan tetapi halaman `https://kiloview.cnn.co.id` kosong, env `MEDIAMTX_WEBRTC` di server harus `https://webrtc-kiloview.cnn.co.id`, lalu `docker restart operator`.
3. Halaman HTTPS tidak boleh meminta `http://` ke port 8889. Itu yang memblokir preview di browser.

### Cek cepat kalau SDI hitam

1. Di halaman, slot harus menunjukkan sumber, bukan Empty.
2. `docker logs operator` di Z420. Pesan DeckLink biasanya soal persistent ID atau format.
3. Jangan menukar SDI 1 dan SDI 2 lewat device-number. ID yang dipakai: SDI 1 `40604f81`, SDI 2 `49205acf`.

### Yang tidak dipegang dari halaman

- Restart Keycloak. Layanan itu dipakai aplikasi lain.
- Mengubah theme realm selain `kiloview`.
- Mematikan verifikasi TLS Keycloak.
- Mengubah pipeline GStreamer atau menganggap dua BNC pada satu kartu sebagai dua output berbeda.
