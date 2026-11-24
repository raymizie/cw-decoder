# CW Decoder

**CW Decoder** ialah alat nyahkod bunyi **Continuous Wave (Kod Morse)** kepada perkataan dalam **3 versi** yang berkongsi satu enjin DSP yang sama:

1. **CLI** (`cw_cli.py`) — Nyahkod fail audio, rakam terus dari mikrofon, jana bunyi CW untuk ujian, dan tukar teks ↔ Morse.
2. **Telegram Bot** (`run_telegram.py`) — Bot *long-polling* yang menerima *voice note* / fail audio dan membalas hasil nyahkod secara automatik.
3. **WhatsApp Bot** (`run_whatsapp.py`) — Pelayan webhook Flask yang menyokong **Meta WhatsApp Cloud API** dan **Twilio WhatsApp API**.

## Ciri Enjin CW Decoder
- **Auto-detect frekuensi nada (Hz)** menggunakan analisis spektrum STFT + demodulasi kuadratur (I/Q).
- **Auto-detect kelajuan (WPM)** & sokongan jarak *Farnsworth* melalui pengelompokan adaptif.
- **Sokongan penuh ITU Morse**: huruf A–Z, nombor 0–9, tanda baca, dan *prosign* (`<SOS>`, `<SK>`, `<KA>`, `<BK>`, `<SN>`, `<HH>`).
- **Mod Encode**: jana fail bunyi CW `.wav` daripada teks (dengan pilihan WPM, frekuensi, Farnsworth & hingar ujian).
- **Mod Mikrofon CLI**: menyokong `sounddevice`, `pyaudio`, dan *fallback* terbina dalam Windows (`winmm`).

---

## Pemasangan

Pastikan Python 3.10+ dipasang, kemudian pasang kebergantungan:

```bash
pip install -r requirements.txt
```

> **Penting untuk Voice Note WhatsApp & Telegram (`.ogg` / `.opus` / `.mp3` / `.m4a`):**
> Fail `.wav` disokong secara natif tanpa perisian tambahan. Untuk format voice note WhatsApp & Telegram, pasang **FFmpeg** pada `PATH`, atau jalankan:
> ```bash
> pip install imageio-ffmpeg
> ```

Salin fail konfigurasi:
```bash
copy .env.example .env
```

---

## 1. Versi CLI (`cw_cli.py`)

```bash
# Semak status persekitaran (FFmpeg & mikrofon)
python cw_cli.py doctor

# Jana fail audio CW untuk ujian
python cw_cli.py encode "HELLO WORLD 2026" -o demo.wav --wpm 20 --freq 700

# Nyahkod fail audio CW kepada perkataan
python cw_cli.py decode demo.wav

# Nyahkod dengan output JSON atau tetapan manual
python cw_cli.py decode demo.wav --json
python cw_cli.py decode demo.wav --freq 700 --wpm 20

# Rakam bunyi CW terus daripada mikrofon (8 saat) dan nyahkod
python cw_cli.py listen --duration 8

# Tukar teks <-> kod Morse tanpa audio
python cw_cli.py morse --encode "CQ DE 9M2CW <SK>"
python cw_cli.py morse ".... . .-.. .-.. ---"
```

---

## 2. Versi Telegram (`run_telegram.py`)

1. Cipta bot melalui `@BotFather` di Telegram dan dapatkan **Bot Token**.
2. Masukkan token ke dalam `.env`:
   ```env
   TELEGRAM_BOT_TOKEN=123456789:ABCdef...
   ```
3. Jalankan bot:
   ```bash
   python run_telegram.py
   ```
4. Hantar **voice note** atau fail audio CW kepada bot anda! Anda juga boleh guna arahan:
   - `/encode HELLO WORLD`, `/encode --wpm 18 --freq 750 CQ CQ`, atau **Reply** mana-mana mesej teks dengan `/encode`
   - `/morse .... . .-.. .-.. ---`
   - `/tomorse HELLO`
   - `/status`

---

## 3. Versi WhatsApp — Tanpa Token (Imbas Kod QR) ⭐ *Disyorkan untuk Render*

Tidak perlu akaun Meta Developers, tidak perlu Twilio, dan **tiada sebarang token**! Hanya imbas **Kod QR** daripada aplikasi WhatsApp telefon anda (*Linked Devices / Peranti Dipautkan*).

### Deploy ke GitHub + Render (Percuma, Tanpa Token)
Projek ini menggunakan [Dockerfile](Dockerfile) (Node.js Baileys + Python CW Decoder + FFmpeg) dan [render.yaml](render.yaml):

1. **Tolak projek ke GitHub**:
   ```bash
   git init
   git add .
   git commit -m "Initial commit: CW Decoder (CLI, Telegram, WhatsApp QR)"
   git branch -M main
   git remote add origin https://github.com/<USERNAME_ANDA>/cw-decoder.git
   git push -u origin main
   ```
