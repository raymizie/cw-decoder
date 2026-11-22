/**
 * CW Decoder - Bot WhatsApp Tanpa Token (Imbas Kod QR via Baileys)
 *
 * Cara guna:
 * - Jalankan di Render (Docker) atau secara lokal (`node whatsapp_qr/index.js`)
 * - Buka `/qr` di pelayar dan imbas kod QR guna WhatsApp -> Peranti Dipautkan (Linked Devices)
 * - Hantar voice note / audio CW (atau taip `/encode HELLO`) ke bot atau ke chat diri sendiri!
 */

"use strict";

const { execFile } = require("child_process");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");
const { promisify } = require("util");

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  jidNormalizedUser,
  makeCacheableSignalKeyStore,
  Browsers,
} = require("@whiskeysockets/baileys");
const pino = require("pino");
const QRCode = require("qrcode");

const execFileAsync = promisify(execFile);

const PORT = Number(process.env.PORT || 10000);
const PYTHON_BIN = process.env.PYTHON_BIN || (process.platform === "win32" ? "python" : "python3");
const FFMPEG_BIN = process.env.FFMPEG_PATH || "ffmpeg";
const PROJECT_ROOT = path.resolve(__dirname, "..");
const CW_CLI_PATH = path.join(PROJECT_ROOT, "cw_cli.py");
const AUTH_DIR = process.env.AUTH_DIR || path.join(__dirname, "auth_info");
const ALLOW_GROUP_AUDIO = String(process.env.ALLOW_GROUP_AUDIO || "false").toLowerCase() === "true";
const KEEPALIVE_URLS = Array.from(
  new Set(
    String(process.env.KEEPALIVE_URLS || "")
      .split(/[\s,]+/)
      .map((u) => u.trim())
      .filter((u) => /^https?:\/\//i.test(u))
  )
);
const rawKeepAliveTimeout = Number(process.env.KEEPALIVE_TIMEOUT_MS);
const KEEPALIVE_TIMEOUT_MS = Math.max(
  2000,
  Math.min(60000, Number.isFinite(rawKeepAliveTimeout) && rawKeepAliveTimeout > 0 ? rawKeepAliveTimeout : 15000)
);
const rawKeepAliveInterval = Number(process.env.KEEPALIVE_MIN_INTERVAL_MS);
const KEEPALIVE_MIN_INTERVAL_MS = Math.max(
  5000,
  Math.min(3600000, Number.isFinite(rawKeepAliveInterval) && rawKeepAliveInterval > 0 ? rawKeepAliveInterval : 60000)
);

const HELP_TEXT = [
  "📡 *CW Decoder — Bot WhatsApp (Tanpa Token)*",
  "Hantar *voice note* atau fail audio CW (WAV/OGG/MP3/M4A) dan saya akan nyahkod bunyi Morse kepada perkataan secara automatik!",
  "",
  "*Arahan Tersedia:*",
  "• Hantar voice note / audio → Nyahkod CW kepada teks",
  "• Reply `/decode` pada mana-mana voice note/audio → Nyahkod audio tersebut (sesuai untuk Group!)",
  "• `/cwauto on` / `/cwauto off` → Hidup/matikan auto-decode semua audio dalam Group ini",
  "• `/encode <teks>` → Jana voice note bunyi CW untuk ujian",
  "  _(Contoh: `/encode --wpm 18 --freq 700 HELLO WORLD`)_",
  "• `/morse <kod>` → Nyahkod kod Morse teks",
  "  _(Contoh: `/morse .... . .-.. .-.. ---`)_",
  "• `/tomorse <teks>` → Tukar teks kepada simbol Morse",
  "• `/help` → Papar panduan ini",
].join("\n");

const BOT_REPLY_PREFIXES = ["📡", "⚠️", "❌", "🔤", "•−", "🔊", "✅"];

// Per-group auto-decode overrides toggled via `/cwauto on` or `/cwauto off`
const groupAutoDecode = new Map();
function isGroupAutoEnabled(jid) {
  if (groupAutoDecode.has(jid)) return groupAutoDecode.get(jid);
  return ALLOW_GROUP_AUDIO;
}

// State for web dashboard & QR page
const state = {
  status: "starting", // starting | waiting_qr | connected | reconnecting
  qrText: null,
  qrDataUrl: null,
  connectedUser: null,
  lastError: null,
  startedAt: new Date().toISOString(),
};

// Simple bounded in-memory cache implementing Baileys CacheStore interface
function createSimpleCache(maxSize = 1000) {
  const map = new Map();
  return {
    get(key) {
      return map.get(key);
    },
    set(key, val) {
      map.set(key, val);
      if (map.size > maxSize) {
        const firstKey = map.keys().next().value;
        map.delete(firstKey);
      }
    },
    del(key) {
      map.delete(key);
    },
    flushAll() {
      map.clear();
    },
  };
}

const msgRetryCounterCache = createSimpleCache(1500);

// Store recent sent & received WAMessageContent so Baileys `getMessage` can
// re-encrypt messages on retry receipts (fixes "Waiting for this message")
const messageStore = new Map();
function rememberMessageContent(msg) {
  const id = msg?.key?.id;
  const jid = msg?.key?.remoteJid;
  const content = msg?.message;
  if (!id || !content) return;
  if (jid) messageStore.set(`${jid}:${id}`, content);
  messageStore.set(id, content);
  if (messageStore.size > 1500) {
    const first = messageStore.keys().next().value;
    messageStore.delete(first);
  }
}

// Track message IDs sent by this bot to prevent reply loops (especially in self-chat)
const sentByBotIds = new Set();
function rememberSentId(msg) {
  rememberMessageContent(msg);
  const id = msg?.key?.id;
  if (!id) return;
  sentByBotIds.add(id);
  if (sentByBotIds.size > 500) {
    const first = sentByBotIds.values().next().value;
    sentByBotIds.delete(first);
  }
}

function makeTempPath(ext) {
  const name = `cw_${Date.now()}_${Math.random().toString(36).slice(2, 8)}${ext}`;
  return path.join(os.tmpdir(), name);
}

function safeUnlink(filePath) {
  if (!filePath) return;
  try {
    if (fs.existsSync(filePath)) fs.unlinkSync(filePath);
  } catch (_) {}
}

async function runPythonCli(args) {
  const { stdout } = await execFileAsync(PYTHON_BIN, [CW_CLI_PATH, ...args], {
    cwd: PROJECT_ROOT,
    timeout: 60000,
    maxBuffer: 4 * 1024 * 1024,
  });
  return stdout.trim();
}

async function decodeAudioFile(audioPath) {
  const out = await runPythonCli(["decode", audioPath, "--json"]);
  return JSON.parse(out);
}

async function encodeTextToOggVoiceNote(text, wpm, freqHz) {
  const wavPath = makeTempPath(".wav");
  const oggPath = makeTempPath(".ogg");
  const cliArgs = ["encode", "-o", wavPath, "--wpm", String(wpm), "--freq", String(freqHz), "--", text];
  await runPythonCli(cliArgs);

  const morse = await runPythonCli(["morse", "--encode", "--", text]);

  // Convert WAV -> OGG Opus so WhatsApp renders it as a playable voice note
  try {
    await execFileAsync(
      FFMPEG_BIN,
      ["-y", "-hide_banner", "-loglevel", "error", "-i", wavPath, "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1", oggPath],
      { timeout: 30000 }
    );
    safeUnlink(wavPath);
    return { audioPath: oggPath, mimetype: "audio/ogg; codecs=opus", ptt: true, morse };
  } catch (_) {
    safeUnlink(oggPath);
    return { audioPath: wavPath, mimetype: "audio/wav", ptt: false, morse };
  }
}

function formatDecodeResult(res) {
  if (!res || !res.text || !String(res.text).trim()) {
    return [
      "⚠️ *Tiada isyarat CW/Morse yang jelas dikesan.*",
      `• Durasi: ${Number(res?.duration_sec || 0).toFixed(2)}s`,
      `• Nada dikesan: ${Math.round(Number(res?.tone_hz || 0))} Hz (SNR ${Number(res?.snr_db || 0).toFixed(1)} dB)`,
      "",
      "_Tip: Pastikan bunyi CW jelas dan tidak terlalu pendek._",
    ].join("\n");
  }

  return [
    "📡 *Hasil Nyahkod CW Decoder*",
    `*Teks:* \`${res.text}\``,
    `*Morse:* \`${res.morse}\``,
    `*Kelajuan:* ${Number(res.wpm).toFixed(1)} WPM | *Nada:* ${Math.round(Number(res.tone_hz))} Hz`,
    `*Keyakinan:* ${Math.round(Number(res.confidence) * 100)}% (SNR ${Number(res.snr_db).toFixed(1)} dB, ${Number(res.duration_sec).toFixed(1)}s)`,
  ].join("\n");
}

function parseEncodeArgs(raw) {
  const tokens = raw.trim().split(/\s+/).filter(Boolean);
  let wpm = 20;
  let freq = 700;
  const parts = [];
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if ((t === "--wpm" || t === "-w") && i + 1 < tokens.length) {
      const val = Number(tokens[i + 1]);
      if (!Number.isNaN(val)) {
        wpm = Math.max(5, Math.min(50, val));
        i++;
        continue;
      }
    } else if ((t === "--freq" || t === "-f") && i + 1 < tokens.length) {
      const val = Number(tokens[i + 1]);
      if (!Number.isNaN(val)) {
        freq = Math.max(300, Math.min(2500, val));
        i++;
        continue;
      }
    }
    parts.push(t);
  }
  const text = parts.join(" ").trim();
  return text ? { text, wpm, freq } : null;
}

