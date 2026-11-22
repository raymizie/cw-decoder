"""Shared message formatting and text command parsing for Telegram & WhatsApp bots."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

from .. import __version__
from ..decoder import DecodeResult
from ..morse import decode_morse, encode_morse

HELP_TEXT = (
    f"📡 *CW Decoder v{__version__}*\n"
    "Hantar rakaman suara (*voice note*) atau fail audio CW (WAV/OGG/MP3/M4A) "
    "dan saya akan nyahkod bunyi Morse kepada perkataan secara automatik!\n\n"
    "*Arahan Tersedia:*\n"
    "• Hantar audio/voice note → Nyahkod CW kepada teks\n"
    "• `/encode <teks>` → Jana audio bunyi CW (.wav)\n"
    "  _(Contoh: `/encode --wpm 18 --freq 700 HELLO WORLD`)_\n"
    "• `/morse <kod>` → Nyahkod teks Morse terus\n"
    "  _(Contoh: `/morse .... . .-.. .-.. ---`)_\n"
    "• `/tomorse <teks>` → Tukar teks kepada simbol Morse\n"
    "• `/help` → Papar panduan ini"
)


@dataclass
class EncodeCommand:
    text: str
    wpm: float = 20.0
    freq_hz: float = 700.0


def format_decode_reply(result: DecodeResult) -> str:
    """Format a CW DecodeResult as a clean chat message."""
    if result.is_empty:
        return (
            "⚠️ *Tiada isyarat CW/Morse yang jelas dikesan.*\n"
            f"• Durasi: {result.duration_sec:.2f}s\n"
            f"• Nada dikesan: {result.tone_hz:.0f} Hz (SNR {result.snr_db:.1f} dB)\n\n"
            "_Tip: Pastikan bunyi CW jelas dan tidak terlalu pendek._"
        )

    return (
        "📡 *Hasil Nyahkod CW Decoder*\n"
        f"*Teks:* `{result.text}`\n"
        f"*Morse:* `{result.morse}`\n"
        f"*Kelajuan:* {result.wpm:.1f} WPM | *Nada:* {result.tone_hz:.0f} Hz\n"
        f"*Keyakinan:* {int(result.confidence * 100)}% (SNR {result.snr_db:.1f} dB, {result.duration_sec:.1f}s)"
    )


def parse_encode_command(raw_args: str) -> EncodeCommand | None:
    """Parse arguments for `/encode [--wpm N] [--freq HZ] <text>`."""
    if not raw_args or not raw_args.strip():
        return None

    try:
        tokens = shlex.split(raw_args)
    except ValueError:
        tokens = raw_args.split()

    wpm = 20.0
    freq_hz = 700.0
    text_parts: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--wpm", "-w") and i + 1 < len(tokens):
            try:
                wpm = max(5.0, min(50.0, float(tokens[i + 1])))
                i += 2
                continue
            except ValueError:
                pass
        elif tok in ("--freq", "-f") and i + 1 < len(tokens):
            try:
                freq_hz = max(300.0, min(2500.0, float(tokens[i + 1])))
                i += 2
                continue
            except ValueError:
                pass
        text_parts.append(tok)
        i += 1

    text = " ".join(text_parts).strip()
    if not text:
        return None
    return EncodeCommand(text=text, wpm=wpm, freq_hz=freq_hz)


def handle_simple_text_command(message_text: str) -> tuple[str, str | EncodeCommand]:
    """Classify an incoming text message.

    Returns (action, payload):
    - ("reply", text_reply)
    - ("encode", EncodeCommand)
    """
    text = (message_text or "").strip()
    if not text:
        return "reply", HELP_TEXT

    lower = text.lower()
    cmd_word = lower.split()[0]
    # Strip @BotUsername suffix in Telegram group commands
    if "@" in cmd_word and cmd_word.startswith("/"):
        cmd_word = cmd_word.split("@", 1)[0]

    rest = text[len(text.split()[0]) :].strip() if text.split() else ""

    if cmd_word in ("/start", "/help", "help", "bantuan", "menu"):
        return "reply", HELP_TEXT

    if cmd_word in ("/encode", "encode", "/cw"):
        parsed = parse_encode_command(rest)
        if not parsed:
            return (
                "reply",
                "Sila masukkan teks selepas `/encode`.\nContoh: `/encode HELLO WORLD` atau `/encode --wpm 18 CQ CQ`",
            )
        return "encode", parsed

    if cmd_word in ("/morse", "morse"):
        if not rest:
            return "reply", "Sila masukkan kod Morse.\nContoh: `/morse .... . .-.. .-.. ---`"
        decoded = decode_morse(rest)
        return "reply", f"🔤 *Teks:* `{decoded}`\n*Morse:* `{rest}`"

    if cmd_word in ("/tomorse", "tomorse"):
        if not rest:
            return "reply", "Sila masukkan teks.\nContoh: `/tomorse HELLO`"
        encoded = encode_morse(rest)
        return "reply", f"•− *Morse:* `{encoded}`\n*Teks:* `{rest.upper()}`"

    # If user directly types dots and dashes without a command, decode it!
    stripped_symbols = set(text.replace(" ", "").replace("/", ""))
    if stripped_symbols and stripped_symbols.issubset({".", "-", "·", "•", "∙", "−", "‒", "–", "—", "_"}):
        decoded = decode_morse(text)
        return "reply", f"🔤 *Teks:* `{decoded}`\n*Morse:* `{text}`"

    return (
        "reply",
        "Hantar *voice note* atau fail audio CW untuk nyahkod bunyi Morse.\n"
        "Taip `/help` untuk lihat semua arahan.",
    )
