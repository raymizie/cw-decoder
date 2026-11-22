"""Tests for CW Encoder, DSP tone/envelope detection, timing estimator, and CWDecoder."""

from pathlib import Path

import numpy as np
import pytest

from cw_decoder import CWDecoder, CWEncoder, DecodeOptions, EncodeOptions


@pytest.mark.parametrize(
    ("message", "wpm", "freq_hz"),
    [
        ("HELLO WORLD", 15.0, 600.0),
        ("CQ CQ DE 9M2XYZ K", 20.0, 750.0),
        ("CW DECODER 2026", 28.0, 1100.0),
        ("SOS TEST 12345", 10.0, 480.0),
        ("FAST MORSE CHECK", 36.0, 1650.0),
    ],
)
def test_clean_roundtrip_auto_detect(message: str, wpm: float, freq_hz: float):
    enc = CWEncoder(EncodeOptions(wpm=wpm, freq_hz=freq_hz, sample_rate=16000))
    samples, sr, expected_morse = enc.encode_text_to_samples(message)

    dec = CWDecoder()
    res = dec.decode_samples(samples, sr)

    assert res.text == message
    assert res.morse == expected_morse
    assert abs(res.tone_hz - freq_hz) <= 15.0
    assert abs(res.wpm - wpm) <= max(2.0, wpm * 0.12)
    assert res.confidence >= 0.75


def test_noisy_signal_decoding():
    message = "SELAMAT DATANG CW"
    enc = CWEncoder(
        EncodeOptions(
            wpm=20.0,
            freq_hz=820.0,
            sample_rate=16000,
            amplitude=0.7,
            noise_level=0.22,
            seed=42,
        )
    )
    samples, sr, _ = enc.encode_text_to_samples(message)
    res = CWDecoder().decode_samples(samples, sr)

    assert res.text == message
    assert abs(res.tone_hz - 820.0) <= 20.0
    assert res.snr_db > 6.0


def test_farnsworth_spacing_decoding():
    message = "MORSE CODE TEST"
    enc = CWEncoder(
        EncodeOptions(
            wpm=22.0,
            farnsworth_wpm=12.0,
            freq_hz=700.0,
            sample_rate=16000,
        )
    )
    samples, sr, _ = enc.encode_text_to_samples(message)
    res = CWDecoder().decode_samples(samples, sr)

    assert res.text == message


def test_single_element_messages_dots_and_dashes():
    dec = CWDecoder()

    # All dots
    s_dots, sr, _ = CWEncoder(EncodeOptions(wpm=20.0, freq_hz=700.0)).encode_text_to_samples("SSS")
    assert dec.decode_samples(s_dots, sr).text == "SSS"

    # All dashes
    s_dashes, sr, _ = CWEncoder(EncodeOptions(wpm=20.0, freq_hz=700.0)).encode_text_to_samples("OOO")
    assert dec.decode_samples(s_dashes, sr).text == "OOO"


def test_prosign_audio_roundtrip(tmp_path: Path):
    wav_file = tmp_path / "prosign.wav"
    enc = CWEncoder(EncodeOptions(wpm=18.0, freq_hz=680.0))
    enc.encode_text_to_wav("CQ DE 9M2CW <SK>", wav_file)

    res = CWDecoder().decode_file(wav_file)
    assert res.text == "CQ DE 9M2CW <SK>"


def test_high_sample_rate_decimation():
    enc = CWEncoder(EncodeOptions(wpm=20.0, freq_hz=700.0, sample_rate=48000))
    samples, sr, _ = enc.encode_text_to_samples("AUDIO 48KHZ")
    res = CWDecoder().decode_samples(samples, sr)
    assert res.text == "AUDIO 48KHZ"


def test_silence_and_pure_noise_return_empty():
    dec = CWDecoder()
    silence = np.zeros(16000 * 2, dtype=np.float32)
    res_silent = dec.decode_samples(silence, 16000)
    assert res_silent.is_empty

    rng = np.random.default_rng(123)
    noise = rng.normal(0.0, 0.25, size=16000 * 2).astype(np.float32)
    res_noise = dec.decode_samples(noise, 16000)
    assert res_noise.is_empty


def test_manual_freq_and_wpm_override():
    enc = CWEncoder(EncodeOptions(wpm=20.0, freq_hz=900.0))
    samples, sr, _ = enc.encode_text_to_samples("TEST OVERRIDE")
    res = CWDecoder(DecodeOptions(freq_hz=900.0, wpm=20.0)).decode_samples(samples, sr)
    assert res.text == "TEST OVERRIDE"
    assert res.tone_hz == 900.0
    assert res.wpm == 20.0