function extFromMime(mime) {
  const clean = String(mime || "").split(";")[0].trim().toLowerCase();
  const map = {
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
  };
  return map[clean] || ".ogg";
}

function extractMessageText(message) {
  if (!message) return "";
  return (
    message.conversation ||
    message.extendedTextMessage?.text ||
    message.imageMessage?.caption ||
    message.videoMessage?.caption ||
    message.documentMessage?.caption ||
    ""
  ).trim();
}

function extractAudioInfo(message) {
  if (!message) return null;
  if (message.audioMessage) {
    return {
      ext: extFromMime(message.audioMessage.mimetype),
    };
  }
  if (message.documentMessage) {
    const mime = String(message.documentMessage.mimetype || "").toLowerCase();
    const fname = String(message.documentMessage.fileName || "").toLowerCase();
    const audioExts = [".wav", ".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".amr", ".flac", ".webm"];
    const matchedExt = audioExts.find((e) => fname.endsWith(e));
    if (mime.startsWith("audio/") || matchedExt) {
      return { ext: matchedExt || extFromMime(mime) };
    }
  }
  return null;
}

function extractQuotedAudio(content, remoteJid) {
  const ctx = content?.extendedTextMessage?.contextInfo;
  const quotedMsg = ctx?.quotedMessage;
  if (!quotedMsg) return null;
  const unwrapped =
    quotedMsg.ephemeralMessage?.message ||
    quotedMsg.viewOnceMessage?.message ||
    quotedMsg.viewOnceMessageV2?.message ||
    quotedMsg;
  const audioInfo = extractAudioInfo(unwrapped);
  if (!audioInfo) return null;
  return {
    audioInfo,
    downloadableMsg: {
      key: {
        remoteJid,
        id: ctx.stanzaId,
        participant: ctx.participant,
      },
      message: unwrapped,
    },
  };
}

function isSelfChat(remoteJid, sockUser) {
  if (!remoteJid || !sockUser) return false;
  const normRemote = jidNormalizedUser(remoteJid);
  if (sockUser.id && normRemote === jidNormalizedUser(sockUser.id)) return true;
  if (sockUser.lid && normRemote === jidNormalizedUser(sockUser.lid)) return true;
  return false;
}

async function sendText(sock, jid, text, quoted) {
  const sent = await sock.sendMessage(jid, { text }, quoted ? { quoted } : undefined);
  rememberSentId(sent);
  return sent;
}

async function decodeAndReply(sock, remoteJid, downloadableMsg, ext, quoteTarget) {
  const tmpAudio = makeTempPath(ext);
  try {
    const buffer = await downloadMediaMessage(
      downloadableMsg,
      "buffer",
      {},
      { logger: pino({ level: "silent" }), reuploadRequest: sock.updateMediaMessage }
    );
    fs.writeFileSync(tmpAudio, buffer);
    const result = await decodeAudioFile(tmpAudio);
    await sendText(sock, remoteJid, formatDecodeResult(result), quoteTarget);
  } catch (err) {
    console.error("[CW Decode Error]", err);
    await sendText(sock, remoteJid, `❌ *Gagal menyahkod audio:* ${err.message || err}`, quoteTarget);
  } finally {
    safeUnlink(tmpAudio);
  }
}

const EXPLICIT_COMMANDS = new Set([
  "/help",
  "!help",
  "/start",
  "!start",
  "/cwhelp",
  "/decode",
  "!decode",
  "/cwauto",
  "!cwauto",
  "/encode",
  "!encode",
  "/cw",
  "!cw",
  "/morse",
  "!morse",
  "/tomorse",
  "!tomorse",
]);

async function handleIncomingMessage(sock, msg) {
  if (!msg || !msg.message) return;
  const remoteJid = msg.key?.remoteJid;
  if (!remoteJid || remoteJid === "status@broadcast") return;
  if (sentByBotIds.has(msg.key?.id)) return;

  const isGroup = remoteJid.endsWith("@g.us");
  const selfChat = isSelfChat(remoteJid, sock.user);

  // Unwrap ephemeral / viewOnce wrappers if present
  const content =
    msg.message.ephemeralMessage?.message ||
    msg.message.viewOnceMessage?.message ||
    msg.message.viewOnceMessageV2?.message ||
    msg.message;

  const text = extractMessageText(content);
  const firstWord = text ? text.split(/\s+/)[0].toLowerCase() : "";
  const rest = text ? text.slice(firstWord.length).trim() : "";

  // Safeguard: never re-process our own formatted bot replies
  if (msg.key?.fromMe && BOT_REPLY_PREFIXES.some((p) => text.startsWith(p))) {
    return;
  }

  // For own outgoing messages (`fromMe`):
  // - In self-chat: allow everything (audio + commands + raw Morse)
  // - In groups/DMs: allow explicit commands (/decode, /encode, /help, /cwauto, etc.) and group audio when auto-decode is ON
  const isExplicitCmd = EXPLICIT_COMMANDS.has(firstWord);
  const audioInfo = extractAudioInfo(content);
  if (msg.key?.fromMe && !selfChat) {
    const allowOwnGroupAudio = Boolean(isGroup && audioInfo && isGroupAutoEnabled(remoteJid));
    if (!isExplicitCmd && !allowOwnGroupAudio) {
      return;
    }
  }

  // 1. Direct audio / voice note decoding
  if (audioInfo) {
    if (isGroup && !isGroupAutoEnabled(remoteJid) && !text.toLowerCase().includes("decode")) {
      return;
    }
    await decodeAndReply(sock, remoteJid, msg, audioInfo.ext, msg);
    return;
  }

  if (!text) return;

  // 2. Reply `/decode` to a quoted voice note / audio message (works in groups & DMs)
  if (["/decode", "!decode"].includes(firstWord)) {
    const quoted = extractQuotedAudio(content, remoteJid);
    if (!quoted) {
      await sendText(
        sock,
        remoteJid,
        "⚠️ Sila *Reply* mesej voice note atau fail audio dengan `/decode`.",
        msg
      );
      return;
    }
    await decodeAndReply(sock, remoteJid, quoted.downloadableMsg, quoted.audioInfo.ext, msg);
    return;
  }

  // 3. Toggle auto-decode for the current group (`/cwauto on` | `/cwauto off`)
  if (["/cwauto", "!cwauto"].includes(firstWord)) {
    if (!isGroup) {
      await sendText(sock, remoteJid, "✅ Dalam chat peribadi, auto-decode sentiasa aktif.", msg);
      return;
    }
    const mode = rest.toLowerCase();
    if (mode === "on" || mode === "hidup") {
      groupAutoDecode.set(remoteJid, true);
      await sendText(
        sock,
        remoteJid,
        "✅ *Auto-decode Group AKTIF.* Semua voice note / audio yang dihantar dalam group ini akan dinyahkod automatik.",
        msg
      );
    } else if (mode === "off" || mode === "mati") {
      groupAutoDecode.set(remoteJid, false);
      await sendText(
        sock,
        remoteJid,
        "✅ *Auto-decode Group DIMATIKAN.* Untuk nyahkod audio dalam group, cuma *Reply* audio tersebut dengan `/decode`.",
        msg
      );
    } else {
      const curr = isGroupAutoEnabled(remoteJid) ? "AKTIF (ON)" : "MATI (OFF)";
      await sendText(
        sock,
        remoteJid,
        `📡 Status auto-decode group ini: *${curr}*\nGuna \`/cwauto on\` atau \`/cwauto off\`.`,
        msg
      );
    }
    return;
  }

  // 4. Other text commands (/help, /encode, /morse, /tomorse, raw Morse)
  if (["/help", "!help", "/start", "!start", "/cwhelp"].includes(firstWord)) {
    await sendText(sock, remoteJid, HELP_TEXT, msg);
    return;
  }

  if (["/encode", "!encode", "/cw", "!cw"].includes(firstWord)) {
    const parsed = parseEncodeArgs(rest);
    if (!parsed) {
      await sendText(
        sock,
        remoteJid,
        "⚠️ Sila masukkan teks selepas `/encode`.\nContoh: `/encode HELLO WORLD` atau `/encode --wpm 18 CQ CQ`",
        msg
      );
      return;
    }
    let outFile = null;
    try {
      const encoded = await encodeTextToOggVoiceNote(parsed.text, parsed.wpm, parsed.freq);
      outFile = encoded.audioPath;
      const audioBuf = fs.readFileSync(outFile);
      const sentAudio = await sock.sendMessage(
        remoteJid,
        { audio: audioBuf, mimetype: encoded.mimetype, ptt: encoded.ptt },
        { quoted: msg }
      );
      rememberSentId(sentAudio);
      const caption = [
        "🔊 *Audio CW Dijana*",
        `*Teks:* \`${parsed.text.toUpperCase()}\``,
        `*Morse:* \`${encoded.morse}\``,
        `_${parsed.wpm} WPM @ ${parsed.freq} Hz_`,
      ].join("\n");
      await sendText(sock, remoteJid, caption);
    } catch (err) {
      console.error("[CW Encode Error]", err);
      await sendText(sock, remoteJid, `❌ *Gagal menjana audio CW:* ${err.message || err}`, msg);
    } finally {
      safeUnlink(outFile);
    }
    return;
  }

  if (["/morse", "!morse"].includes(firstWord)) {
    if (!rest) {
      await sendText(sock, remoteJid, "⚠️ Contoh: `/morse .... . .-.. .-.. ---`", msg);
      return;
    }
    try {
      const decoded = await runPythonCli(["morse", "--", rest]);
      await sendText(sock, remoteJid, `🔤 *Teks:* \`${decoded}\`\n*Morse:* \`${rest}\``, msg);
    } catch (err) {
      await sendText(sock, remoteJid, `❌ *Ralat:* ${err.message || err}`, msg);
    }
    return;
  }

  if (["/tomorse", "!tomorse"].includes(firstWord)) {
    if (!rest) {
      await sendText(sock, remoteJid, "⚠️ Contoh: `/tomorse HELLO`", msg);
      return;
    }
    try {
      const morse = await runPythonCli(["morse", "--encode", "--", rest]);
      await sendText(sock, remoteJid, `•− *Morse:* \`${morse}\`\n*Teks:* \`${rest.toUpperCase()}\``, msg);
    } catch (err) {
      await sendText(sock, remoteJid, `❌ *Ralat:* ${err.message || err}`, msg);
    }
    return;
  }

  // If user types pure Morse symbols in private/self chat, decode directly!
  if (!isGroup) {
    const cleaned = text.replace(/[\s/]/g, "");
    if (cleaned.length > 0 && /^[.\-·•∙−‒–—_]+$/.test(cleaned)) {
      try {
        const decoded = await runPythonCli(["morse", "--", text]);
        await sendText(sock, remoteJid, `🔤 *Teks:* \`${decoded}\`\n*Morse:* \`${text}\``, msg);
      } catch (_) {}
    }
  }
}

let activeSock = null;
let reconnectTimer = null;

function scheduleReconnect(delayMs = 3000) {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    startWhatsApp().catch((err) => {
      state.lastError = String(err);
      console.error("Ralat menyambung semula WhatsApp:", err);
      scheduleReconnect(5000);
    });
  }, delayMs);
}

