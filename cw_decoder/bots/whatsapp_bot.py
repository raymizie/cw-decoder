"""WhatsApp Webhook Bot (Flask) supporting Meta WhatsApp Cloud API and Twilio WhatsApp API."""

from __future__ import annotations

import logging
import mimetypes
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

import requests
from flask import Flask, Response, jsonify, request, send_file

from .. import __version__
from ..audio_io import AudioLoadError, find_ffmpeg
from ..config import WhatsAppConfig
from ..decoder import CWDecoder
from ..encoder import CWEncoder, EncodeOptions
from .formatting import EncodeCommand, format_decode_reply, handle_simple_text_command

logger = logging.getLogger("cw_decoder.whatsapp")

_MIME_TO_EXT = {
    "audio/ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/ogg; codecs=opus": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
}


def _ext_from_mime(mime_type: str | None) -> str:
    if not mime_type:
        return ".ogg"
    clean = mime_type.split(";")[0].strip().lower()
    if clean in _MIME_TO_EXT:
        return _MIME_TO_EXT[clean]
    guessed = mimetypes.guess_extension(clean)
    return guessed or ".ogg"


def _twiml_response(body: str, media_url: str | None = None) -> Response:
    media_tag = f"<Media>{xml_escape(media_url)}</Media>" if media_url else ""
    xml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Message><Body>{xml_escape(body)}</Body>{media_tag}</Message></Response>'
    return Response(xml, status=200, mimetype="application/xml")


class MetaCloudClient:
    """Thin HTTP client for Meta WhatsApp Cloud API."""

    def __init__(self, config: WhatsAppConfig, session: requests.Session | None = None) -> None:
        self.config = config
        self.session = session or requests.Session()
        self.graph_base = f"https://graph.facebook.com/{config.meta_api_version}"

    @property
    def is_configured(self) -> bool:
        return bool(self.config.meta_access_token and self.config.meta_phone_number_id)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.meta_access_token}"}

    def send_text(self, to: str, body: str) -> dict[str, Any]:
        if not self.is_configured:
            logger.warning("META_ACCESS_TOKEN / META_PHONE_NUMBER_ID tidak ditetapkan; mesej tidak dihantar.")
            return {"skipped": True}
        url = f"{self.graph_base}/{self.config.meta_phone_number_id}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        }
        resp = self.session.post(url, headers=self._headers(), json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def download_media(self, media_id: str, mime_type: str | None = None) -> Path:
        meta_url = f"{self.graph_base}/{media_id}"
        meta_resp = self.session.get(meta_url, headers=self._headers(), timeout=20)
        meta_resp.raise_for_status()
        meta_json = meta_resp.json()
        media_url = meta_json["url"]
        ext = _ext_from_mime(mime_type or meta_json.get("mime_type"))

        file_resp = self.session.get(media_url, headers=self._headers(), timeout=60)
        file_resp.raise_for_status()

        tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
        tmp.write(file_resp.content)
        tmp.close()
        return Path(tmp.name)

    def upload_and_send_audio(self, to: str, wav_path: Path, caption_text: str) -> dict[str, Any]:
        if not self.is_configured:
            return {"skipped": True}

        upload_url = f"{self.graph_base}/{self.config.meta_phone_number_id}/media"
        with wav_path.open("rb") as fh:
            up_resp = self.session.post(
                upload_url,
                headers=self._headers(),
                data={"messaging_product": "whatsapp", "type": "audio/wav"},
                files={"file": (wav_path.name, fh, "audio/wav")},
                timeout=60,
            )
            up_resp.raise_for_status()
            media_id = up_resp.json()["id"]

        msg_url = f"{self.graph_base}/{self.config.meta_phone_number_id}/messages"
        audio_payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "audio",
            "audio": {"id": media_id},
        }
        resp = self.session.post(msg_url, headers=self._headers(), json=audio_payload, timeout=30)
        resp.raise_for_status()
        self.send_text(to, caption_text)
        return resp.json()


