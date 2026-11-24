"""Tests for Telegram Bot and WhatsApp Webhook Bot (Meta Cloud API & Twilio)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cw_decoder.bots.formatting import EncodeCommand, handle_simple_text_command, parse_encode_command
from cw_decoder.bots.telegram_bot import TelegramCWBot
from cw_decoder.bots.whatsapp_bot import create_whatsapp_app
from cw_decoder.config import TelegramConfig, WhatsAppConfig
from cw_decoder.encoder import CWEncoder, EncodeOptions


class _FakeResponse:
    def __init__(self, json_data: Any = None, content: bytes = b"", status_code: int = 200) -> None:
        self._json = json_data
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json


class _MockSession:
    """Mock requests.Session capturing outgoing API calls and serving fake audio downloads."""

    def __init__(self, wav_bytes: bytes = b"") -> None:
        self.wav_bytes = wav_bytes
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("GET", url, kwargs))
        if "graph.facebook.com" in url and not url.endswith("/download"):
            return _FakeResponse({"url": "https://cdn.example/download", "mime_type": "audio/wav"})
        return _FakeResponse(content=self.wav_bytes)

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/getFile"):
            return _FakeResponse({"ok": True, "result": {"file_path": "voice/test.wav"}})
        if url.endswith("/media"):
            return _FakeResponse({"id": "MEDIA_999"})
        return _FakeResponse({"ok": True, "result": {"message_id": 1}})


def _make_sample_wav_bytes(tmp_path: Path, text: str) -> bytes:
    wav_path = tmp_path / "cw.wav"
    CWEncoder(EncodeOptions(wpm=20.0, freq_hz=720.0)).encode_text_to_wav(text, wav_path)
    return wav_path.read_bytes()


def test_telegram_bot_audio_and_commands(tmp_path: Path):
    wav_bytes = _make_sample_wav_bytes(tmp_path, "TELEGRAM OK")
    mock_session = _MockSession(wav_bytes=wav_bytes)
    bot = TelegramCWBot(TelegramConfig(bot_token="123:TEST"), session=mock_session)  # type: ignore[arg-type]

    # 1. Text /morse command
    bot.process_update(
        {"update_id": 1, "message": {"message_id": 10, "chat": {"id": 555}, "text": "/morse ... --- ..."}}
    )
    msg_calls = [c for c in mock_session.calls if c[1].endswith("/sendMessage")]
    assert len(msg_calls) == 1
    assert "SOS" in msg_calls[0][2]["json"]["text"]

    # 2. Voice/document WAV decode
    bot.process_update(
        {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "chat": {"id": 555},
                "document": {"file_id": "FILE_1", "mime_type": "audio/wav", "file_name": "test.wav"},
            },
        }
    )
    msg_calls = [c for c in mock_session.calls if c[1].endswith("/sendMessage")]
    assert len(msg_calls) == 2
    assert "TELEGRAM OK" in msg_calls[1][2]["json"]["text"]

    # 3. /encode sends audio
    bot.process_update(
        {"update_id": 3, "message": {"message_id": 12, "chat": {"id": 555}, "text": "/encode --wpm 18 HI"}}
    )
    audio_calls = [c for c in mock_session.calls if c[1].endswith("/sendAudio")]
    assert len(audio_calls) == 1
    assert "HI" in audio_calls[0][2]["data"]["caption"]


def test_whatsapp_meta_verify_and_audio_decode(tmp_path: Path):
    wav_bytes = _make_sample_wav_bytes(tmp_path, "META CW")
    mock_session = _MockSession(wav_bytes=wav_bytes)
    cfg = WhatsAppConfig(
        meta_verify_token="secret-token",
        meta_access_token="ACCESS_123",
        meta_phone_number_id="PHONE_456",
    )
    app = create_whatsapp_app(config=cfg, session=mock_session)  # type: ignore[arg-type]
    client = app.test_client()

    # Index & Health check
    root = client.get("/")
    assert root.status_code == 200
    assert root.get_json()["status"] == "online"

    health = client.get("/health")
    assert health.status_code == 200
    assert health.get_json()["status"] == "ok"

    # Webhook verification (valid & invalid)
    ok = client.get("/webhook/meta?hub.mode=subscribe&hub.verify_token=secret-token&hub.challenge=CHAL99")
    assert ok.status_code == 200
    assert ok.data.decode() == "CHAL99"

    bad = client.get("/webhook/meta?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=CHAL99")
    assert bad.status_code == 403

    # Incoming audio message
    meta_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "60123456789",
                                    "type": "audio",
                                    "audio": {"id": "MEDIA_1", "mime_type": "audio/wav"},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    resp = client.post("/webhook/meta", json=meta_payload)
    assert resp.status_code == 200
    assert resp.get_json()["processed"] == 1

    sent = [c for c in mock_session.calls if c[0] == "POST" and c[1].endswith("/PHONE_456/messages")]
    assert len(sent) == 1
    assert "META CW" in sent[0][2]["json"]["text"]["body"]


def test_whatsapp_twilio_audio_and_encode(tmp_path: Path):
    wav_bytes = _make_sample_wav_bytes(tmp_path, "TWILIO OK")
    mock_session = _MockSession(wav_bytes=wav_bytes)
    cfg = WhatsAppConfig(
        twilio_account_sid="AC123",
        twilio_auth_token="AUTH123",
        public_base_url="https://cw.example.com",
    )
    app = create_whatsapp_app(config=cfg, session=mock_session)  # type: ignore[arg-type]
    client = app.test_client()

    # Incoming Twilio audio
    resp = client.post(
        "/webhook/twilio",
        data={
            "From": "whatsapp:+60123456789",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/media/1",
            "MediaContentType0": "audio/wav",
        },
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/xml"
    xml_text = resp.data.decode()
    assert "TWILIO OK" in xml_text

    # Incoming /encode generates media URL and serves WAV
    enc_resp = client.post(
        "/webhook/twilio",
        data={"From": "whatsapp:+60123456789", "NumMedia": "0", "Body": "/encode TEST"},
    )
    assert enc_resp.status_code == 200
    enc_xml = enc_resp.data.decode()
    assert "<Media>https://cw.example.com/media/" in enc_xml

    # Extract token and test /media/<token>.wav route
    start = enc_xml.index("/media/")
    end = enc_xml.index(".wav", start) + 4
    media_route = enc_xml[start:end]
    media_resp = client.get(media_route)
    assert media_resp.status_code == 200
    assert media_resp.data[:4] == b"RIFF"


def test_parse_encode_command_with_reply_fallback():
    # Plain reply `/encode`
    action, payload = handle_simple_text_command("/encode", reply_text="SELAMAT PAGI")
    assert action == "encode"
    assert isinstance(payload, EncodeCommand)
    assert payload.text == "SELAMAT PAGI"
    assert payload.wpm == 20.0
    assert payload.freq_hz == 700.0

    # Reply `/encode` with custom options only
    action, payload = handle_simple_text_command("/encode --wpm 15 --freq 600", reply_text="CQ\n  DE 9M2CW")
    assert action == "encode"
    assert isinstance(payload, EncodeCommand)
    assert payload.text == "CQ DE 9M2CW"
    assert payload.wpm == 15.0
    assert payload.freq_hz == 600.0

    # Explicit inline text overrides replied text
    parsed = parse_encode_command("--wpm 18 OVERRIDE", fallback_text="IGNORED")
    assert parsed == EncodeCommand(text="OVERRIDE", wpm=18.0, freq_hz=700.0)

    # No inline text and no reply -> helpful usage prompt
    action, msg = handle_simple_text_command("/encode")
    assert action == "reply"
    assert isinstance(msg, str)
    assert "Reply" in msg


def test_telegram_bot_reply_encode():
    mock_session = _MockSession()
    bot = TelegramCWBot(TelegramConfig(bot_token="123:TEST"), session=mock_session)  # type: ignore[arg-type]

    # 1. Reply `/encode` to a text message
    bot.process_update(
        {
            "update_id": 10,
            "message": {
                "message_id": 20,
                "chat": {"id": 555},
                "text": "/encode",
                "reply_to_message": {"message_id": 19, "text": "HELLO CW"},
            },
        }
    )
    audio_calls = [c for c in mock_session.calls if c[1].endswith("/sendAudio")]
    assert len(audio_calls) == 1
    assert "HELLO CW" in audio_calls[0][2]["data"]["caption"]

    # 2. Reply `/encode --wpm 16 --freq 650` to a captioned message
    bot.process_update(
        {
            "update_id": 11,
            "message": {
                "message_id": 21,
                "chat": {"id": 555},
                "text": "/encode --wpm 16 --freq 650",
                "reply_to_message": {"message_id": 18, "caption": "TEST CAPTION"},
            },
        }
    )
    audio_calls = [c for c in mock_session.calls if c[1].endswith("/sendAudio")]
    assert len(audio_calls) == 2
    assert "TEST CAPTION" in audio_calls[1][2]["data"]["caption"]
    assert "16 WPM @ 650 Hz" in audio_calls[1][2]["data"]["caption"]

    # 3. Reply `/encode` to emoji-only text returns warning instead of empty audio
    bot.process_update(
        {
            "update_id": 12,
            "message": {
                "message_id": 22,
                "chat": {"id": 555},
                "text": "/encode",
                "reply_to_message": {"message_id": 17, "text": "😀😀"},
            },
        }
    )
    audio_calls = [c for c in mock_session.calls if c[1].endswith("/sendAudio")]
    assert len(audio_calls) == 2
    msg_calls = [c for c in mock_session.calls if c[1].endswith("/sendMessage")]
    assert len(msg_calls) == 1
    assert "Tiada aksara" in msg_calls[0][2]["json"]["text"]


def test_whatsapp_meta_reply_encode():
    mock_session = _MockSession()
    cfg = WhatsAppConfig(
        meta_verify_token="secret-token",
        meta_access_token="ACCESS_123",
        meta_phone_number_id="PHONE_456",
    )
    app = create_whatsapp_app(config=cfg, session=mock_session)  # type: ignore[arg-type]
    client = app.test_client()

    # Step 1: Original text message arrives and is remembered by ID
    first_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.ORIG_1",
                                    "from": "60123456789",
                                    "type": "text",
                                    "text": {"body": "SALAM 73"},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    assert client.post("/webhook/meta", json=first_payload).status_code == 200

    # Step 2: User replies with `/encode --wpm 15` referencing original message ID
    reply_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.REPLY_2",
                                    "from": "60123456789",
                                    "type": "text",
                                    "context": {"id": "wamid.ORIG_1"},
                                    "text": {"body": "/encode --wpm 15"},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }
    resp = client.post("/webhook/meta", json=reply_payload)
    assert resp.status_code == 200

    media_uploads = [c for c in mock_session.calls if c[0] == "POST" and c[1].endswith("/media")]
    assert len(media_uploads) == 1
    sent_texts = [
        c[2]["json"]["text"]["body"]
        for c in mock_session.calls
        if c[0] == "POST" and c[1].endswith("/PHONE_456/messages") and c[2]["json"].get("type") == "text"
    ]
    assert any("SALAM 73" in t and "15 WPM" in t for t in sent_texts)
