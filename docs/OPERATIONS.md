# STREAM ROUTE — operasi

Halaman: [https://kiloview.cnn.co.id](https://kiloview.cnn.co.id)

Dua peran. Grup Keycloak `IT` mengurus input, output, dan mesin. Grup `Transmission` (Feeddesk) menjalankan siaran: melihat preview dan menarik sumber ke output yang tidak terkunci.

## Transmission / Feeddesk

Buka halaman, login dengan akun Transmission. Yang terlihat:

- Kiri: daftar input. Kartu hanya preview dan nama yang boleh diseret. Path ID, URL, port, dan tombol edit tidak ditampilkan.
- Kanan: output. Awalnya **SDI 1** dan **SDI 2**. IT bisa menambah SDI, SRT, atau NDI.

### Memasang sumber

1. Tunggu preview input bergerak. Kalau hitam, sumber belum masuk. Jangan diseret dulu.
2. Seret kartu input ke output yang dituju.
3. Preview di halaman mengikuti sumber yang sama.
4. Pada output **SDI**, **Video mode** biarkan `1080i50`, kecuali diminta lain.
5. **Audio** hanya ada di SDI
   - `1-2 · original stereo` — kiri tetap kiri, kanan tetap kanan
   - `1-1 · ch 1 to both` — channel 1 masuk ke kedua kanal
   - `2-2 · ch 2 to both` — channel 2 masuk ke kedua kanal
6. **Listen** dan **Monitor volume** hanya untuk speaker komputer ini. Tidak mengubah level di kabel SDI, SRT, atau NDI.
7. **Clear** melepas sumber dari slot itu.

Output bertanda **Locked** tidak bisa diseret, diganti modenya, diganti audionya, atau di-Clear oleh Transmission. Listen tetap bisa dipakai.

Dua soket BNC pada satu kartu SDI menampilkan gambar yang sama. SDI 1 dan SDI 2 adalah dua kartu yang berbeda, jadi bisa dua gambar berbeda. SRT dan NDI tidak memakai kartu itu. Gambarnya adalah sumber asli, bukan 1080i50.

Kalau lapangan putus, sumber tetap menempel di output. Status menjadi **No signal**. Jangan Clear. Saat sinyal kembali, gambar nyambung sendiri tanpa diseret ulang.

Kalau preview halaman hilang sebentar lalu kembali, halaman menyambung ulang sendiri. Gambar di kabel SDI tidak mengikuti volume browser.

Transmission tidak bisa menambah, mengubah, atau menghapus input dan output. Itu tugas IT.

## IT admin

Akun harus ada di grup Keycloak `IT`. Di header ada tab **Route** dan **SRTLA**. Transmission hanya melihat Route. Di Route ada **Add input** dan **Add output**. Kartu input menampilkan display name, path ID, serta URL atau port.

### Menambah input

1. **Add input**.
2. **Display name** — nama yang dilihat Transmission, paling panjang 48 karakter.
3. **Path ID** — nama path MediaMTX. Huruf atau angka di depan. Boleh `_` dan `-`. Jangan pakai ID `sdi1`, `srt1`, `ndi1`, dan nomor berikutnya.
4. **Connection**
   - **Caller** — server yang menelepon. Isi **Remote URL**.
     - SRT dari KiloLink: `srt://127.0.0.1:3001?mode=listener`
     - RTMP Univiso: `rtmp://IP/live/KODE?univiso_golivestream=1`
   - **Listener** — server menunggu. Isi **Listen port** (UDP). Pengirim memakai `srt://172.17.20.34:<port>`. Buka port itu di firewall. UFW yang mati berarti firewall host tidak memblokir, tetapi port tetap harus benar-benar dipegang proses. Cek dengan `ss -ulpn | grep <port>`.
5. **Save**. Path langsung ditambahkan ke MediaMTX. Jangan `docker restart mediamtx`. Input yang sedang tayang tidak ikut putus.

Mengganti display name tidak memutus path. Mengganti URL caller menulis ulang path itu saja. **Delete** ditolak selama input masih terpasang di sebuah output. Lepas dulu dengan **Clear**, baru hapus.

Port ini tidak boleh dipakai: `80`, `8090`, `8189`, `8554`, `8888`, `8889`, `8890`, `9997`, `1935`.

Preview langsung MediaMTX, ganti `PATH` dengan Path ID: `http://172.17.20.34:8889/PATH`. Halaman itu menulis `stream not found` sampai ada yang benar-benar mengirim.

### Menambah output

1. **Add output**.
2. **Display name**.
3. **Type**
   - **SDI** — pilih kartu DeckLink. Kartu yang sudah dipakai tertulis dan tidak bisa dipilih.
   - **SRT → Listener** — isi port UDP. Penerima menelepon `srt://172.17.20.34:<port>`.
   - **SRT → Caller** — isi URL lawan, misalnya `srt://10.0.0.8:9000`.
   - **NDI** — isi nama yang muncul di jaringan NDI. Z420 harus punya NDI runtime dan plugin `ndisink`. Kalau belum, kartu menampilkan pesan, preview halaman tetap ada.
4. **Save**, lalu seret sumber ke kartu itu.

**Lock** pada sebuah output membuat Transmission tidak bisa mengubahnya. **Unlock** mengembalikan. **Remove** hanya bisa saat output sudah di-Clear.

Mode video dan peta audio hanya untuk SDI. SRT dan NDI mengirim sumber asli.

### SRTLA (bonding encoder)

Tab **SRTLA** hanya muncul untuk grup `IT`. Dipakai kalau encoder lapangan mengirim **SRTLA** (bukan SRT biasa ke port listener).

1. Buat dulu **input Listener** di tab Route (misalnya path `bond-a`, listen port `9101`). Port listener dan port SRTLA publik **harus beda**.
2. Buka tab **SRTLA** → **Add receiver**.
3. Isi **Public UDP port** — ini yang dibuka di firewall dan yang dipanggil encoder (bukan port listener).
4. Pilih **Listener input** yang barusan dibuat.
5. **SRT latency** dan **Reorder window** — mulai dari `2000 ms` dan `40`. Nilai ini juga diterapkan ke FFmpeg listener untuk input itu (bonding butuh jendela lebih lebar).
6. **Save**. Operator menjalankan `srtla_rec` dan me-restart listener terkait.

Di `operator/config.env` server, set `SRTLA_PUBLIC_HOST=172.17.20.34` (atau hostname publik) supaya form menampilkan URL encoder lengkap.

Encoder harus MPEG-TS **packet size 1316**. Bonding terlihat di kartu receiver (badge **bonded**) kalau ada **dua link** atau lebih dan sesi SRT sudah accepted.

Troubleshooting dari dashboard:

| Yang terlihat | Arti singkat |
| --- | --- |
| **waiting** | `srtla_rec` jalan, belum ada sesi SRT ke listener |
| **one link** | Satu uplink saja; bonding butuh link kedua |
| **bonded** | Sesi SRT OK + ≥2 link |
| Catatan merah di kartu | Penjelan otomatis (listener down, forward error, registrasi ditolak, dll.) |
| Tabel **Link** | Per IP uplink: rate, loss, RTT, weight, idle, errors |
| Blok log di bawah kartu | Tail `srtla_rec` |

**Restart** mengulang proses receiver. **Remove** menghapus receiver; input listener tetap ada.

Setelah mengubah tab SRTLA atau image operator: `cd /storage/kiloview/operator && docker compose up -d --build` (image harus berisi `srtla_rec`; kalau kartu bilang binary tidak ada, rebuild wajib).

Cek cepat di shell:

```bash
ss -ulpn | grep <port-publik-srtla>
docker exec operator sh -c 'tail -n 40 /data/logs/srtla1-srtla.log'
curl -s http://127.0.0.1:39100/metrics | head
```

Ganti `39100` dengan metrics port receiver (disimpan di `srtla.json`, tidak ditampilkan di UI).

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
| Input atau output lewat halaman | Tidak perlu restart MediaMTX. Operator yang baru di-build sudah menjalankan prosesnya |
| Theme Keycloak | Copy folder `stream-route` ke host Keycloak. Jangan restart Keycloak kalau hanya CSS, kecuali nama theme baru belum muncul |

`config.env` yang diedit di laptop Windows tidak mengubah server. Edit file di `/storage/kiloview/operator/config.env`.

Jangan `docker compose down -v`. Volume `operator/data` menyimpan rute, daftar input, dan daftar output.

### Cek cepat kalau preview kosong

1. Buka `http://172.17.20.34:8889` di jaringan kantor. Path sumber harus bisa diputar di sana.
2. Kalau langsung ke port itu jalan tetapi halaman `https://kiloview.cnn.co.id` kosong, env `MEDIAMTX_WEBRTC` di server harus `https://webrtc-kiloview.cnn.co.id`, lalu `docker restart operator`.
3. Halaman HTTPS tidak boleh meminta `http://` ke port 8889. Itu yang memblokir preview di browser.

### Cek cepat kalau SDI hitam

1. Di halaman, slot harus menunjukkan sumber, bukan Empty. **No signal** berarti rute masih ada, sumbernya yang belum masuk. Jangan Clear.
2. `docker logs operator` di Z420. Pesan DeckLink biasanya soal persistent ID atau format.
3. Jangan menukar SDI 1 dan SDI 2 lewat device-number. ID awal: SDI 1 `40604f81`, SDI 2 `49205acf`.

### Cek cepat kalau listener tidak muncul

1. Di kartu input, IT harus melihat `Listening on UDP <port>`. Kalau ada teks error di situ, baca itu.
2. `ss -ulpn | grep <port>` harus menunjukkan `ffmpeg`.
3. Log: `docker exec operator sh -c 'tail -n 40 /data/logs/*-listen.log'`
4. `http://172.17.20.34:8889/<path-id>` tetap `stream not found` sampai pengirim benar-benar masuk dan keyframe berikutnya lewat.

### Yang tidak dipegang dari halaman

- Restart Keycloak. Layanan itu dipakai aplikasi lain.
- Mengubah theme realm selain `kiloview`.
- Mematikan verifikasi TLS Keycloak.
- Mengubah pipeline GStreamer atau menganggap dua BNC pada satu kartu sebagai dua output berbeda.
