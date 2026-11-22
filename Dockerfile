FROM node:20-slim

# Pasang Python 3, NumPy, Rich, dan FFmpeg (untuk nyahkod voice note .ogg/.opus WhatsApp)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-numpy \
    python3-rich \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pasang kebergantungan Node.js untuk WhatsApp QR (Baileys)
COPY whatsapp_qr/package.json ./whatsapp_qr/
RUN cd whatsapp_qr && npm install --omit=dev

# Salin keseluruhan kod CW Decoder
COPY . .

ENV PORT=10000
ENV PYTHON_BIN=python3
ENV AUTH_DIR=/app/whatsapp_qr/auth_info

EXPOSE 10000

CMD ["node", "whatsapp_qr/index.js"]
