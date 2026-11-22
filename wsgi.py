"""WSGI entrypoint for Render / Gunicorn deployment of CW Decoder WhatsApp Webhook."""

from cw_decoder.bots.whatsapp_bot import create_whatsapp_app

app = create_whatsapp_app()
