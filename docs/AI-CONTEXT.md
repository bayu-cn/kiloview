# Konteks AI — STREAM ROUTE

Pakai dokumen ini saat melanjutkan project. Jangan menebak alamat produksi dari file git. Beberapa file di repo masih menyimpan IP lama.

## Apa ini

Pengganti NDI Core untuk lapangan CNN. Empat (atau lebih) Kiloview P3 Mini masuk lewat bonding ke KiloLink Server, lalu SRT/RTMP masuk MediaMTX. Operator menarik sumber ke dua keluaran SDI DeckLink. Halaman operator bernama **STREAM ROUTE**. Login lewat Keycloak.

Mesin produksi adalah HP Z420, IP `172.17.20.34`, user Linux `itcnnid`. Repo di server biasanya `/storage/kiloview`. Git: `https://github.com/bayu-cn/kiloview.git`, branch `main`. Deploy dari Windows `D:\Project\kiloview` dengan git push, lalu di server `git pull` dan Docker.

Keycloak **bukan** di mesin Z420. Itu layanan bersama di mesin lain, URL `https://login.cnn.co.id`, dipakai aplikasi lain. Jangan restart Keycloak, jangan ubah JVM, dan jangan `docker compose down -v`.

## Yang sedang produksi

| Hal | Nilai yang benar |
| --- | --- |
| Halaman operator | `https://kiloview.cnn.co.id` (NPM → `172.17.20.34:8090`) |
| WebRTC preview | `https://webrtc-kiloview.cnn.co.id` (NPM → `:8889`, websocket nyala) |
| UDP media WebRTC | Langsung ke Z420, bukan lewat NPM. `webrtcAdditionalHosts` di server harus `172.17.20.34` |
| Realm / client | `kiloview` / `kiloview` (confidential, standard flow) |
| Grup admin | `IT` — boleh ubah input |
| Grup operator | `Transmission` — preview dan drag saja |
| SDI 1 | DeckLink 4K Extreme, persistent ID `40604f81` |
| SDI 2 | DeckLink 4K Extreme, persistent ID `49205acf` |
| Standar gambar SDI | `1080i50` |
| Theme login | `stream-route`, parent `keycloak`, hanya realm kiloview |

`operator/config.env` di Windows dan `mediamtx/mediamtx.yml` di git masih menyebut IP lama (`172.17.20.61`, `172.17.20.46`). Itu bukan yang dijalankan server. Jangan commit `config.env`. File itu berisi client secret.

## Yang tidak boleh diubah

- Jangan tee pipeline GStreamer. Video dan audio harus tetap terpisah dari `rtspsrc` (`media=video` dan `media=audio`), masing-masing `decodebin`.
- Jangan paksa 25 fps ke DeckLink. Video SDI: `videorate` ke `50/1`, lalu `interlace` 2:2 top-field-first, format `UYVY`. Audio DeckLink: `S32LE` 48 kHz stereo, bukan `S16LE`.
- `decklinkvideosink` `sync=false`. Jangan set `async=false` di video sink. Audio sink: `sync=false` dan `async=false`.
- Dua kartu ini Extreme, bukan Duo 2. Satu kartu hanya satu kanal playback. Dua BNC di satu kartu menampilkan gambar yang sama. Persistent ID menang atas `device-number`.
- Jangan jalankan GStreamer DeckLink dari binary di dalam image. Pakai GStreamer host: `GST_LAUNCH=/host-usr/bin/gst-launch-1.0`. Preview browser pakai `ffmpeg` di dalam image.
- Realm lain di Keycloak harus tetap theme lama. Hanya realm `kiloview` yang memakai `stream-route`.
- Jangan set `KEYCLOAK_SSL_VERIFY=false`. Trust CA host lewat mount `ca-certificates.crt` dan `SSL_CERT_FILE`.
- Static UI di-copy ke dalam image. Perubahan `operator/static` butuh `docker compose up -d --build`, bukan restart saja. `config.env` di-bind mount dan dibaca saat proses mulai, jadi perubahan env cukup `docker restart operator`.
- MediaMTX tidak hot-reload file yml. API mengubah path di memori. Operator menulis ulang yml dari baris `paths:` ke bawah dan mempertahankan header, termasuk `webrtcAdditionalHosts`.
- Jangan commit `__pycache__`, `config.env`, atau secret.

