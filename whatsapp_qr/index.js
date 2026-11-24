/**
 * CW Decoder - Bot WhatsApp Tanpa Token (Imbas Kod QR via Baileys)
 * Menyokong sehingga MAX_SESSIONS (default 10) pengguna serentak dengan auto-logout 1 jam setiap sesi.
 *
 * Cara guna:
 * - Jalankan di Render (Docker) atau secara lokal (`node whatsapp_qr/index.js`)
 * - Buka `/qr` di pelayar dan imbas kod QR guna WhatsApp -> Peranti Dipautkan (Linked Devices)
 * - Hantar voice note / audio CW, taip `/encode HELLO`, atau Reply teks dengan `/encode`!
 */

"use strict";

const { execFile } = require("child_process");
const crypto = require("crypto");
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

const rawMaxSessions = Number(process.env.MAX_SESSIONS);
const MAX_SESSIONS = Math.max(
  1,
  Math.min(50, Number.isFinite(rawMaxSessions) && rawMaxSessions > 0 ? Math.floor(rawMaxSessions) : 10)
);
const rawSessionTtlMin = Number(process.env.SESSION_TTL_MINUTES);
const SESSION_TTL_MINUTES = Math.max(
  1,
  Math.min(1440, Number.isFinite(rawSessionTtlMin) && rawSessionTtlMin > 0 ? rawSessionTtlMin : 60)
);
const SESSION_TTL_MS = SESSION_TTL_MINUTES * 60 * 1000;
const MASK_PHONE = String(process.env.MASK_PHONE || "false").toLowerCase() === "true";

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

const STARTED_AT = new Date().toISOString();

const HELP_TEXT = [
  "📡 *CW Decoder — Bot WhatsApp (Tanpa Token)*",
  "Hantar *voice note* atau fail audio CW (WAV/OGG/MP3/M4A) dan saya akan nyahkod bunyi Morse kepada perkataan secara automatik!",
  "",
  "*Arahan Tersedia:*",
  "• Hantar voice note / audio → Nyahkod CW kepada teks",
  "• Reply `/decode` pada mana-mana voice note/audio → Nyahkod audio tersebut (sesuai untuk Group!)",
  "• `/cwauto on` / `/cwauto off` → Hidup/matikan auto-decode semua audio dalam Group ini",
  "• `/encode <teks>` atau Reply `/encode` pada teks → Jana voice note bunyi CW",
  "  _(Contoh: `/encode --wpm 18 --freq 700 HELLO WORLD` atau Reply teks dengan `/encode`)_",
  "• `/morse <kod>` → Nyahkod kod Morse teks",
  "  _(Contoh: `/morse .... . .-.. .-.. ---`)_",
  "• `/tomorse <teks>` → Tukar teks kepada simbol Morse",
  `• \`/masa\` → Semak baki masa sesi anda (had ${SESSION_TTL_MINUTES} minit, auto-logout selepas tamat)`,
  "• `/help` → Papar panduan ini",
].join("\n");

const BOT_REPLY_PREFIXES = ["📡", "⚠️", "❌", "🔤", "•−", "🔊", "✅", "⏱️"];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function extractPhoneFromJid(jid) {
  if (!jid) return "";
  const userPart = String(jid).split("@")[0].split(":")[0];
  return userPart.replace(/[^0-9A-Za-z_.-]/g, "");
}

function formatPhoneDisplay(phone, mask = MASK_PHONE) {
  if (!phone) return "Tidak diketahui";
  const digits = String(phone).replace(/\D/g, "");
  if (mask && digits.length >= 7) {
    return `+${digits.slice(0, 4)}••••${digits.slice(-3)}`;
  }
  return digits.length >= 7 ? `+${digits}` : phone;
}

function formatRemainingMs(ms) {
  const clamped = Math.max(0, Math.floor(ms / 1000));
  const hrs = Math.floor(clamped / 3600);
  const mins = Math.floor((clamped % 3600) / 60);
  const secs = clamped % 60;
  if (hrs > 0) {
    return `${hrs}j ${String(mins).padStart(2, "0")}m ${String(secs).padStart(2, "0")}s`;
  }
  return `${mins}m ${String(secs).padStart(2, "0")}s`;
}

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

function createSessionContext(id, options = {}) {
  return {
    id,
    authDir: path.join(AUTH_DIR, id),
    isPairing: Boolean(options.isPairing),
    status: "starting", // starting | waiting_qr | connected | reconnecting | logging_out
    sock: null,
    qrText: null,
    qrDataUrl: null,
    rawJid: null,
    userJid: options.userJid || null,
    phone: options.phone || null,
    name: options.name || "",
    createdAt: Date.now(),
    connectedAt: options.connectedAt || null,
    expiresAt: options.expiresAt || null,
    lastError: null,
    stats: {
      decoded: Number(options.decoded || 0),
      encoded: Number(options.encoded || 0),
    },
    msgRetryCounterCache: createSimpleCache(1000),
    messageStore: new Map(),
    sentByBotIds: new Set(),
    groupAutoDecode: new Map(),
    reconnectTimer: null,
    expiryTimer: null,
  };
}

const defaultSessionContext = createSessionContext("default");

function isGroupAutoEnabled(session, jid) {
  const ctx = session || defaultSessionContext;
  if (ctx.groupAutoDecode.has(jid)) return ctx.groupAutoDecode.get(jid);
  return ALLOW_GROUP_AUDIO;
}

function rememberMessageContent(session, msg) {
  const ctx = session || defaultSessionContext;
  const id = msg?.key?.id;
  const jid = msg?.key?.remoteJid;
  const content = msg?.message;
  if (!id || !content) return;
  if (jid) ctx.messageStore.set(`${jid}:${id}`, content);
  ctx.messageStore.set(id, content);
  if (ctx.messageStore.size > 1000) {
    const first = ctx.messageStore.keys().next().value;
    ctx.messageStore.delete(first);
  }
}