def create_whatsapp_app(
    config: WhatsAppConfig | None = None,
    decoder: CWDecoder | None = None,
    encoder: CWEncoder | None = None,
    session: requests.Session | None = None,
) -> Flask:
    """Create the Flask application serving Meta Cloud API & Twilio WhatsApp webhooks."""
    cfg = config or WhatsAppConfig.from_env()
    dec = decoder or CWDecoder()
    enc = encoder or CWEncoder()
    http_session = session or requests.Session()
    meta_client = MetaCloudClient(cfg, session=http_session)

    app = Flask("cw_decoder_whatsapp")
    media_dir = Path(tempfile.mkdtemp(prefix="cw_wa_media_"))
    media_registry: dict[str, tuple[Path, float]] = {}

    def _cleanup_old_media(ttl_sec: float = 900.0) -> None:
        now = time.time()
        expired = [tok for tok, (_, ts) in media_registry.items() if now - ts > ttl_sec]
        for tok in expired:
            path, _ = media_registry.pop(tok)
            path.unlink(missing_ok=True)

    def _register_wav(cmd: EncodeCommand) -> tuple[str, Path, str]:
        _cleanup_old_media()
        token = uuid.uuid4().hex
        wav_path = media_dir / f"{token}.wav"
        opts = EncodeOptions(wpm=cmd.wpm, freq_hz=cmd.freq_hz)
        _, morse = enc.encode_text_to_wav(cmd.text, wav_path, options=opts)
        media_registry[token] = (wav_path, time.time())
        return token, wav_path, morse

    def _handle_meta_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        processed = 0
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value") or {}
                for msg in value.get("messages", []):
                    sender = msg.get("from")
                    msg_type = msg.get("type")
                    if not sender:
                        continue
                    processed += 1

                    if msg_type == "audio":
                        audio_obj = msg.get("audio") or {}
                        media_id = audio_obj.get("id")
                        mime = audio_obj.get("mime_type")
                        tmp_path: Path | None = None
                        try:
                            tmp_path = meta_client.download_media(media_id, mime_type=mime)
                            result = dec.decode_file(tmp_path)
                            meta_client.send_text(sender, format_decode_reply(result))
                        except AudioLoadError as exc:
                            meta_client.send_text(sender, f"❌ *Ralat audio:* {exc}")
                        except Exception as exc:
                            logger.exception("Ralat memproses audio Meta WhatsApp")
                            meta_client.send_text(sender, f"❌ *Gagal menyahkod audio:* {exc}")
                        finally:
                            if tmp_path is not None:
                                tmp_path.unlink(missing_ok=True)

                    elif msg_type == "text":
                        body = (msg.get("text") or {}).get("body", "")
                        action, cmd_payload = handle_simple_text_command(body)
                        if action == "reply" and isinstance(cmd_payload, str):
                            meta_client.send_text(sender, cmd_payload)
                        elif action == "encode" and isinstance(cmd_payload, EncodeCommand):
                            _, wav_path, morse = _register_wav(cmd_payload)
                            caption = (
                                f"🔊 *Audio CW Dijana*\n"
                                f"*Teks:* `{cmd_payload.text.upper()}`\n"
                                f"*Morse:* `{morse}` ({cmd_payload.wpm:.0f} WPM @ {cmd_payload.freq_hz:.0f} Hz)"
                            )
                            try:
                                meta_client.upload_and_send_audio(sender, wav_path, caption)
                            except Exception as exc:
                                logger.exception("Gagal muat naik audio Meta WhatsApp")
                                meta_client.send_text(sender, f"{caption}\n_(Gagal hantar fail audio: {exc})_")
                    else:
                        meta_client.send_text(
                            sender,
                            "Sila hantar *voice note* atau fail audio CW untuk nyahkod. Taip `/help` untuk panduan.",
                        )

        return {"status": "ok", "processed": processed}, 200

    def _handle_twilio_form() -> Response:
        num_media = int(request.form.get("NumMedia", "0") or "0")
        body = (request.form.get("Body") or "").strip()

        if num_media > 0:
            media_url = request.form.get("MediaUrl0", "").strip()
            content_type = request.form.get("MediaContentType0", "").strip()
            if not content_type.lower().startswith("audio/"):
                return _twiml_response("Sila hantar fail audio atau voice note CW sahaja.")

            tmp_path: Path | None = None
            try:
                auth = (
                    (cfg.twilio_account_sid, cfg.twilio_auth_token)
                    if cfg.twilio_account_sid and cfg.twilio_auth_token
                    else None
                )
                resp = http_session.get(media_url, auth=auth, timeout=60)
                resp.raise_for_status()
                ext = _ext_from_mime(content_type)
                tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                tmp.write(resp.content)
                tmp.close()
                tmp_path = Path(tmp.name)

                result = dec.decode_file(tmp_path)
                return _twiml_response(format_decode_reply(result))
            except AudioLoadError as exc:
                return _twiml_response(f"❌ Ralat audio: {exc}")
            except Exception as exc:
                logger.exception("Ralat menyahkod audio Twilio WhatsApp")
                return _twiml_response(f"❌ Gagal menyahkod audio: {exc}")
            finally:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)

        action, cmd_payload = handle_simple_text_command(body)
        if action == "reply" and isinstance(cmd_payload, str):
            return _twiml_response(cmd_payload)
        if action == "encode" and isinstance(cmd_payload, EncodeCommand):
            token, _, morse = _register_wav(cmd_payload)
            base_url = cfg.public_base_url or request.url_root.rstrip("/")
            media_url = f"{base_url}/media/{token}.wav" if cfg.public_base_url else None
            caption = (
                f"🔊 *Audio CW Dijana*\n"
                f"*Teks:* `{cmd_payload.text.upper()}`\n"
                f"*Morse:* `{morse}`\n"
                f"_{cmd_payload.wpm:.0f} WPM @ {cmd_payload.freq_hz:.0f} Hz_"
            )
            if not media_url:
                caption += "\n_(Tetapkan PUBLIC_BASE_URL dalam .env supaya Twilio dapat menghantar fail WAV)_"
            return _twiml_response(caption, media_url=media_url)

        return _twiml_response("Taip `/help` untuk panduan CW Decoder.")

    @app.get("/")
    def index() -> Response:
        return jsonify(
            {
                "service": "CW Decoder WhatsApp Webhook",
                "version": __version__,
                "status": "online",
                "ffmpeg_available": find_ffmpeg() is not None,
                "endpoints": {
                    "health": "/health",
                    "unified_webhook": "/webhook",
                    "meta_webhook": "/webhook/meta",
                    "twilio_webhook": "/webhook/twilio",
                },
            }
        )

    @app.get("/health")
    def health() -> Response:
        return jsonify(
            {
                "status": "ok",
                "service": "CW Decoder WhatsApp Webhook",
                "version": __version__,
                "provider": cfg.provider,
                "ffmpeg_available": find_ffmpeg() is not None,
            }
        )

    @app.get("/media/<token>.wav")
    def serve_media(token: str) -> Response:
        _cleanup_old_media()
        entry = media_registry.get(token)
        if not entry or not entry[0].is_file():
            return Response("Media tidak dijumpai", status=404)
        return send_file(entry[0], mimetype="audio/wav", as_attachment=False)

    @app.route("/webhook/meta", methods=["GET", "POST"])
    def webhook_meta() -> Any:
        if request.method == "GET":
            mode = request.args.get("hub.mode")
            token = request.args.get("hub.verify_token")
            challenge = request.args.get("hub.challenge", "")
            if mode == "subscribe" and token == cfg.meta_verify_token:
                return Response(challenge, status=200, mimetype="text/plain")
            return Response("Forbidden", status=403)

        payload = request.get_json(silent=True) or {}
        data, code = _handle_meta_payload(payload)
        return jsonify(data), code

    @app.post("/webhook/twilio")
    def webhook_twilio() -> Response:
        return _handle_twilio_form()

    @app.route("/webhook", methods=["GET", "POST"])
    def webhook_unified() -> Any:
        if request.method == "GET":
            return webhook_meta()
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            data, code = _handle_meta_payload(payload)
            return jsonify(data), code
        return _handle_twilio_form()

    return app


def run_whatsapp_server() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = WhatsAppConfig.from_env()
    if not find_ffmpeg():
        logger.warning(
            "FFmpeg tidak ditemui! Voice note WhatsApp (.ogg/.opus) memerlukan FFmpeg atau `pip install imageio-ffmpeg`."
        )
    app = create_whatsapp_app(config)
    logger.info("CW Decoder WhatsApp Webhook berjalan di http://%s:%s", config.host, config.port)
    app.run(host=config.host, port=config.port, debug=config.debug)
