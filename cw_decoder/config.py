"""Configuration loader with zero-dependency .env parsing."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(env_path: str | Path = ".env") -> None:
    """Load key=value pairs from a .env file into os.environ (without overwriting)."""
    path = Path(env_path)
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class TelegramConfig:
    bot_token: str = ""
    poll_timeout: int = 30
    allowed_chat_ids: tuple[int, ...] = ()

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        load_dotenv()
        raw_ids = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
        allowed: list[int] = []
        if raw_ids:
            for item in raw_ids.split(","):
                item = item.strip()
                if item:
                    try:
                        allowed.append(int(item))
                    except ValueError:
                        pass
        return cls(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            poll_timeout=int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "30")),
            allowed_chat_ids=tuple(allowed),
        )


@dataclass
class WhatsAppConfig:
    provider: str = "auto"  # "meta", "twilio", or "auto"
    host: str = "0.0.0.0"
    port: int = 5000
    debug: bool = False
    # Meta WhatsApp Cloud API
    meta_verify_token: str = "cw-decoder-verify-token"
    meta_access_token: str = ""
    meta_phone_number_id: str = ""
    meta_api_version: str = "v21.0"
    # Twilio WhatsApp API
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_number: str = ""
    public_base_url: str = ""

    @classmethod
    def from_env(cls) -> "WhatsAppConfig":
        load_dotenv()
        port_raw = os.environ.get("PORT") or os.environ.get("WHATSAPP_PORT") or "5000"
        public_url = (
            os.environ.get("PUBLIC_BASE_URL")
            or os.environ.get("RENDER_EXTERNAL_URL")
            or ""
        )
        return cls(
            provider=os.environ.get("WHATSAPP_PROVIDER", "auto").strip().lower(),
            host=os.environ.get("WHATSAPP_HOST", "0.0.0.0").strip(),
            port=int(port_raw),
            debug=os.environ.get("WHATSAPP_DEBUG", "false").strip().lower() in ("1", "true", "yes"),
            meta_verify_token=os.environ.get("META_VERIFY_TOKEN", "cw-decoder-verify-token").strip(),
            meta_access_token=os.environ.get("META_ACCESS_TOKEN", "").strip(),
            meta_phone_number_id=os.environ.get("META_PHONE_NUMBER_ID", "").strip(),
            meta_api_version=os.environ.get("META_API_VERSION", "v21.0").strip(),
            twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID", "").strip(),
            twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN", "").strip(),
            twilio_whatsapp_number=os.environ.get("TWILIO_WHATSAPP_NUMBER", "").strip(),
            public_base_url=public_url.strip().rstrip("/"),
        )