function rememberSentId(session, msg) {
  const ctx = session || defaultSessionContext;
  rememberMessageContent(ctx, msg);
  const id = msg?.key?.id;
  if (!id) return;
  ctx.sentByBotIds.add(id);
  if (ctx.sentByBotIds.size > 500) {
    const first = ctx.sentByBotIds.values().next().value;
    ctx.sentByBotIds.delete(first);
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
  const morse = await runPythonCli(["morse", "--encode", "--", text]);
  if (!morse) {
    return { audioPath: null, mimetype: null, ptt: false, morse: "" };
  }

  const wavPath = makeTempPath(".wav");
  const oggPath = makeTempPath(".ogg");
  const cliArgs = ["encode", "-o", wavPath, "--wpm", String(wpm), "--freq", String(freqHz), "--", text];
  await runPythonCli(cliArgs);

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

function parseEncodeArgs(raw, fallbackText = "") {
  const tokens = String(raw || "").trim().split(/\s+/).filter(Boolean);
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
  const inlineText = parts.join(" ").trim();
  const text = inlineText || String(fallbackText || "").replace(/\s+/g, " ").trim();
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

function unwrapMessage(message) {
  if (!message) return null;
  let current = message;
  for (let depth = 0; depth < 5; depth++) {
    const next =
      current.ephemeralMessage?.message ||
      current.viewOnceMessage?.message ||
      current.viewOnceMessageV2?.message ||
      current.viewOnceMessageV2Extension?.message ||
      current.documentWithCaptionMessage?.message ||
      null;
    if (!next) break;
    current = next;
  }
  return current;
}

function getContextInfo(content) {
  return (
    content?.extendedTextMessage?.contextInfo ||
    content?.imageMessage?.contextInfo ||
    content?.videoMessage?.contextInfo ||
    content?.documentMessage?.contextInfo ||
    null
  );
}

function extractQuotedText(content, remoteJid, session) {
  const ctx = getContextInfo(content);
  if (!ctx) return "";
  const store = (session || defaultSessionContext).messageStore;
  const quotedMsg =
    ctx.quotedMessage ||
    (ctx.stanzaId
      ? (remoteJid ? store.get(`${remoteJid}:${ctx.stanzaId}`) : undefined) || store.get(ctx.stanzaId)
      : null);
  if (!quotedMsg) return "";
  const unwrapped = unwrapMessage(quotedMsg);
  return extractMessageText(unwrapped);
}

function extractQuotedAudio(content, remoteJid) {
  const ctx = getContextInfo(content);
  const quotedMsg = ctx?.quotedMessage;
  if (!quotedMsg) return null;
  const unwrapped = unwrapMessage(quotedMsg);
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

async function sendText(sock, jid, text, quoted, session) {
  const sent = await sock.sendMessage(jid, { text }, quoted ? { quoted } : undefined);
  rememberSentId(session, sent);
  return sent;
}

async function decodeAndReply(sock, remoteJid, downloadableMsg, ext, quoteTarget, session) {
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
    if (session) {
      session.stats.decoded += 1;
      if (session.connectedAt !== null) saveSessionMeta(session);
    }
    await sendText(sock, remoteJid, formatDecodeResult(result), quoteTarget, session);
  } catch (err) {
    console.error("[CW Decode Error]", err);
    await sendText(sock, remoteJid, `❌ *Gagal menyahkod audio:* ${err.message || err}`, quoteTarget, session);
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
  "/masa",
  "!masa",
  "/sesi",
  "!sesi",
]);

async function handleIncomingMessage(sock, msg, session = defaultSessionContext) {
  if (!msg || !msg.message) return;
  const remoteJid = msg.key?.remoteJid;
  if (!remoteJid || remoteJid === "status@broadcast") return;
  if (session.sentByBotIds.has(msg.key?.id)) return;

  const isGroup = remoteJid.endsWith("@g.us");
  const selfChat = isSelfChat(remoteJid, sock.user);

  // Unwrap ephemeral / viewOnce wrappers if present
  const content = unwrapMessage(msg.message) || msg.message;

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
    const allowOwnGroupAudio = Boolean(isGroup && audioInfo && isGroupAutoEnabled(session, remoteJid));
    if (!isExplicitCmd && !allowOwnGroupAudio) {
      return;
    }
  }

  // 1. Direct audio / voice note decoding
  if (audioInfo) {
    if (isGroup && !isGroupAutoEnabled(session, remoteJid) && !text.toLowerCase().includes("decode")) {
      return;
    }
    await decodeAndReply(sock, remoteJid, msg, audioInfo.ext, msg, session);
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
        msg,
        session
      );
      return;
    }
    await decodeAndReply(sock, remoteJid, quoted.downloadableMsg, quoted.audioInfo.ext, msg, session);
    return;
  }

  // 3. Toggle auto-decode for the current group (`/cwauto on` | `/cwauto off`)
  if (["/cwauto", "!cwauto"].includes(firstWord)) {
    if (!isGroup) {
      await sendText(sock, remoteJid, "✅ Dalam chat peribadi, auto-decode sentiasa aktif.", msg, session);
      return;
    }
    const mode = rest.toLowerCase();
    if (mode === "on" || mode === "hidup") {
      session.groupAutoDecode.set(remoteJid, true);
      await sendText(
        sock,
        remoteJid,
        "✅ *Auto-decode Group AKTIF.* Semua voice note / audio yang dihantar dalam group ini akan dinyahkod automatik.",
        msg,
        session
      );
    } else if (mode === "off" || mode === "mati") {
      session.groupAutoDecode.set(remoteJid, false);
      await sendText(
        sock,
        remoteJid,
        "✅ *Auto-decode Group DIMATIKAN.* Untuk nyahkod audio dalam group, cuma *Reply* audio tersebut dengan `/decode`.",
        msg,
        session
      );
    } else {
      const curr = isGroupAutoEnabled(session, remoteJid) ? "AKTIF (ON)" : "MATI (OFF)";
      await sendText(
        sock,
        remoteJid,
        `📡 Status auto-decode group ini: *${curr}*\nGuna \`/cwauto on\` atau \`/cwauto off\`.`,
        msg,
        session
      );
    }
    return;
  }

  // 4. Session remaining time check (`/masa` | `/sesi`)
  if (["/masa", "!masa", "/sesi", "!sesi"].includes(firstWord)) {
    if (session.expiresAt) {
      const remaining = Math.max(0, session.expiresAt - Date.now());
      await sendText(
        sock,
        remoteJid,
        `⏱️ *Status Sesi CW Decoder*\n• Baki masa: *${formatRemainingMs(remaining)}*\n• Had sesi: ${SESSION_TTL_MINUTES} minit (auto-logout selepas tamat)\n• Statistik: ${session.stats.decoded} decode | ${session.stats.encoded} encode`,
        msg,
        session
      );
    } else {
      await sendText(
        sock,
        remoteJid,
        `⏱️ Had sesi CW Decoder ialah *${SESSION_TTL_MINUTES} minit* setiap sambungan.`,
        msg,
        session
      );
    }
    return;
  }

  // 5. Other text commands (/help, /encode, /morse, /tomorse, raw Morse)
  if (["/help", "!help", "/start", "!start", "/cwhelp"].includes(firstWord)) {
    await sendText(sock, remoteJid, HELP_TEXT, msg, session);
    return;
  }

  if (["/encode", "!encode", "/cw", "!cw"].includes(firstWord)) {
    const quotedText = extractQuotedText(content, remoteJid, session);
    const parsed = parseEncodeArgs(rest, quotedText);
    if (!parsed) {
      await sendText(
        sock,
        remoteJid,
        "⚠️ Sila masukkan teks selepas `/encode` atau *Reply* mesej teks dengan `/encode`.\nContoh: `/encode HELLO WORLD`, `/encode --wpm 18 CQ CQ`, atau *Reply* teks dengan `/encode`.",
        msg,
        session
      );
      return;
    }
    let outFile = null;
    try {
      const encoded = await encodeTextToOggVoiceNote(parsed.text, parsed.wpm, parsed.freq);
      if (!encoded.morse || !encoded.audioPath) {
        await sendText(
          sock,
          remoteJid,
          "⚠️ Tiada aksara yang boleh ditukar kepada kod Morse dalam teks tersebut.",
          msg,
          session
        );
        return;
      }
      outFile = encoded.audioPath;
      const audioBuf = fs.readFileSync(outFile);
      const sentAudio = await sock.sendMessage(
        remoteJid,
        { audio: audioBuf, mimetype: encoded.mimetype, ptt: encoded.ptt },
        { quoted: msg }
      );
      rememberSentId(session, sentAudio);
      session.stats.encoded += 1;
      if (session.connectedAt !== null) saveSessionMeta(session);
      const safeText = parsed.text.toUpperCase().replace(/`/g, "'");
      const caption = [
        "🔊 *Audio CW Dijana*",
        `*Teks:* \`${safeText}\``,
        `*Morse:* \`${encoded.morse}\``,
        `_${parsed.wpm} WPM @ ${parsed.freq} Hz_`,
      ].join("\n");
      await sendText(sock, remoteJid, caption, undefined, session);
    } catch (err) {
      console.error("[CW Encode Error]", err);
      await sendText(sock, remoteJid, `❌ *Gagal menjana audio CW:* ${err.message || err}`, msg, session);
    } finally {
      safeUnlink(outFile);
    }
    return;
  }

  if (["/morse", "!morse"].includes(firstWord)) {
    if (!rest) {
      await sendText(sock, remoteJid, "⚠️ Contoh: `/morse .... . .-.. .-.. ---`", msg, session);
      return;
    }
    try {
      const decoded = await runPythonCli(["morse", "--", rest]);
      await sendText(sock, remoteJid, `🔤 *Teks:* \`${decoded}\`\n*Morse:* \`${rest}\``, msg, session);
    } catch (err) {
      await sendText(sock, remoteJid, `❌ *Ralat:* ${err.message || err}`, msg, session);
    }
    return;
  }

  if (["/tomorse", "!tomorse"].includes(firstWord)) {
    if (!rest) {
      await sendText(sock, remoteJid, "⚠️ Contoh: `/tomorse HELLO`", msg, session);
      return;
    }
    try {
      const morse = await runPythonCli(["morse", "--encode", "--", rest]);
      await sendText(sock, remoteJid, `•− *Morse:* \`${morse}\`\n*Teks:* \`${rest.toUpperCase()}\``, msg, session);
    } catch (err) {
      await sendText(sock, remoteJid, `❌ *Ralat:* ${err.message || err}`, msg, session);
    }
    return;
  }

  // If user types pure Morse symbols in private/self chat, decode directly!
  if (!isGroup) {
    const cleaned = text.replace(/[\s/]/g, "");
    if (cleaned.length > 0 && /^[.\-·•∙−‒–—_]+$/.test(cleaned)) {
      try {
        const decoded = await runPythonCli(["morse", "--", text]);
        await sendText(sock, remoteJid, `🔤 *Teks:* \`${decoded}\`\n*Morse:* \`${text}\``, msg, session);
      } catch (_) {}
    }
  }
}

