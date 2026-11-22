"""High-level CW Decoder orchestrating audio loading, DSP, timing, and Morse translation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio_io import load_audio
from .dsp import analyze_audio_dsp
from .morse import decode_morse
from .timing import segments_to_morse


@dataclass
class DecodeOptions:
    freq_hz: float | None = None
    wpm: float | None = None
    min_hz: float = 300.0
    max_hz: float = 2500.0
    bandwidth_hz: float = 120.0
    min_glitch_ms: float = 12.0
    use_prosigns: bool = True
    unknown_char: str = "?"


@dataclass
class DecodeResult:
    text: str
    morse: str
    wpm: float
    tone_hz: float
    dot_ms: float
    snr_db: float
    confidence: float
    duration_sec: float

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "morse": self.morse,
            "wpm": self.wpm,
            "tone_hz": round(self.tone_hz, 1),
            "dot_ms": self.dot_ms,
            "snr_db": round(self.snr_db, 1),
            "confidence": self.confidence,
            "duration_sec": round(self.duration_sec, 2),
        }


class CWDecoder:
    """Continuous Wave (Morse Code) audio decoder."""

    def __init__(self, options: DecodeOptions | None = None) -> None:
        self.options = options or DecodeOptions()

    def decode_samples(
        self,
        samples: np.ndarray,
        sample_rate: int,
        options: DecodeOptions | None = None,
    ) -> DecodeResult:
        opts = options or self.options

        dsp = analyze_audio_dsp(
            samples=samples,
            sample_rate=sample_rate,
            freq_hz=opts.freq_hz,
            min_hz=opts.min_hz,
            max_hz=opts.max_hz,
            bandwidth_hz=opts.bandwidth_hz,
            min_glitch_ms=opts.min_glitch_ms,
        )

        if not dsp.segments:
            return DecodeResult(
                text="",
                morse="",
                wpm=0.0,
                tone_hz=round(dsp.tone_hz, 1),
                dot_ms=0.0,
                snr_db=round(dsp.snr_db, 1),
                confidence=0.0,
                duration_sec=round(dsp.duration_sec, 2),
            )

        timing = segments_to_morse(dsp.segments, wpm_hint=opts.wpm)
        text = decode_morse(
            timing.morse,
            unknown_char=opts.unknown_char,
            use_prosigns=opts.use_prosigns,
        )

        # Penalize confidence slightly if unknown characters appeared
        unknown_ratio = text.count(opts.unknown_char) / max(len(text.replace(" ", "")), 1)
        final_conf = max(0.0, round(timing.confidence * (1.0 - 0.6 * unknown_ratio), 2))

        return DecodeResult(
            text=text,
            morse=timing.morse,
            wpm=timing.wpm,
            tone_hz=round(dsp.tone_hz, 1),
            dot_ms=timing.dot_ms,
            snr_db=round(dsp.snr_db, 1),
            confidence=final_conf,
            duration_sec=round(dsp.duration_sec, 2),
        )

    def decode_file(
        self,
        path: str | Path,
        options: DecodeOptions | None = None,
    ) -> DecodeResult:
        samples, sample_rate = load_audio(path)
        return self.decode_samples(samples, sample_rate, options=options)


def decode_samples(
    samples: np.ndarray,
    sample_rate: int,
    options: DecodeOptions | None = None,
) -> DecodeResult:
    """Convenience function to decode CW from NumPy audio samples."""
    return CWDecoder(options=options).decode_samples(samples, sample_rate)


def decode_file(path: str | Path, options: DecodeOptions | None = None) -> DecodeResult:
    """Convenience function to decode CW from an audio file."""
    return CWDecoder(options=options).decode_file(path)
