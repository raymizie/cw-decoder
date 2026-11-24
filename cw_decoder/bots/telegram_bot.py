"""Telegram Bot (Polling) for CW Decoder using Telegram Bot API over requests."""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any

import requests

from ..audio_io import SUPPORTED_AUDIO_EXTENSIONS, AudioLoadError, find_ffmpeg
from ..config import TelegramConfig
from ..decoder import CWDecoder
from ..encoder import CWEncoder, EncodeOptions
from .formatting import EncodeCommand, format_decode_reply, handle_simple_text_command

logger = logging.getLogger("cw_decoder.telegram")


class TelegramCWBot:
    """Long-polling Telegram Bot for decoding CW audio and generating test CW tones."""

    def __init__(
        self,
        config: TelegramConfig,
        decoder: CWDecoder | None = None,
        encoder: CWEncoder | None = None,
        session: requests.Session | None = None,
    ) -> None:
        if not config.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN tidak ditetapkan. Sila isi dalam fail .env.")
        self.config = config
        self.decoder = decoder or CWDecoder()
        self.encoder = encoder or CWEncoder()
        self.session = session or requests.Session()
        self.api_base = f"https://api.telegram.org/bot{config.bot_token}"
        self.file_base = f"https://api.telegram.org/file/bot{config.bot_token}"
        self._offset: int = 0
        self._running: bool = False

    def _api_call(self, method: str, timeout: int = 30, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.api_base}/{method}"
        resp = self.session.post(url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error ({method}): {data}")
        return data

    def send_message(self, chat_id: int, text: str, reply_to_message_id: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        return self._api_call("sendMessage", json=payload)

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self._api_call("sendChatAction", json={"chat_id": chat_id, "action": action}, timeout=10)
        except Exception:
            pass

    def send_audio_file(
        self,
        chat_id: int,
        wav_path: Path,
        caption: str,
        title: str = "CW Audio",
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "chat_id": str(chat_id),
            "caption": caption,
            "parse_mode": "Markdown",
            "title": title,
        }
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = str(reply_to_message_id)

        with wav_path.open("rb") as fh:
            return self._api_call(
                "sendAudio",
                data=data,
                files={"audio": (wav_path.name, fh, "audio/wav")},
                timeout=60,
            )

    def download_telegram_file(self, file_id: str, suffix: str = ".ogg") -> Path:
        meta = self._api_call("getFile", json={"file_id": file_id}, timeout=20)
        file_path = meta["result"]["file_path"]
        ext = Path(file_path).suffix or suffix

        download_url = f"{self.file_base}/{file_path}"
        resp = self.session.get(download_url, timeout=60)
        resp.raise_for_status()

        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tmp.write(resp.content)
        tmp.close()
        return Path(tmp.name)

    def _extract_audio_attachment(self, message: dict[str, Any]) -> tuple[str, str] | None:
        """Return (file_id, default_suffix) if message contains voice or audio."""
        if "voice" in message:
            return message["voice"]["file_id"], ".ogg"
        if "audio" in message:
            audio = message["audio"]
            fname = audio.get("file_name") or ""
            ext = Path(fname).suffix.lower() or ".mp3"
            return audio["file_id"], ext
        if "document" in message:
            doc = message["document"]
            mime = (doc.get("mime_type") or "").lower()
            fname = doc.get("file_name") or ""
            ext = Path(fname).suffix.lower()
            if mime.startswith("audio/") or ext in SUPPORTED_AUDIO_EXTENSIONS:
                return doc["file_id"], (ext or ".wav")
        return None

    def _extract_reply_text(self, message: dict[str, Any]) -> str | None:
        """Return text or caption from the replied-to message, if any."""
        reply = message.get("reply_to_message")
        if not isinstance(reply, dict):
            return None
        reply_text = reply.get("text") or reply.get("caption")
        if isinstance(reply_text, str) and reply_text.strip():
            return reply_text.strip()
        return None

    def process_update(self, update: dict[str, Any]) -> None:
        """Process a single Telegram Update dict."""
        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        if chat_id is None:
            return

        if self.config.allowed_chat_ids and chat_id not in self.config.allowed_chat_ids:
            logger.warning("Mengabaikan mesej daripada chat_id tidak dibenarkan: %s", chat_id)
            return

        # 1. Check audio / voice attachment
        attachment = self._extract_audio_attachment(message)
        if attachment:
            file_id, suffix = attachment
            self.send_chat_action(chat_id, "typing")
            tmp_audio: Path | None = None
            try:
                tmp_audio = self.download_telegram_file(file_id, suffix=suffix)
                result = self.decoder.decode_file(tmp_audio)
                self.send_message(chat_id, format_decode_reply(result), reply_to_message_id=message_id)
            except AudioLoadError as exc:
                self.send_message(chat_id, f"❌ *Ralat audio:* {exc}", reply_to_message_id=message_id)
            except Exception as exc:
                logger.exception("Ralat menyahkod audio Telegram")
                self.send_message(chat_id, f"❌ *Gagal menyahkod audio:* {exc}", reply_to_message_id=message_id)
            finally:
                if tmp_audio is not None:
                    tmp_audio.unlink(missing_ok=True)
            return

        # 2. Check text commands
        text = message.get("text")
        if text is None:
            return

        if text.strip().lower().startswith("/status"):
            ff = "Tersedia ✅" if find_ffmpeg() else "Tiada ⚠️ (WAV sahaja)"
            self.send_message(
                chat_id,
                f"🤖 *CW Decoder Bot Aktif*\n• FFmpeg (OGG/MP3/M4A): {ff}",
                reply_to_message_id=message_id,
            )
            return

        reply_text = self._extract_reply_text(message)
        action, payload = handle_simple_text_command(text, reply_text=reply_text)
        if action == "reply" and isinstance(payload, str):
            self.send_message(chat_id, payload, reply_to_message_id=message_id)
        elif action == "encode" and isinstance(payload, EncodeCommand):
            self.send_chat_action(chat_id, "upload_audio")
            tmp_wav = Path(tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name)
            try:
                opts = EncodeOptions(wpm=payload.wpm, freq_hz=payload.freq_hz)
                _, morse = self.encoder.encode_text_to_wav(payload.text, tmp_wav, options=opts)
                if not morse:
                    self.send_message(
                        chat_id,
                        "⚠️ Tiada aksara yang boleh ditukar kepada kod Morse dalam teks tersebut.",
                        reply_to_message_id=message_id,
                    )
                    return
                safe_text = payload.text.upper().replace("`", "'")
                caption = (
                    f"🔊 *Audio CW Dijana*\n"
                    f"*Teks:* `{safe_text}`\n"
                    f"*Morse:* `{morse}`\n"
                    f"_{payload.wpm:.0f} WPM @ {payload.freq_hz:.0f} Hz_"
                )
                self.send_audio_file(
                    chat_id,
                    tmp_wav,
                    caption=caption,
                    title=f"CW - {payload.text[:24].upper()}",
                    reply_to_message_id=message_id,
                )
            finally:
                tmp_wav.unlink(missing_ok=True)

    def run_forever(self) -> None:
        """Start long-polling loop until KeyboardInterrupt."""
        me = self._api_call("getMe", timeout=15)["result"]
        logger.info("Telegram CW Decoder Bot berjalan sebagai @%s (id=%s)", me.get("username"), me.get("id"))
        if not find_ffmpeg():
            logger.warning(
                "FFmpeg tidak ditemui! Voice note Telegram (.ogg) memerlukan FFmpeg atau `pip install imageio-ffmpeg`."
            )

        self._running = True
        backoff = 1.0
        while self._running:
            try:
                data = self._api_call(
                    "getUpdates",
                    timeout=self.config.poll_timeout + 15,
                    json={
                        "offset": self._offset,
                        "timeout": self.config.poll_timeout,
                        "allowed_updates": ["message", "edited_message"],
                    },
                )
                backoff = 1.0
                for update in data.get("result", []):
                    self._offset = max(self._offset, int(update["update_id"]) + 1)
                    try:
                        self.process_update(update)
                    except Exception:
                        logger.exception("Ralat memproses update_id=%s", update.get("update_id"))
            except KeyboardInterrupt:
                logger.info("Bot Telegram dihentikan oleh pengguna.")
                self._running = False
            except Exception as exc:
                logger.error("Ralat polling Telegram: %s (cuba semula dalam %.0fs)", exc, backoff)
                time.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)


def run_telegram_bot() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = TelegramConfig.from_env()
    bot = TelegramCWBot(config)
    bot.run_forever()