// ============================================================================
// Multi-Session Manager (max MAX_SESSIONS concurrent users, 1h auto-logout)
// ============================================================================

const sessions = new Map(); // sessionId -> SessionContext
let pairingSessionId = null;
let cachedBaileysVersion = null;
let cachedBaileysVersionAt = 0;
let ensurePairingTimer = null;

async function getBaileysVersionCached() {
  const now = Date.now();
  if (cachedBaileysVersion && now - cachedBaileysVersionAt < 6 * 3600 * 1000) {
    return cachedBaileysVersion;
  }
  const { version } = await fetchLatestBaileysVersion();
  cachedBaileysVersion = version;
  cachedBaileysVersionAt = now;
  return version;
}

function generateSessionId() {
  return `session_${Date.now().toString(36)}_${crypto.randomBytes(3).toString("hex")}`;
}

function getActiveSessions() {
  return Array.from(sessions.values())
    .filter((s) => s.connectedAt !== null && s.status !== "logging_out")
    .sort((a, b) => (a.connectedAt || 0) - (b.connectedAt || 0));
}

function getPairingSession() {
  if (!pairingSessionId) return null;
  const s = sessions.get(pairingSessionId);
  if (!s || !s.isPairing || s.status === "logging_out") {
    pairingSessionId = null;
    return null;
  }
  return s;
}

function getSessionMetaPath(authDir) {
  return path.join(authDir, "session_meta.json");
}

function saveSessionMeta(session) {
  try {
    fs.mkdirSync(session.authDir, { recursive: true });
    const meta = {
      id: session.id,
      userJid: session.userJid,
      phone: session.phone,
      name: session.name,
      connectedAt: session.connectedAt,
      expiresAt: session.expiresAt,
      decoded: session.stats.decoded,
      encoded: session.stats.encoded,
    };
    fs.writeFileSync(getSessionMetaPath(session.authDir), JSON.stringify(meta, null, 2), "utf8");
  } catch (err) {
    console.error(`[${session.id}] Gagal simpan metadata sesi:`, err.message || err);
  }
}

function loadSessionMeta(authDir) {
  try {
    const p = getSessionMetaPath(authDir);
    if (!fs.existsSync(p)) return null;
    return JSON.parse(fs.readFileSync(p, "utf8"));
  } catch (_) {
    return null;
  }
}

function clearSessionTimers(session) {
  if (session.reconnectTimer) {
    clearTimeout(session.reconnectTimer);
    session.reconnectTimer = null;
  }
  if (session.expiryTimer) {
    clearTimeout(session.expiryTimer);
    session.expiryTimer = null;
  }
}