## Peta file

| Path | Peran |
| --- | --- |
| `operator/app.py` | HTTP server, login, katalog sumber, pipeline SDI dan preview |
| `operator/static/` | UI STREAM ROUTE. Bahasa Inggris |
| `operator/config.env` | Env runtime. Hanya di server yang berlaku. Jangan di-commit |
| `operator/docker-compose.yml` | Container operator, network host, privileged, mount host gst dan CA |
| `operator/data/` | `state.json` (slot SDI) dan `sources.json` (katalog). Ada di server, volume `./data` |
| `mediamtx/mediamtx.yml` | Path MediaMTX. Header di server harus IP Z420 |
| `mediamtx/docker-compose.yml` | `bluenviron/mediamtx:1`, network host, yml read-only |
| `keycloak/stream-route/` | Theme login. Dipasang dengan copy ke host Keycloak, bukan lewat image operator |
| `docs/` | Dokumen ini, teknis, dan operasi |

`keycloak/stream-route/login/preview.html` hanya pratinjau CSS lokal. Bukan halaman produksi.

## Perilaku yang sudah dikunci

- Sumber disimpan sebagai URL, bukan port. Skema: `srt`, `rtsp`, `rtsps`, `rtmp`, `rtmps`, `udp`, `http`, `https`. Contoh Univiso: `rtmp://IP/live/KODE?univiso_golivestream=1` (port RTMP default 1935).
- Path ID: `[A-Za-z0-9][A-Za-z0-9_-]{0,40}`. `sdi1` dan `sdi2` terlarang sebagai ID sumber. Port lokal yang diblokir: `80`, `8090`, `8554`, `8888`, `8889`, `9997`, `1935`.
- Audio: `1-2` stereo asli (tanpa matrix), `1-1` channel 1 ke kedua sisi, `2-2` channel 2 ke kedua sisi. Slider volume di halaman hanya untuk dengar di browser, tidak mengubah gain SDI.
- Grup dibaca dari access token dan ID token, dari claim yang namanya mengandung `group`. Kalau user ada di grup operator dan tidak di grup admin, dia bukan admin walaupun punya realm role admin.
- Redirect login: `https://kiloview.cnn.co.id/callback` tanpa slash. Post logout: `https://kiloview.cnn.co.id/` dengan slash.
- Theme Keycloak 26 memakai PatternFly. Tombol mata password memakai class `stream-eye` lewat `theme.properties`. Browser meminta CSS terkompres. Salinan gzip lama bisa tertahan di depan Keycloak. `proxy_set_header Accept-Encoding ""` harus ada di **custom location** `/resources`, bukan di kotak Advanced host NPM, karena `location /` sudah punya `proxy_set_header` sendiri dan nginx tidak mewarisi yang di level server.
- CSS theme di-cache `max-age=2592000`. Versi URL (`qbnzb` atau apa pun hash resource Keycloak) tidak berubah saat file CSS diganti. Setelah CSS benar-benar terkirim, uji di jendela privat.

## Cara lanjut yang aman

1. Baca `docs/TECHNICAL.md` untuk jalur sinyal dan instalasi.
2. Baca `docs/OPERATIONS.md` sebelum mengubah alur IT atau Transmission.
3. Ubah kode di Windows, commit hanya jika diminta, push `main`, pull di server.
4. UI operator: rebuild image. Env: restart container `operator`. MediaMTX yml header: restart container `mediamtx` hanya jika header berubah. Path yang diubah dari halaman operator tidak butuh restart MediaMTX.
5. Jangan menyentuh Keycloak produksi untuk percobaan theme kalau belum jelas cache gzip-nya.
