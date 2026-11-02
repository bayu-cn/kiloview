# STREAM ROUTE — dokumen teknis

## Mesin dan layanan

Satu mesin HP Z420 (`172.17.20.34`) menjalankan KiloLink, MediaMTX, dan operator. Keycloak dan Nginx Proxy Manager ada di mesin lain.

| Layanan | Di mana | Port yang dipakai |
| --- | --- | --- |
| KiloLink Server | Docker, network host, Z420 | `80`, bonding `50000–50001`, AV `30000–30300`, SRT listener `3001+` |
| MediaMTX `bluenviron/mediamtx:1` | Docker, network host, Z420 | API `9997`, RTSP `8554`, HLS `8888`, WebRTC `8889`, RTMP `1935`, SRT `8890`, UDP media `8189` |
| Operator STREAM ROUTE | Docker, network host, privileged, Z420 | `8090` |
| Halaman publik | NPM | `https://kiloview.cnn.co.id` → `:8090` |
| Preview WebRTC | NPM | `https://webrtc-kiloview.cnn.co.id` → `:8889` |
| Keycloak | Mesin lain, bersama aplikasi lain | `https://login.cnn.co.id` |

P3 Mini hanya bonding ke KiloLink. Mereka tidak mengirim NDI ke mesin ini. NDI Core, `ndi_matrix`, dan OBS di bawah `/storage` tidak dipakai.

## Jalur sinyal

```text
P3 Mini
  bonding  -->  KiloLink Server (Docker, port 80)
                  streaming service SRT listener, satu service per P3, latency sekitar 120 ms
                    -->  srt://127.0.0.1:3001 (dan port berikut)
                          -->  MediaMTX path, misalnya p3-1
                                -->  halaman STREAM ROUTE (preview WebRTC)
                                -->  GStreamer host  -->  DeckLink  -->  kabel SDI
```

Sumber tidak harus SRT. Halaman admin menyimpan satu URL untuk input **caller**. MediaMTX menarik URL itu. RTMP Univiso berbentuk `rtmp://IP/live/KODE?univiso_golivestream=1`. RTMP listen di port `1935`. Banyak stream caller dibedakan oleh path, bukan oleh banyak port listen.

Input **listener** beda. MediaMTX hanya punya satu server SRT, bawaan UDP `8890`. Supaya tiap input bisa punya port sendiri, container operator menjalankan FFmpeg:

```text
pengirim luar  -->  srt://172.17.20.34:<port>  (FFmpeg di operator, mode listener)
                      -->  srt://127.0.0.1:8890?streamid=publish:<path-id>&pkt_size=1316
                            -->  path MediaMTX
```

**SRTLA** (bonding beberapa uplink encoder) memakai `srtla_rec` di image operator. Bukan skema URL MediaMTX. Satu receiver SRTLA = satu port UDP publik + satu input listener yang sudah ada:

```text
encoder SRTLA (2+ link)  -->  UDP publik :<srtla-port>  (srtla_rec)
                                 -->  SRT caller ke 127.0.0.1:<port listener input>
                                       -->  FFmpeg listener (latency/reorder diset dari dashboard)
                                             -->  MediaMTX  -->  SDI / SRT / NDI keluar
```

Konfigurasi receiver disimpan di `data/srtla.json`. Proses `srtla_rec` mengekspor metrik Prometheus di `127.0.0.1:<metricsPort>` (rentang otomatis sekitar `39100+`). Halaman **SRTLA** di operator (hanya grup IT) menambah/mengedit receiver, menampilkan status per link, bitrate, loss, RTT, catatan troubleshooting, dan tail log `/data/logs/srtlaN-srtla.log`. API: `GET/POST /api/srtla`, `POST /api/srtla/{id}/restart`, `POST /api/srtla/{id}/delete` — semua membutuhkan sesi admin (grup `IT`).

Env opsional di `config.env`: `SRTLA_PUBLIC_HOST` (IP atau hostname yang ditampilkan ke encoder, misalnya `172.17.20.34`), `SRTLA_REC` (default `/usr/local/bin/srtla_rec`).

Isi yang diteruskan adalah MPEG-TS asli. Menyalin ke RTSP membuat MediaMTX tetap `stream not found` karena SPS/PPS hilang. Container MediaMTX tidak di-restart saat input baru ditambah. Port listener itu harus dibuka di firewall host sebagai UDP. `ports:` di Compose tidak dipakai karena kedua container `network_mode: host`.

Keluaran SRT dan NDI juga proses terpisah di operator, membaca RTSP sumber yang sama. SRT listener membuka port UDP yang dipilih IT. SRT caller menelepon URL lawan. Keduanya `-c copy`. NDI memakai GStreamer host (`ndisink` + `ndisinkcombiner`) dan mengirim video UYVY plus audio F32LE. Itu bukan format 1080i50 kabel SDI.