function scheduleSessionExpiry(session) {
  if (session.expiryTimer) {
    clearTimeout(session.expiryTimer);
    session.expiryTimer = null;
  }
  if (!session.expiresAt) return;
  const remaining = session.expiresAt - Date.now();
  if (remaining <= 0) {
    logoutSession(session.id, "expired").catch((err) => {
      console.error(`[${session.id}] Ralat auto-logout:`, err);
    });
    return;
  }
  session.expiryTimer = setTimeout(() => {
    session.expiryTimer = null;
    logoutSession(session.id, "expired").catch((err) => {
      console.error(`[${session.id}] Ralat auto-logout:`, err);
    });
  }, remaining);
  if (typeof session.expiryTimer.unref === "function") {
    session.expiryTimer.unref();
  }
}

function scheduleSessionReconnect(session, delayMs = 2500) {
  if (!sessions.has(session.id) || session.status === "logging_out") return;
  if (session.reconnectTimer) return;
  session.reconnectTimer = setTimeout(() => {
    session.reconnectTimer = null;
    if (!sessions.has(session.id) || session.status === "logging_out") return;
    if (session.expiresAt && Date.now() >= session.expiresAt) {
      logoutSession(session.id, "expired").catch(() => {});
      return;
    }
    startSessionSocket(session).catch((err) => {
      session.lastError = String(err?.message || err);
      console.error(`[${session.id}] Ralat menyambung semula WhatsApp:`, err);
      scheduleSessionReconnect(session, 5000);
    });
  }, delayMs);
}

function scheduleEnsurePairing(delayMs = 1500) {
  if (ensurePairingTimer) return;
  ensurePairingTimer = setTimeout(() => {
    ensurePairingTimer = null;
    ensurePairingSession().catch((err) => {
      console.error("[Pairing] Ralat menyediakan sesi QR baharu:", err);
    });
  }, delayMs);
}

async function destroySession(sessionId, options = {}) {
  const { callLogout = false, notifyReason = null, removeAuth = true } = options;
  const session = sessions.get(sessionId);
  if (!session) return false;

  session.status = "logging_out";
  clearSessionTimers(session);

  if (pairingSessionId === sessionId) {
    pairingSessionId = null;
  }

  const sock = session.sock;
  session.sock = null;

  if (sock) {
    if (notifyReason && session.userJid) {
      try {
        await Promise.race([sock.sendMessage(session.userJid, { text: notifyReason }), sleep(3000)]);
      } catch (_) {}
    }

    if (callLogout) {
      try {
        await Promise.race([sock.logout(), sleep(4000)]);
      } catch (_) {}
    }

    try {
      sock.ev.removeAllListeners();
      sock.end(undefined);
    } catch (_) {}
  }

  session.messageStore.clear();
  session.sentByBotIds.clear();
  session.groupAutoDecode.clear();
  session.msgRetryCounterCache.flushAll();
  sessions.delete(sessionId);

  if (removeAuth) {
    try {
      fs.rmSync(session.authDir, { recursive: true, force: true });
    } catch (_) {}
  }

  return true;
}

async function logoutSession(sessionId, reason = "manual") {
  const session = sessions.get(sessionId);
  if (!session || session.status === "logging_out") return false;

  const label = session.phone ? formatPhoneDisplay(session.phone, false) : sessionId;
  let notifyText = null;
  if (reason === "expired") {
    notifyText = [
      "⏱️ *Tempoh Sesi 1 Jam Tamat*",
      `Sesi CW Decoder anda (${SESSION_TTL_MINUTES} minit) telah tamat dan peranti telah dilog keluar secara automatik.`,
      "Sila imbas kod QR baharu di halaman `/qr` jika ingin guna semula.",
    ].join("\n");
  } else if (reason === "full") {
    notifyText = `🚫 *Had Maksimum Pengguna (${MAX_SESSIONS}/${MAX_SESSIONS})*\nSemua slot sedang digunakan. Sila cuba semula selepas salah satu sesi tamat.`;
  }

  console.log(`🔒 [${sessionId}] Log keluar (${reason}) untuk pengguna ${label}`);
  await destroySession(sessionId, {
    callLogout: reason !== "remote_logout" && reason !== "pairing_reset",
    notifyReason: notifyText,
    removeAuth: true,
  });

  scheduleEnsurePairing(800);
  return true;
}

async function ensurePairingSession() {
  const activeCount = getActiveSessions().length;
  if (activeCount >= MAX_SESSIONS) {
    const existingPairing = getPairingSession();
    if (existingPairing) {
      await destroySession(existingPairing.id, { callLogout: false, removeAuth: true });
    }
    return null;
  }

  const existing = getPairingSession();
  if (existing) return existing;

  const id = generateSessionId();
  const session = createSessionContext(id, { isPairing: true });
  sessions.set(id, session);
  pairingSessionId = id;

  try {
    await startSessionSocket(session);
  } catch (err) {
    session.lastError = String(err?.message || err);
    console.error(`[${id}] Gagal memulakan sesi QR:`, err);
    scheduleSessionReconnect(session, 4000);
  }
  return session;
}

async function refreshPairingSession() {
  const existing = getPairingSession();
  if (existing) {
    await destroySession(existing.id, { callLogout: false, removeAuth: true });
  }
  return ensurePairingSession();
}

async function resetAllSessions() {
  const ids = Array.from(sessions.keys());
  for (const id of ids) {
    await destroySession(id, { callLogout: true, removeAuth: true });
  }
  try {
    fs.rmSync(AUTH_DIR, { recursive: true, force: true });
  } catch (_) {}
  scheduleEnsurePairing(800);
}