async function startWhatsApp() {
  if (activeSock) {
    try {
      activeSock.ev.removeAllListeners();
      activeSock.end(undefined);
    } catch (_) {}
    activeSock = null;
  }

  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const { state: authState, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();
  const waLogger = pino({ level: "warn" });

  const sock = makeWASocket({
    version,
    auth: {
      creds: authState.creds,
      keys: makeCacheableSignalKeyStore(authState.keys, waLogger),
    },
    logger: waLogger,
    browser: Browsers.ubuntu("Chrome"),
    printQRInTerminal: false,
    syncFullHistory: false,
    markOnlineOnConnect: false,
    keepAliveIntervalMs: 30000,
    retryRequestDelayMs: 500,
    msgRetryCounterCache,
    getMessage: async (key) => {
      if (!key?.id) return undefined;
      return (
        (key.remoteJid ? messageStore.get(`${key.remoteJid}:${key.id}`) : undefined) ||
        messageStore.get(key.id) ||
        undefined
      );
    },
  });

  activeSock = sock;

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    if (activeSock !== sock) return;
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      state.status = "waiting_qr";
      state.qrText = qr;
      state.connectedUser = null;
      state.lastError = null;
      try {
        state.qrDataUrl = await QRCode.toDataURL(qr, { width: 320, margin: 2 });
        const asciiQr = await QRCode.toString(qr, { type: "terminal", small: true });
        console.log("\n=== IMBAS KOD QR WHATSAPP (atau buka /qr di pelayar) ===\n" + asciiQr);
      } catch (err) {
        console.error("Gagal jana imej QR:", err);
      }
    }

    if (connection === "open") {
      state.status = "connected";
      state.qrText = null;
      state.qrDataUrl = null;
      state.lastError = null;
      state.connectedUser = sock.user?.id || "WhatsApp";
      console.log(`✅ CW Decoder WhatsApp berjaya disambung sebagai: ${state.connectedUser}`);
    }

    if (connection === "close") {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      const badSession =
        statusCode === DisconnectReason.badSession ||
        statusCode === DisconnectReason.connectionReplaced;
      state.connectedUser = null;
      state.lastError = lastDisconnect?.error?.message || `Disconnect code ${statusCode}`;
      console.log(`⚠️ Sambungan WhatsApp terputus (kod=${statusCode}). LoggedOut=${loggedOut}`);

      if (loggedOut || badSession) {
        // Clear corrupted/replaced session so a fresh QR code is generated cleanly
        state.status = "reconnecting";
        try {
          fs.rmSync(AUTH_DIR, { recursive: true, force: true });
        } catch (_) {}
        scheduleReconnect(3000);
      } else {
        state.status = "reconnecting";
        scheduleReconnect(3000);
      }
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const msg of messages) {
      rememberMessageContent(msg);
      try {
        await handleIncomingMessage(sock, msg);
      } catch (err) {
        console.error("[Message Handler Error]", err);
      }
    }
  });
}

