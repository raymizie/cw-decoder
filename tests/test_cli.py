"""Tests for the CW Decoder CLI."""

import json
from pathlib import Path

from cw_decoder.cli import main


def test_cli_encode_and_decode_json(tmp_path: Path, capsys):
    wav_path = tmp_path / "sample.wav"

    rc_enc = main(["encode", "HELLO", "CW", "-o", str(wav_path), "--wpm", "20", "--freq", "750"])
    assert rc_enc == 0
    assert wav_path.is_file()
    capsys.readouterr()

    rc_dec = main(["decode", str(wav_path), "--json"])
    assert rc_dec == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["text"] == "HELLO CW"
    assert abs(data["tone_hz"] - 750.0) <= 15.0


def test_cli_morse_subcommand(capsys):
    assert main(["morse", "--encode", "HI", "CW"]) == 0
    assert capsys.readouterr().out.strip() == ".... .. / -.-. .--"

    assert main(["morse", ".... .. / -.-. .--"]) == 0
    assert capsys.readouterr().out.strip() == "HI CW"


def test_cli_doctor(capsys):
    assert main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "CW Decoder" in out
    assert "FFmpeg" in out