async function startSessionSocket(session) {
  if (!sessions.has(session.id) || session.status === "logging_out") return;

  if (session.sock) {
    try {
      session.sock.ev.removeAllListeners();
      session.sock.end(undefined);
    } catch (_) {}
    session.sock = null;
  }

  fs.mkdirSync(session.authDir, { recursive: true });
  const { state: authState, saveCreds } = await useMultiFileAuthState(session.authDir);
  const version = await getBaileysVersionCached();
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
    msgRetryCounterCache: session.msgRetryCounterCache,
    getMessage: async (key) => {
      if (!key?.id) return undefined;
      return (
        (key.remoteJid ? session.messageStore.get(`${key.remoteJid}:${key.id}`) : undefined) ||
        session.messageStore.get(key.id) ||
        undefined
      );
    },
  });

  session.sock = sock;

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", async (update) => {
    if (session.sock !== sock || session.status === "logging_out") return;
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // If an active user somehow gets a QR prompt again (session invalidated), drop them to free the slot
      if (session.connectedAt !== null) {
        console.log(`⚠️ [${session.id}] Sesi aktif diminta imbas QR semula — dibersihkan.`);
        await logoutSession(session.id, "remote_logout");
        return;
      }
      session.status = "waiting_qr";
      session.qrText = qr;
      session.lastError = null;
      try {
        session.qrDataUrl = await QRCode.toDataURL(qr, { width: 320, margin: 2 });
        const asciiQr = await QRCode.toString(qr, { type: "terminal", small: true });
        console.log(`\n=== [${session.id}] IMBAS KOD QR WHATSAPP (${getActiveSessions().length}/${MAX_SESSIONS} aktif) ===\n${asciiQr}`);
      } catch (err) {
        console.error(`[${session.id}] Gagal jana imej QR:`, err);
      }
    }

    if (connection === "open") {
      const rawJid = sock.user?.id || "";
      const normJid = rawJid ? jidNormalizedUser(rawJid) : null;
      const phone = extractPhoneFromJid(rawJid);
      const displayName = sock.user?.name || session.name || "";

      // If this same WhatsApp account already has another active session, replace the older slot
      if (normJid) {
        const duplicates = getActiveSessions().filter((s) => s.id !== session.id && s.userJid === normJid);
        for (const dup of duplicates) {
          console.log(`♻️ [${session.id}] Menggantikan sesi lama ${dup.id} bagi pengguna ${phone}`);
          await destroySession(dup.id, {
            callLogout: Boolean(dup.rawJid && dup.rawJid !== rawJid),
            removeAuth: true,
          });
        }
      }

      // Enforce hard MAX_SESSIONS capacity for brand-new connections
      const otherActiveCount = getActiveSessions().filter((s) => s.id !== session.id).length;
      if (session.connectedAt === null && otherActiveCount >= MAX_SESSIONS) {
        session.rawJid = rawJid;
        session.userJid = normJid;
        session.phone = phone;
        await logoutSession(session.id, "full");
        return;
      }

      session.rawJid = rawJid;
      session.userJid = normJid;
      session.phone = phone;
      session.name = displayName;
      session.qrText = null;
      session.qrDataUrl = null;
      session.lastError = null;
      session.status = "connected";

      const isNewConnection = session.connectedAt === null;
      if (isNewConnection) {
        const now = Date.now();
        session.connectedAt = now;
        session.expiresAt = now + SESSION_TTL_MS;
        session.isPairing = false;
        if (pairingSessionId === session.id) {
          pairingSessionId = null;
        }
      } else if (session.expiresAt && Date.now() >= session.expiresAt) {
        await logoutSession(session.id, "expired");
        return;
      }

      saveSessionMeta(session);
      scheduleSessionExpiry(session);

      const activeTotal = getActiveSessions().length;
      console.log(
        `✅ [${session.id}] Disambung: ${displayName || "WhatsApp"} (${phone || rawJid}) | Aktif: ${activeTotal}/${MAX_SESSIONS} | Baki: ${formatRemainingMs(session.expiresAt - Date.now())}`
      );

      // Immediately prepare a fresh QR code for the next user if capacity remains
      if (isNewConnection && activeTotal < MAX_SESSIONS) {
        scheduleEnsurePairing(800);
      }
    }

    if (connection === "close") {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      const badSession =
        statusCode === DisconnectReason.badSession ||
        statusCode === DisconnectReason.connectionReplaced;
      const restartRequired = statusCode === DisconnectReason.restartRequired;
      const timedOut = statusCode === DisconnectReason.timedOut;

      session.lastError = lastDisconnect?.error?.message || `Disconnect code ${statusCode}`;
      console.log(`⚠️ [${session.id}] Terputus (kod=${statusCode}, pairing=${session.isPairing}).`);

      // Case A: Still in pairing stage (never completed `open` yet)
      if (session.connectedAt === null) {
        // After QR scan, WhatsApp requires a stream restart (515) with saved creds!
        if (restartRequired) {
          session.status = "reconnecting";
          scheduleSessionReconnect(session, 500);
          return;
        }
        // Otherwise (QR timed out / cancelled / bad session), replace with a clean pairing session
        await destroySession(session.id, { callLogout: false, removeAuth: true });
        scheduleEnsurePairing(1500);
        return;
      }

      // Case B: Already an active connected session
      if (session.expiresAt && Date.now() >= session.expiresAt) {
        await logoutSession(session.id, "expired");
        return;
      }

      if (loggedOut || badSession) {
        await logoutSession(session.id, "remote_logout");
        return;
      }

      session.status = "reconnecting";
      scheduleSessionReconnect(session, timedOut ? 2000 : 3000);
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify" || session.status === "logging_out") return;
    if (session.expiresAt && Date.now() >= session.expiresAt) {
      logoutSession(session.id, "expired").catch(() => {});
      return;
    }
    for (const msg of messages) {
      rememberMessageContent(session, msg);
      try {
        await handleIncomingMessage(sock, msg, session);
      } catch (err) {
        console.error(`[${session.id}][Message Handler Error]`, err);
      }
    }
  });
}

function sweepExpiredSessions() {
  const now = Date.now();
  for (const session of getActiveSessions()) {
    if (session.expiresAt && now >= session.expiresAt) {
      logoutSession(session.id, "expired").catch((err) => {
        console.error(`[${session.id}] Ralat sweep auto-logout:`, err);
      });
    }
  }
}

async function restoreSavedSessions() {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const entries = fs.readdirSync(AUTH_DIR, { withFileTypes: true });
  const now = Date.now();
  let restored = 0;

  for (const entry of entries) {
    const fullPath = path.join(AUTH_DIR, entry.name);
    // Clean up legacy single-user root auth files if migrating from older version
    if (!entry.isDirectory() || !entry.name.startsWith("session_")) {
      try {
        fs.rmSync(fullPath, { recursive: true, force: true });
      } catch (_) {}
      continue;
    }

    const meta = loadSessionMeta(fullPath);
    const credsPath = path.join(fullPath, "creds.json");
    if (!meta || !meta.connectedAt || !meta.expiresAt || meta.expiresAt <= now || !fs.existsSync(credsPath)) {
      try {
        fs.rmSync(fullPath, { recursive: true, force: true });
      } catch (_) {}
      continue;
    }

    if (restored >= MAX_SESSIONS) {
      try {
        fs.rmSync(fullPath, { recursive: true, force: true });
      } catch (_) {}
      continue;
    }

    const session = createSessionContext(entry.name, {
      isPairing: false,
      userJid: meta.userJid,
      phone: meta.phone,
      name: meta.name,
      connectedAt: meta.connectedAt,
      expiresAt: meta.expiresAt,
      decoded: meta.decoded,
      encoded: meta.encoded,
    });
    sessions.set(session.id, session);
    scheduleSessionExpiry(session);
    restored += 1;
    startSessionSocket(session).catch((err) => {
      session.lastError = String(err?.message || err);
      scheduleSessionReconnect(session, 4000);
    });
  }

  console.log(`🔄 Memulihkan ${restored} sesi aktif daripada cakera (maksimum ${MAX_SESSIONS} pengguna).`);
  await ensurePairingSession();
}