function renderQrHtml() {
  const autoRefresh = state.status !== "connected" ? '<meta http-equiv="refresh" content="5">' : "";
  let bodyContent = "";

  if (state.status === "connected") {
    bodyContent = `
      <div class="badge ok">✅ DISAMBUNG</div>
      <p>Bot <b>CW Decoder WhatsApp</b> sedang aktif!</p>
      <p class="small">Akaun: <code>${state.connectedUser || "WhatsApp"}</code></p>
      <p>Cuba hantar <b>voice note bunyi CW</b> atau taip <code>/help</code> di WhatsApp anda (boleh juga hantar ke chat diri sendiri / <i>Message Yourself</i>).</p>
      <form method="POST" action="/reset" style="margin-top:16px">
        <button type="submit" style="background:#7f1d1d;color:#fecaca;border:none;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:0.85rem">Reset Sesi &amp; Jana QR Baharu</button>
      </form>
    `;
  } else if (state.qrDataUrl) {
    bodyContent = `
      <div class="badge warn">📱 IMBAS KOD QR</div>
      <p>Buka <b>WhatsApp</b> di telefon → <b>Peranti Dipautkan (Linked Devices)</b> → <b>Pautkan Peranti</b> → imbas QR di bawah:</p>
      <img src="${state.qrDataUrl}" alt="WhatsApp QR Code" class="qr" />
      <p class="small">Halaman ini akan muat semula automatik setiap 5 saat.</p>
    `;
  } else {
    bodyContent = `
      <div class="badge info">⏳ MENYEDIAKAN KOD QR...</div>
      <p>Status semasa: <code>${state.status}</code></p>
      ${state.lastError ? `<p class="small">Info: ${state.lastError}</p>` : ""}
      <p class="small">Halaman ini akan muat semula automatik setiap 5 saat.</p>
    `;
  }

  return `<!DOCTYPE html>
<html lang="ms">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  ${autoRefresh}
  <title>CW Decoder — WhatsApp QR</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; background: #0f172a; color: #e2e8f0; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 16px; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 28px; max-width: 420px; width: 100%; text-align: center; box-shadow: 0 20px 40px rgba(0,0,0,0.4); }
    h1 { margin: 0 0 12px; font-size: 1.4rem; color: #38bdf8; }
    .badge { display: inline-block; padding: 6px 14px; border-radius: 999px; font-weight: 700; font-size: 0.85rem; margin-bottom: 14px; }
    .ok { background: #14532d; color: #86efac; }
    .warn { background: #713f12; color: #fde047; }
    .info { background: #1e3a8a; color: #93c5fd; }
    .qr { background: #fff; padding: 12px; border-radius: 12px; margin: 12px auto; max-width: 280px; width: 100%; display: block; }
    code { background: #0f172a; padding: 2px 6px; border-radius: 6px; color: #38bdf8; }
    .small { font-size: 0.85rem; color: #94a3b8; }
  </style>
</head>
<body>
  <div class="card">
    <h1>📡 CW Decoder WhatsApp</h1>
    ${bodyContent}
  </div>
</body>
</html>`;
}