Dari MediaMTX ke SDI, operator membuka RTSP `rtsp://127.0.0.1:8554/<path-id>` dengan TCP.

Pipeline SDI memisahkan video dan audio di `rtspsrc`:

1. Video: `decodebin` → `videorate` 50/1 → `interlace` 2:2 top-field-first → `UYVY` → `decklinkvideosink` mode `1080i50`, `sync=false`.
2. Audio: `decodebin` → peta kanal → `S32LE` 48 kHz stereo → `decklinkaudiosink` `sync=false` `async=false`.

Mode `1080p60` melewatkan interlace dan mengirim 60/1 progressive. Standar lapangan adalah `1080i50`.

Preview di browser tidak mengambil gambar dari kartu SDI. FFmpeg di dalam container membaca RTSP sumber yang sama, mengecilkan ke 640×360 25 fps, H.264 baseline + Opus, lalu publish ke path `sdi1` atau `sdi2`. Browser memutarnya lewat WHEP. Tombol Listen dan slider volume hanya untuk halaman itu.

## Isi repo

```text
mediamtx/          MediaMTX compose dan mediamtx.yml
operator/          aplikasi STREAM ROUTE, UI, Dockerfile, config.env
keycloak/stream-route/   theme login realm kiloview
docs/              konteks AI, teknis, operasi
```

Operator adalah Python stdlib (`ThreadingHTTPServer`), bukan framework web. Tiga file di volume Docker `./data` pada server:

| File | Isi |
| --- | --- |
| `state.json` | Sumber, mode video, peta audio, dan kunci tiap output |
| `sources.json` | Katalog input: nama, path, caller/listener, URL atau port |
| `outputs.json` | Daftar output: SDI (persistent ID), SRT (arah dan port/URL), NDI (nama) |
| `srtla.json` | Receiver SRTLA: port publik, `sourceId` listener, `latencyMs`, `lossmaxttl`, `metricsPort` |

Dua SDI awal diisi dari `SDI1_PERSISTENT_ID` dan `SDI2_PERSISTENT_ID` hanya saat `outputs.json` belum ada. Setelah file itu ada, daftar output mengikuti file, bukan env.

`mediamtx.yml` di git masih menulis `webrtcAdditionalHosts: [172.17.20.46]`. Di server, header itu harus `172.17.20.34`. Operator hanya menulis ulang mulai dari `paths:` ke bawah. Header tidak ikut tertimpa saat seseorang menambah input dari halaman.

## Instalasi di Z420

Syarat host:

- Docker dengan Compose v2
- Driver Blackmagic Desktop Video, dan kedua kartu Extreme terlihat
- GStreamer 1.x di host, termasuk `gst-launch-1.0` dan plugin DeckLink
- KiloLink sudah jalan di network host

```bash
cd /storage/kiloview
git pull

cd mediamtx
docker compose up -d

cd ../operator
docker compose up -d --build
```

`config.env` di server, bukan salinan Windows, harus berisi kira-kira:

```text
BIND=0.0.0.0
PORT=8090
PUBLIC_URL=https://kiloview.cnn.co.id
MEDIAMTX_PUBLIC=http://127.0.0.1:8888
MEDIAMTX_WEBRTC=https://webrtc-kiloview.cnn.co.id
MEDIAMTX_API=http://127.0.0.1:9997
RTSP_BASE=rtsp://127.0.0.1:8554
SDI1_PERSISTENT_ID=40604f81
SDI2_PERSISTENT_ID=49205acf
KEYCLOAK_URL=https://login.cnn.co.id
KEYCLOAK_REALM=kiloview
KEYCLOAK_CLIENT_ID=kiloview
KEYCLOAK_CLIENT_SECRET=<hanya di server>
ADMIN_GROUP=IT
OPERATOR_GROUP=Transmission
```

`KEYCLOAK_SSL_VERIFY` tidak perlu diisi. Container memakai bundel CA host.

Setelah mengubah `config.env`: `docker restart operator`. Setelah mengubah HTML, CSS, atau JS: `docker compose up -d --build` di folder `operator`.

## Keycloak

Realm `kiloview`, client `kiloview`:

- Standard flow, client confidential
- Valid redirect URI: `https://kiloview.cnn.co.id/callback` (tanpa slash di akhir)
- Valid post logout redirect URI: `https://kiloview.cnn.co.id/` (dengan slash)
- Web origins: `https://kiloview.cnn.co.id`
- Mapper **Group Membership**, bukan User Realm Role. Claim name `groups`, full path on, masuk ke ID token, access token, lightweight access token, dan userinfo