function getPublicStatusSnapshot(includeQrData = true) {
  const now = Date.now();
  const active = getActiveSessions();
  const pairing = getPairingSession();

  const serializedSessions = active.map((s, idx) => {
    const remainingMs = s.expiresAt ? Math.max(0, s.expiresAt - now) : 0;
    return {
      slot: idx + 1,
      id: s.id,
      name: s.name || "Pengguna WhatsApp",
      phone: formatPhoneDisplay(s.phone),
      status: s.status,
      connected_at: s.connectedAt ? new Date(s.connectedAt).toISOString() : null,
      expires_at: s.expiresAt ? new Date(s.expiresAt).toISOString() : null,
      expires_at_ms: s.expiresAt || null,
      remaining_sec: Math.ceil(remainingMs / 1000),
      remaining_human: formatRemainingMs(remainingMs),
      decoded_count: s.stats.decoded,
      encoded_count: s.stats.encoded,
    };
  });

  const nextFreeAtMs =
    active.length >= MAX_SESSIONS
      ? active.reduce((min, s) => (s.expiresAt && s.expiresAt < min ? s.expiresAt : min), Infinity)
      : null;

  return {
    max_sessions: MAX_SESSIONS,
    session_ttl_minutes: SESSION_TTL_MINUTES,
    active_count: active.length,
    available_slots: Math.max(0, MAX_SESSIONS - active.length),
    is_full: active.length >= MAX_SESSIONS,
    next_slot_free_at_ms: Number.isFinite(nextFreeAtMs) ? nextFreeAtMs : null,
    pairing: pairing
      ? {
          id: pairing.id,
          status: pairing.status,
          has_qr: Boolean(pairing.qrDataUrl),
          qr_data_url: includeQrData ? pairing.qrDataUrl : undefined,
          last_error: pairing.lastError,
        }
      : null,
    sessions: serializedSessions,
  };
}

// ============================================================================
// Web Dashboard UI (/qr & /)
// ============================================================================

