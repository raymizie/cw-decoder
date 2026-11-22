#!/usr/bin/env python3
"""Entrypoint Server Webhook WhatsApp (Flask: Meta Cloud API & Twilio) untuk CW Decoder."""

from cw_decoder.bots.whatsapp_bot import run_whatsapp_server

if __name__ == "__main__":
    run_whatsapp_server()