2. **Deploy di Render**:
   - Buka [dashboard.render.com](https://dashboard.render.com) → **New +** → **Web Service** (atau **Blueprint**).
   - Sambungkan repositori GitHub `cw-decoder`.
   - Pilih **Runtime / Language**: `Docker` (atau biar Render kesan `Dockerfile` secara automatik).
   - **Tiada Environment Variable / Token wajib diisi!** Terus klik **Create Web Service**.
3. **Imbas Kod QR (Sokongan Multi-Pengguna — Maksimum 10 Orang Serentak)**:
   - Selepas *deploy* selesai, buka URL Render anda di pelayar:
     `https://<nama-app-anda>.onrender.com/qr`
   - Di telefon anda: buka **WhatsApp → Peranti Dipautkan (Linked Devices) → Pautkan Peranti** → imbas kod QR di skrin.
   - Selepas seorang pengguna berjaya paut peranti, **kod QR baharu akan dijana secara automatik** untuk pengguna seterusnya sehingga maksimum **10 pengguna serentak** (`MAX_SESSIONS=10`).
   - Halaman `/qr` memaparkan **info pengguna aktif** (nama, nombor, masa mula, baki masa secara langsung, statistik decode/encode, dan butang Log Keluar).
   - Setiap pengguna mempunyai tempoh aktif selama **1 jam** (`SESSION_TTL_MINUTES=60`) selepas bersambung, kemudian **auto-logout** dan slot akan kosong untuk orang lain. Taip `/masa` di WhatsApp untuk semak baki masa sesi.
4. **Cuba terus di WhatsApp (Chat Peribadi & Group)**:
   - **Chat peribadi / diri sendiri (*Message Yourself*)**:
     - Taip `/help`
     - Taip `/encode HELLO WORLD` atau **Reply** mana-mana mesej teks dengan `/encode` → bot balas *voice note* bunyi CW!
     - Hantar **voice note / audio bunyi CW** → bot terus balas teks dinyahkod, kod Morse, WPM & frekuensi (Hz)!
   - **Dalam WhatsApp Group** (cuma masukkan nombor bot ke dalam group):
     - **Cara 1 (On-demand — default, tidak ganggu voice note biasa)**: *Reply* mana-mana *voice note* / fail audio dalam group dengan `/decode` (atau `!decode`).
     - **Cara 2 (Auto-decode semua audio dalam group tersebut)**: Taip `/cwauto on` dalam group (taip `/cwauto off` untuk matikan), atau tetapkan `ALLOW_GROUP_AUDIO=true` pada Environment Variables di Render supaya aktif untuk semua group.
     - Arahan `/help`, `/encode <teks>` (atau *Reply* teks dengan `/encode`), `/morse <kod>`, `/tomorse <teks>` sentiasa berfungsi dalam group (termasuk daripada nombor anda sendiri).

> **Nota Render Free Tier**: Pelan percuma Render memadam cakera sementara apabila servis dimulakan semula / di-deploy semula, jadi jika servis tidur lama atau di-redeploy, cuma buka `/qr` semula dan imbas dalam 5 saat.

### Keep-Alive Multi-Servis dengan UptimeRobot (`/health`)
Anda boleh gunakan **1 monitor UptimeRobot sahaja** (`https://<nama-app-anda>.onrender.com/health`, interval **5 minit**) untuk kekalkan servis ini **dan** ping servis-servis lain anda secara automatik:
1. Di **Render → Environment Variables**, tambah `KEEPALIVE_URLS` dengan senarai URL servis lain (dipisahkan koma):
   ```env
   KEEPALIVE_URLS=https://servis-1.onrender.com/health,https://servis-2.onrender.com/health
   ```
2. Setiap kali UptimeRobot memanggil `/health` (`GET` atau `HEAD`), servis ini akan membalas `200 OK` serta-merta dan mem-ping semua URL dalam `KEEPALIVE_URLS` di latar belakang (had sela minimum default 60 saat; tambah `?force=1` untuk ujian segera).
3. Buka `/health` di pelayar untuk melihat status setiap servis di bawah medan `keepalive.targets`.

### Pilihan Alternatif: WhatsApp Webhook API (Meta Cloud API / Twilio)
Jika anda mahu guna API rasmi Meta Cloud API atau Twilio melalui Flask ([run_whatsapp.py](run_whatsapp.py) / [wsgi.py](wsgi.py)):
1. Isi kelayakan dalam `.env` (`META_*` atau `TWILIO_*`).
2. Jalankan `python run_whatsapp.py` atau di Render guna `gunicorn wsgi:app --bind 0.0.0.0:$PORT`.
3. Tetapkan webhook ke `/webhook/meta` atau `/webhook/twilio`.

---

## Ujian Automatik

```bash
pytest -v
```