function renderQrHtml() {
  const snap = getPublicStatusSnapshot(true);
  const nextSlotNumber = snap.active_count + 1;

  let qrCardHtml = "";
  if (snap.is_full) {
    const nextFreeMs = snap.next_slot_free_at_ms;
    const waitHtml = nextFreeMs
      ? `<p class="small">Slot terawal dijangka kosong dalam: <b class="countdown" data-expires-ms="${nextFreeMs}">${escapeHtml(formatRemainingMs(nextFreeMs - Date.now()))}</b></p>`
      : "";
    qrCardHtml = `
      <div class="badge full">🚫 SLOT PENUH (${snap.active_count} / ${snap.max_sessions})</div>
      <p>Semua <b>${snap.max_sessions} slot pengguna serentak</b> sedang digunakan.</p>
      ${waitHtml}
      <p class="small">Halaman ini akan memaparkan kod QR baharu secara automatik sebaik sahaja satu slot kosong.</p>
    `;
  } else if (snap.pairing && snap.pairing.qr_data_url) {
    qrCardHtml = `
      <div class="badge warn">📱 IMBAS KOD QR (Slot #${nextSlotNumber} / ${snap.max_sessions})</div>
      <p>Buka <b>WhatsApp</b> di telefon → <b>Peranti Dipautkan (Linked Devices)</b> → <b>Pautkan Peranti</b> → imbas QR di bawah:</p>
      <img id="qr-img" src="${snap.pairing.qr_data_url}" alt="WhatsApp QR Code" class="qr" />
      <p class="small">⏱️ Tempoh aktif setiap pengguna ialah <b>${snap.session_ttl_minutes} minit (1 jam)</b> sebelum auto-logout.</p>
      <p class="small">Selepas anda berjaya imbas, kod QR baharu akan dijana untuk pengguna seterusnya.</p>
      <form method="POST" action="/qr/refresh" style="margin-top:12px">
        <button type="submit" class="btn secondary">🔄 Jana Semula Kod QR</button>
      </form>
    `;
  } else {
    const statusText = snap.pairing ? snap.pairing.status : "preparing";
    const errText = snap.pairing?.last_error ? `<p class="small">Info: ${escapeHtml(snap.pairing.last_error)}</p>` : "";
    qrCardHtml = `
      <div class="badge info">⏳ MENYEDIAKAN KOD QR...</div>
      <p>Status semasa: <code>${escapeHtml(statusText)}</code></p>
      ${errText}
      <p class="small">Kod QR akan muncul secara automatik dalam beberapa saat.</p>
    `;
  }

  let usersRowsHtml = "";
  if (snap.sessions.length === 0) {
    usersRowsHtml = `
      <div class="empty-state">
        Tiada pengguna sedang bersambung buat masa ini.<br/>
        <span class="small">Imbas kod QR di atas untuk mula menggunakan CW Decoder.</span>
      </div>
    `;
  } else {
    usersRowsHtml = snap.sessions
      .map((s) => {
        const statusBadge =
          s.status === "connected"
            ? '<span class="pill ok">AKTIF</span>'
            : '<span class="pill info">SAMBUNG SEMULA</span>';
        return `
          <div class="user-item">
            <div class="user-main">
              <div class="user-title">
                <span class="slot-tag">#${s.slot}</span>
                <strong>${escapeHtml(s.name)}</strong>
                ${statusBadge}
              </div>
              <div class="user-meta">
                <span>📱 <code>${escapeHtml(s.phone)}</code></span>
                <span>🕒 Mula: <span class="local-time" data-iso="${escapeHtml(s.connected_at || "")}">${escapeHtml(s.connected_at || "-")}</span></span>
                <span>📊 Decode: <b>${s.decoded_count}</b> · Encode: <b>${s.encoded_count}</b></span>
              </div>
            </div>
            <div class="user-actions">
              <div class="timer-box">
                <span class="timer-label">Baki Masa</span>
                <span class="countdown timer-val" data-expires-ms="${s.expires_at_ms || 0}">${escapeHtml(s.remaining_human)}</span>
              </div>
              <form method="POST" action="/logout" onsubmit="return confirm('Log keluar sesi ini sekarang?');">
                <input type="hidden" name="id" value="${escapeHtml(s.id)}" />
                <button type="submit" class="btn danger-sm">Log Keluar</button>
              </form>
            </div>
          </div>
        `;
      })
      .join("");
  }

  const initialFingerprint = JSON.stringify({
    active: snap.sessions.map((s) => [s.id, s.status, s.decoded_count, s.encoded_count]),
    qr: snap.pairing?.qr_data_url ? snap.pairing.qr_data_url.slice(-32) : null,
    is_full: snap.is_full,
  });

  return `<!DOCTYPE html>
<html lang="ms">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <noscript><meta http-equiv="refresh" content="5"></noscript>
  <title>CW Decoder — WhatsApp QR (${snap.active_count}/${snap.max_sessions})</title>
  <style>
    * { box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f172a;
      color: #e2e8f0;
      margin: 0;
      padding: 20px 16px 40px;
      min-height: 100vh;
    }
    .container { max-width: 960px; margin: 0 auto; }
    .header { text-align: center; margin-bottom: 20px; }
    h1 { margin: 0 0 8px; font-size: 1.6rem; color: #38bdf8; }
    .stats-bar {
      display: flex; flex-wrap: wrap; justify-content: center; gap: 10px;
      margin-top: 10px;
    }
    .stat-chip {
      background: #1e293b; border: 1px solid #334155; border-radius: 999px;
      padding: 6px 14px; font-size: 0.85rem; color: #cbd5e1;
    }
    .stat-chip b { color: #38bdf8; }
    .grid {
      display: grid;
      grid-template-columns: minmax(290px, 380px) 1fr;
      gap: 18px;
      align-items: start;
    }
    @media (max-width: 780px) {
      .grid { grid-template-columns: 1fr; }
    }
    .card {
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 16px;
      padding: 22px;
      box-shadow: 0 18px 36px rgba(0,0,0,0.35);
    }
    .card.center { text-align: center; }
    h2 { margin: 0 0 14px; font-size: 1.08rem; color: #f8fafc; display: flex; justify-content: space-between; align-items: center; }
    .badge { display: inline-block; padding: 6px 14px; border-radius: 999px; font-weight: 700; font-size: 0.82rem; margin-bottom: 12px; }
    .ok { background: #14532d; color: #86efac; }
    .warn { background: #713f12; color: #fde047; }
    .info { background: #1e3a8a; color: #93c5fd; }
    .full { background: #7f1d1d; color: #fecaca; }
    .pill { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.7rem; font-weight: 700; }
    .qr { background: #fff; padding: 12px; border-radius: 12px; margin: 12px auto; max-width: 260px; width: 100%; display: block; }
    code { background: #0f172a; padding: 2px 6px; border-radius: 6px; color: #38bdf8; font-size: 0.85em; }
    .small { font-size: 0.84rem; color: #94a3b8; line-height: 1.45; }
    .btn {
      border: none; padding: 8px 14px; border-radius: 8px; cursor: pointer;
      font-size: 0.84rem; font-weight: 600;
    }
    .btn.secondary { background: #334155; color: #e2e8f0; }
    .btn.secondary:hover { background: #475569; }
    .btn.danger { background: #7f1d1d; color: #fecaca; }
    .btn.danger:hover { background: #991b1b; }
    .btn.danger-sm { background: #7f1d1d; color: #fecaca; padding: 6px 10px; font-size: 0.78rem; }
    .btn.danger-sm:hover { background: #991b1b; }
    .user-list { display: flex; flex-direction: column; gap: 10px; }
    .user-item {
      background: #0f172a;
      border: 1px solid #334155;
      border-radius: 12px;
      padding: 12px 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }
    .user-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
    .slot-tag {
      background: #1e293b; color: #38bdf8; border: 1px solid #334155;
      border-radius: 6px; padding: 1px 7px; font-size: 0.78rem; font-weight: 700;
    }
    .user-meta { display: flex; flex-wrap: wrap; gap: 8px 14px; font-size: 0.8rem; color: #94a3b8; }
    .user-actions { display: flex; align-items: center; gap: 10px; margin-left: auto; }
    .timer-box { text-align: right; }
    .timer-label { display: block; font-size: 0.68rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.04em; }
    .timer-val { font-variant-numeric: tabular-nums; font-weight: 700; color: #fde047; font-size: 0.95rem; }
    .empty-state {
      text-align: center; padding: 28px 16px; color: #94a3b8;
      border: 1px dashed #334155; border-radius: 12px;
    }
    .tips {
      margin-top: 16px; padding-top: 14px; border-top: 1px solid #334155;
      font-size: 0.82rem; color: #94a3b8; line-height: 1.5;
    }
    .footer-actions { margin-top: 16px; display: flex; justify-content: flex-end; }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>📡 CW Decoder WhatsApp</h1>
      <div class="stats-bar">
        <span class="stat-chip">👥 Pengguna Aktif: <b>${snap.active_count} / ${snap.max_sessions}</b></span>
        <span class="stat-chip">🟢 Slot Kosong: <b>${snap.available_slots}</b></span>
        <span class="stat-chip">⏱️ Had Masa: <b>${snap.session_ttl_minutes} minit / sesi</b></span>
      </div>
    </div>

    <div class="grid">
      <div class="card center">
        <h2>Pautkan Peranti Baharu</h2>
        ${qrCardHtml}
      </div>

      <div class="card">
        <h2>
          <span>👥 Info Pengguna Aktif</span>
          <span class="small">${snap.active_count} daripada ${snap.max_sessions} slot</span>
        </h2>
        <div class="user-list">
          ${usersRowsHtml}
        </div>
        <div class="tips">
          <b>Tip guna di WhatsApp:</b> Hantar <i>voice note</i> CW untuk nyahkod, taip <code>/encode HELLO</code> atau <b>Reply</b> mana-mana teks dengan <code>/encode</code> untuk jana bunyi CW, dan taip <code>/masa</code> untuk semak baki sesi.
        </div>
        ${
          snap.active_count > 0
            ? `<div class="footer-actions">
                <form method="POST" action="/reset" onsubmit="return confirm('Log keluar SEMUA pengguna aktif dan jana QR baharu?');">
                  <button type="submit" class="btn danger">Log Keluar Semua Sesi</button>
                </form>
              </div>`
            : ""
        }
      </div>
    </div>
  </div>

  <script>
    (function () {
      const initialFp = ${JSON.stringify(initialFingerprint)};

      function formatRem(ms) {
        const s = Math.max(0, Math.floor(ms / 1000));
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        const r = s % 60;
        if (h > 0) return h + "j " + String(m).padStart(2, "0") + "m " + String(r).padStart(2, "0") + "s";
        return m + "m " + String(r).padStart(2, "0") + "s";
      }

      document.querySelectorAll(".local-time").forEach((el) => {
        const iso = el.getAttribute("data-iso");
        if (!iso) return;
        const d = new Date(iso);
        if (!Number.isNaN(d.getTime())) {
          el.textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
        }
      });

      let reloadScheduled = false;
      function tickCountdowns() {
        const now = Date.now();
        let anyExpired = false;
        document.querySelectorAll(".countdown").forEach((el) => {
          const exp = Number(el.getAttribute("data-expires-ms") || 0);
          if (!exp) return;
          const rem = exp - now;
          el.textContent = formatRem(rem);
          if (rem <= 0) anyExpired = true;
        });
        if (anyExpired && !reloadScheduled) {
          reloadScheduled = true;
          setTimeout(() => window.location.reload(), 1200);
        }
      }
      tickCountdowns();
      setInterval(tickCountdowns, 1000);

      async function pollStatus() {
        try {
          const resp = await fetch("/status", { cache: "no-store" });
          if (!resp.ok) return;
          const data = await resp.json();
          const fp = JSON.stringify({
            active: (data.sessions || []).map((s) => [s.id, s.status, s.decoded_count, s.encoded_count]),
            qr: data.pairing && data.pairing.qr_data_url ? data.pairing.qr_data_url.slice(-32) : null,
            is_full: data.is_full,
          });
          if (fp !== initialFp) {
            window.location.reload();
          }
        } catch (_) {}
      }
      setInterval(pollStatus, 3000);
    })();
  </script>
</body>
</html>`;
}