Grup:

- `IT` — admin halaman
- `Transmission` — operator lapangan

Theme `stream-route` disalin ke `/opt/keycloak/themes/stream-route` di host Keycloak. Realm kiloview → Themes → Login theme = `stream-route`. Realm lain jangan diubah.

Production Keycloak meng-cache theme. CSS yang baru tidak otomatis dipakai browser: resource di-cache 30 hari, dan permintaan gzip pernah tertahan pada salinan lama. Di NPM, custom location `/resources` (bukan kotak Advanced host) perlu:

```nginx
proxy_set_header Accept-Encoding "";
```

Uji dengan jendela privat, atau dengan:

```powershell
curl.exe -s --compressed -H "Accept-Encoding: gzip, deflate, br, zstd" "https://login.cnn.co.id/resources/qbnzb/login/stream-route/css/stream.css" | findstr stream-eye
```

`qbnzb` adalah versi resource yang terpasang sekarang. Angka itu bisa beda setelah Keycloak di-build ulang. `findstr` harus menampilkan `button.stream-eye`.

## NPM

| Host | Tujuan | Catatan |
| --- | --- | --- |
| `kiloview.cnn.co.id` | `http://172.17.20.34:8090` | Halaman operator |
| `webrtc-kiloview.cnn.co.id` | `http://172.17.20.34:8889` | WebSocket on. Jangan mem-proxy UDP 8189 |
| `login.cnn.co.id` | Keycloak | Sudah ada, dipakai bersama |

`webrtcAdditionalHosts` tetap IP LAN Z420 supaya ICE mengirim media langsung ke kartu jaringan, bukan ke NPM.

## DeckLink

Dua kartu awal **DeckLink 4K Extreme**. Masing-masing satu kanal playback. Persistent ID (dari firmware, ditulis tanpa `0x`):

- SDI 1: `40604f81`
- SDI 2: `49205acf`

GStreamer memakai ID itu sebagai bilangan desimal, lalu mengabaikan device-number. Kalau ID kosong, cadangannya device 0 untuk SDI 1 dan device 1 untuk SDI 2.

IT menambah SDI dari halaman. Operator menjalankan `gst-device-monitor-1.0` di host dan hanya menampilkan perangkat `Video/Sink` yang punya persistent ID. ID yang sudah terpasang tetap muncul, tetapi tidak bisa dipilih. Kartu fisik harus sudah terlihat driver Blackmagic. Bukan Duo 2.

## Katalog input

Path ID: huruf atau angka di depan, lalu huruf, angka, `_`, `-`, paling panjang 41 karakter. ID berbentuk `sdiN`, `srtN`, atau `ndiN` tidak boleh dipakai sebagai ID sumber.

URL lokal dan port listener tidak boleh memakai `80`, `8090`, `8189`, `8554`, `8888`, `8889`, `8890`, `9997`, atau `1935`. Port yang sama tidak boleh dipakai dua input atau satu input dan satu output listener.

Menyimpan input baru menulis path ke `mediamtx.yml` dan memanggil API `POST /v3/config/paths/add/{name}`. Mengganti URL caller memakai `replace` pada path itu saja. Mengganti display name saja tidak membuat path ulang. Hapus ditolak kalau sumber itu sedang dipasang ke sebuah output.

Saat container operator mulai: katalog dimuat, yml ditulis, path yang belum ada di-add (yang sudah ada tidak di-replace), listener yang tersimpan dibuka lagi, lalu pipeline output yang tersimpan dinyalakan lagi.

## Keluaran

`outputs.json` menyimpan jenis tiap slot.

| Jenis | Proses | Catatan |
| --- | --- | --- |
| SDI | GStreamer host ke DeckLink | `1080i50` atau `1080p60`, plus peta audio |
| SRT listener | FFmpeg `-c copy` ke `srt://0.0.0.0:<port>?mode=listener` | Penerima menelepon IP server. Firewall UDP |
| SRT caller | FFmpeg `-c copy` ke URL lawan | Server yang menelepon |
| NDI | GStreamer host `ndisink` | Butuh `ndisink` dan NDI runtime di host |

Preview browser tetap FFmpeg di dalam image: RTSP sumber diperkecil dan dipublish ke path MediaMTX dengan ID slot (`sdi1`, `srt1`, `ndi1`, …). Bukan gambar dari kartu atau dari proses NDI.

Sinyal sumber hilang (RTSP 404): slot tidak dikosongkan. Status **No signal**, pipeline dan preview mencoba lagi. Saat sinyal kembali, status menjadi **On air**. Kunci output (`locked` di `state.json`) hanya boleh diubah grup IT.
