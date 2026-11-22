"""CW Audio Encoder: generates clean Morse code WAV audio from text for testing and bot replies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio_io import write_wav
from .morse import encode_morse


@dataclass
class EncodeOptions:
    wpm: float = 20.0
    farnsworth_wpm: float | None = None
    freq_hz: float = 700.0
    sample_rate: int = 16000
    amplitude: float = 0.75
    rise_ms: float = 5.0
    lead_silence_ms: float = 250.0
    trail_silence_ms: float = 250.0
    noise_level: float = 0.0
    seed: int | None = None


def _make_tone_pulse(
    duration_sec: float,
    freq_hz: float,
    sample_rate: int,
    amplitude: float,
    rise_ms: float,
) -> np.ndarray:
    """Generate a sine wave pulse with raised-cosine attack/release to prevent key clicks."""
    n = max(1, int(round(duration_sec * sample_rate)))
    t = np.arange(n, dtype=np.float64) / sample_rate
    wave = np.sin(2.0 * np.pi * freq_hz * t) * amplitude

    ramp_samples = min(n // 2, max(1, int(round((rise_ms / 1000.0) * sample_rate))))
    if ramp_samples > 1:
        ramp = 0.5 * (1.0 - np.cos(np.pi * np.arange(ramp_samples) / ramp_samples))
        wave[:ramp_samples] *= ramp
        wave[-ramp_samples:] *= ramp[::-1]

    return wave.astype(np.float32)


class CWEncoder:
    """Encodes plain text into Continuous Wave (Morse Code) audio."""

    def __init__(self, options: EncodeOptions | None = None) -> None:
        self.options = options or EncodeOptions()

    def encode_text_to_samples(
        self,
        text: str,
        options: EncodeOptions | None = None,
    ) -> tuple[np.ndarray, int, str]:
        opts = options or self.options
        morse = encode_morse(text)
        sr = int(opts.sample_rate)

        if not morse:
            return np.zeros(0, dtype=np.float32), sr, ""

        dot_sec = 1.2 / max(1.0, float(opts.wpm))

        # Farnsworth spacing slows down character & word gaps while keeping character speed crisp
        if opts.farnsworth_wpm and 0 < opts.farnsworth_wpm < opts.wpm:
            ta = (60.0 * opts.wpm - 37.2 * opts.farnsworth_wpm) / (opts.wpm * opts.farnsworth_wpm)
            char_gap_sec = 3.0 * ta / 19.0
            word_gap_sec = 7.0 * ta / 19.0
        else:
            char_gap_sec = 3.0 * dot_sec
            word_gap_sec = 7.0 * dot_sec

        dot_pulse = _make_tone_pulse(dot_sec, opts.freq_hz, sr, opts.amplitude, opts.rise_ms)
        dash_pulse = _make_tone_pulse(3.0 * dot_sec, opts.freq_hz, sr, opts.amplitude, opts.rise_ms)
        intra_gap = np.zeros(max(1, int(round(dot_sec * sr))), dtype=np.float32)
        char_gap = np.zeros(max(1, int(round(char_gap_sec * sr))), dtype=np.float32)
        word_gap = np.zeros(max(1, int(round(word_gap_sec * sr))), dtype=np.float32)

        chunks: list[np.ndarray] = []
        if opts.lead_silence_ms > 0:
            chunks.append(np.zeros(int(round(opts.lead_silence_ms * sr / 1000.0)), dtype=np.float32))

        words = morse.split(" / ")
        for w_idx, word in enumerate(words):
            chars = word.split()
            for c_idx, symbol in enumerate(chars):
                for s_idx, elem in enumerate(symbol):
                    if elem == ".":
                        chunks.append(dot_pulse)
                    elif elem == "-":
                        chunks.append(dash_pulse)
                    if s_idx < len(symbol) - 1:
                        chunks.append(intra_gap)
                if c_idx < len(chars) - 1:
                    chunks.append(char_gap)
            if w_idx < len(words) - 1:
                chunks.append(word_gap)

        if opts.trail_silence_ms > 0:
            chunks.append(np.zeros(int(round(opts.trail_silence_ms * sr / 1000.0)), dtype=np.float32))

        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

        if opts.noise_level > 0 and len(audio) > 0:
            rng = np.random.default_rng(opts.seed)
            noise = rng.normal(0.0, opts.noise_level, size=len(audio)).astype(np.float32)
            audio = np.clip(audio + noise, -1.0, 1.0)

        return audio, sr, morse

    def encode_text_to_wav(
        self,
        text: str,
        output_path: str | Path,
        options: EncodeOptions | None = None,
    ) -> tuple[Path, str]:
        audio, sr, morse = self.encode_text_to_samples(text, options=options)
        saved = write_wav(output_path, audio, sample_rate=sr)
        return saved, morse


def encode_text_to_samples(
    text: str,
    options: EncodeOptions | None = None,
) -> tuple[np.ndarray, int, str]:
    return CWEncoder(options=options).encode_text_to_samples(text)


def encode_text_to_wav(
    text: str,
    output_path: str | Path,
    options: EncodeOptions | None = None,
) -> tuple[Path, str]:
    return CWEncoder(options=options).encode_text_to_wav(text, output_path)