// Keep-alive hub state for pinging other services when /health is hit
const keepAliveState = {
  inFlight: false,
  lastRunAt: null,
  lastRunMs: 0,
  results: Object.fromEntries(
    KEEPALIVE_URLS.map((targetUrl) => [
      targetUrl,
      { ok: null, status: "pending", httpStatus: null, latencyMs: null, checkedAt: null, error: null },
    ])
  ),
};

async function pingKeepAliveTarget(targetUrl) {
  const started = Date.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), KEEPALIVE_TIMEOUT_MS);
  try {
    const resp = await fetch(targetUrl, {
      method: "GET",
      redirect: "follow",
      signal: controller.signal,
      headers: { "User-Agent": "CW-Decoder-KeepAlive/1.0" },
    });
    if (resp.body && typeof resp.body.cancel === "function") {
      resp.body.cancel().catch(() => {});
    }
    const latencyMs = Date.now() - started;
    keepAliveState.results[targetUrl] = {
      ok: resp.ok,
      status: resp.ok ? "up" : "error",
      httpStatus: resp.status,
      latencyMs,
      checkedAt: new Date().toISOString(),
      error: resp.ok ? null : `HTTP ${resp.status}`,
    };
  } catch (err) {
    const latencyMs = Date.now() - started;
    const isTimeout = err && err.name === "AbortError";
    keepAliveState.results[targetUrl] = {
      ok: false,
      status: "down",
      httpStatus: null,
      latencyMs,
      checkedAt: new Date().toISOString(),
      error: isTimeout ? `Timeout (${KEEPALIVE_TIMEOUT_MS}ms)` : String(err?.message || err),
    };
  } finally {
    clearTimeout(timer);
  }
}