// ============================================================================
// Keep-Alive Hub for pinging other services when /health is hit
// ============================================================================

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

function readRequestBody(req, maxBytes = 16384) {
  return new Promise((resolve) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size <= maxBytes) chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", () => resolve(""));
  });
}

// ============================================================================
// HTTP Server (Render health check, QR dashboard, session status & controls)
// ============================================================================

const server = http.createServer(async (req, res) => {
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
    const snap = getPublicStatusSnapshot(false);
    res.writeHead(200, headers);
    res.end(
      JSON.stringify({
        status: "ok",
        service: "CW Decoder WhatsApp QR Bot (Multi-User)",
        max_sessions: snap.max_sessions,
        session_ttl_minutes: snap.session_ttl_minutes,
        active_sessions: snap.active_count,
        available_slots: snap.available_slots,
        connected: snap.active_count > 0,
        pairing_status: snap.pairing ? snap.pairing.status : snap.is_full ? "full" : "idle",
        sessions: snap.sessions,
        started_at: STARTED_AT,
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

  if (url === "/status" && req.method === "GET") {
    res.writeHead(200, {
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
    });
    res.end(JSON.stringify(getPublicStatusSnapshot(true)));
    return;
  }

  if (url === "/qr/refresh" && req.method === "POST") {
    try {
      await refreshPairingSession();
    } catch (err) {
      console.error("[/qr/refresh Error]", err);
    }
    res.writeHead(303, { Location: "/qr" });
    res.end();
    return;
  }

  if (url === "/logout" && req.method === "POST") {
    let targetId = parsedUrl.searchParams.get("id") || "";
    if (!targetId) {
      const body = await readRequestBody(req);
      const params = new URLSearchParams(body);
      targetId = params.get("id") || "";
    }
    if (targetId && sessions.has(targetId)) {
      try {
        await logoutSession(targetId, "manual");
      } catch (err) {
        console.error("[/logout Error]", err);
      }
    }
    res.writeHead(303, { Location: "/qr" });
    res.end();
    return;
  }

  if (url === "/reset" && req.method === "POST") {
    try {
      await resetAllSessions();
    } catch (err) {
      console.error("[/reset Error]", err);
    }
    res.writeHead(303, { Location: "/qr" });
    res.end();
    return;
  }

  if (url === "/" || url === "/qr") {
    res.writeHead(200, {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "no-store",
    });
    res.end(renderQrHtml());
    return;
  }

  res.writeHead(404, { "Content-Type": "text/plain" });
  res.end("Not Found");
});

if (require.main === module) {
  const sweepTimer = setInterval(sweepExpiredSessions, 30000);
  if (typeof sweepTimer.unref === "function") {
    sweepTimer.unref();
  }

  server.listen(PORT, "0.0.0.0", () => {
    console.log(`🌐 Pelayan CW Decoder WhatsApp QR berjalan di http://0.0.0.0:${PORT}`);
    console.log(`📱 Buka http://localhost:${PORT}/qr untuk imbas kod QR WhatsApp.`);
    console.log(`👥 Mod Multi-Pengguna: Maksimum ${MAX_SESSIONS} pengguna serentak | Auto-logout: ${SESSION_TTL_MINUTES} minit.`);
    if (KEEPALIVE_URLS.length > 0) {
      console.log(`💓 Keep-Alive Hub aktif untuk ${KEEPALIVE_URLS.length} servis sasaran apabila /health dipanggil.`);
    }
    restoreSavedSessions().catch((err) => {
      console.error("Ralat memulakan sesi WhatsApp:", err);
      scheduleEnsurePairing(3000);
    });
  });
} else {
  module.exports = {
    MAX_SESSIONS,
    SESSION_TTL_MINUTES,
    SESSION_TTL_MS,
    sessions,
    createSessionContext,
    getActiveSessions,
    getPairingSession,
    getPublicStatusSnapshot,
    renderQrHtml,
    unwrapMessage,
    extractMessageText,
    extractQuotedText,
    extractQuotedAudio,
    parseEncodeArgs,
    handleIncomingMessage,
    ensurePairingSession,
    logoutSession,
    destroySession,
    sweepExpiredSessions,
    scheduleSessionExpiry,
    extractPhoneFromJid,
    formatPhoneDisplay,
    formatRemainingMs,
    escapeHtml,
    startSessionSocket,
  };
}