function triggerKeepAlivePings(force = false) {
  if (KEEPALIVE_URLS.length === 0 || keepAliveState.inFlight) return false;
  const now = Date.now();
  if (!force && keepAliveState.lastRunMs > 0 && now - keepAliveState.lastRunMs < KEEPALIVE_MIN_INTERVAL_MS) {
    return false;
  }

  keepAliveState.inFlight = true;
  keepAliveState.lastRunMs = now;

  Promise.allSettled(KEEPALIVE_URLS.map((u) => pingKeepAliveTarget(u)))
    .then(() => {
      const upCount = Object.values(keepAliveState.results).filter((r) => r.ok).length;
      console.log(`[KeepAlive] Ping selesai: ${upCount}/${KEEPALIVE_URLS.length} servis aktif.`);
    })
    .finally(() => {
      keepAliveState.inFlight = false;
      keepAliveState.lastRunAt = new Date().toISOString();
    });

  return true;
}

// Start HTTP server for Render health check + browser QR code scanner
const server = http.createServer((req, res) => {
  let parsedUrl;
  try {
    parsedUrl = new URL(req.url || "/", "http://localhost");
  } catch (_) {
    res.writeHead(400, { "Content-Type": "text/plain" });
    res.end("Bad Request");
    return;
  }
  const url = parsedUrl.pathname;
  if (url === "/health" || url === "/health/") {
    const forcePing = ["1", "true", "yes"].includes(String(parsedUrl.searchParams.get("force") || "").toLowerCase());
    const triggered = triggerKeepAlivePings(forcePing);
    const headers = {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    };
    if (req.method === "HEAD") {
      res.writeHead(200, headers);
      res.end();
      return;
    }
    res.writeHead(200, headers);
    res.end(
      JSON.stringify({
        status: "ok",
        service: "CW Decoder WhatsApp QR Bot",
        whatsapp_state: state.status,
        connected: state.status === "connected",
        started_at: state.startedAt,
        uptime_sec: Math.floor(process.uptime()),
        keepalive: {
          configured: KEEPALIVE_URLS.length,
          triggered,
          in_flight: keepAliveState.inFlight,
          last_run_at: keepAliveState.lastRunAt,
          targets: keepAliveState.results,
        },
      })
    );
    return;
  }

  if (url === "/reset" && req.method === "POST") {
    state.status = "reconnecting";
    state.qrText = null;
    state.qrDataUrl = null;
    state.connectedUser = null;
    if (activeSock) {
      try {
        activeSock.ev.removeAllListeners();
        activeSock.end(undefined);
      } catch (_) {}
      activeSock = null;
    }
    try {
      fs.rmSync(AUTH_DIR, { recursive: true, force: true });
    } catch (_) {}
    messageStore.clear();
    msgRetryCounterCache.flushAll();
    scheduleReconnect(1000);
    res.writeHead(303, { Location: "/qr" });
    res.end();
    return;
  }

  if (url === "/" || url === "/qr") {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    res.end(renderQrHtml());
    return;
  }

  res.writeHead(404, { "Content-Type": "text/plain" });
  res.end("Not Found");
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`🌐 Pelayan CW Decoder WhatsApp QR berjalan di http://0.0.0.0:${PORT}`);
  console.log(`📱 Buka http://localhost:${PORT}/qr untuk imbas kod QR WhatsApp.`);
  if (KEEPALIVE_URLS.length > 0) {
    console.log(`💓 Keep-Alive Hub aktif untuk ${KEEPALIVE_URLS.length} servis sasaran apabila /health dipanggil.`);
  }
  startWhatsApp().catch((err) => {
    state.lastError = String(err);
    console.error("Ralat memulakan WhatsApp:", err);
  });
});
