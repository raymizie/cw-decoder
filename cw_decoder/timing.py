"""Adaptive CW timing analyzer: estimates WPM/dot unit and converts mark/space segments to Morse."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class TimingResult:
    morse: str
    dot_ms: float
    wpm: float
    dot_dash_threshold_ms: float
    char_gap_threshold_ms: float
    word_gap_threshold_ms: float
    confidence: float


def _kmeans_1d_two(values: np.ndarray) -> tuple[float, float]:
    """Simple 1D 2-means clustering returning (low_centroid, high_centroid)."""
    c1 = float(np.percentile(values, 20))
    c2 = float(np.percentile(values, 85))
    if c2 <= c1:
        return c1, c2

    for _ in range(25):
        mid = 0.5 * (c1 + c2)
        g1 = values[values <= mid]
        g2 = values[values > mid]
        if len(g1) == 0 or len(g2) == 0:
            break
        n1 = float(np.median(g1))
        n2 = float(np.median(g2))
        if abs(n1 - c1) < 1e-3 and abs(n2 - c2) < 1e-3:
            c1, c2 = n1, n2
            break
        c1, c2 = n1, n2

    return min(c1, c2), max(c1, c2)


def _trim_outer_silences(segments: list[tuple[bool, float]]) -> list[tuple[bool, float]]:
    """Strip leading and trailing space segments."""
    start = 0
    while start < len(segments) and not segments[start][0]:
        start += 1
    end = len(segments)
    while end > start and not segments[end - 1][0]:
        end -= 1
    return segments[start:end]


def segments_to_morse(
    segments: list[tuple[bool, float]],
    wpm_hint: float | None = None,
) -> TimingResult:
    """Convert (is_mark, duration_ms) segments into a Morse string with adaptive WPM estimation."""
    trimmed = _trim_outer_silences(segments)
    marks = np.array([dur for is_mark, dur in trimmed if is_mark and dur >= 15.0], dtype=np.float64)
    spaces = np.array([dur for is_mark, dur in trimmed if not is_mark and dur >= 15.0], dtype=np.float64)

    if len(marks) == 0:
        return TimingResult(
            morse="",
            dot_ms=0.0,
            wpm=0.0,
            dot_dash_threshold_ms=0.0,
            char_gap_threshold_ms=0.0,
            word_gap_threshold_ms=0.0,
            confidence=0.0,
        )

    confidence = 0.7

    if wpm_hint is not None and wpm_hint > 0:
        dot_ms = 1200.0 / float(wpm_hint)
        dot_dash_thresh = 2.0 * dot_ms
        confidence = 0.9
    else:
        # Cluster marks in log-space for scale invariance (handles 5-50 WPM cleanly)
        log_marks = np.log(marks)
        c1_log, c2_log = _kmeans_1d_two(log_marks)
        c_short = float(np.exp(c1_log))
        c_long = float(np.exp(c2_log))
        ratio = c_long / max(c_short, 1e-6)

        if len(marks) >= 2 and 1.85 <= ratio <= 5.2:
            # Clear bimodal distribution: dots and dashes both present
            dot_ms = 0.5 * (c_short + c_long / 3.0)
            dot_dash_thresh = float(np.sqrt(c_short * c_long))
            # Confidence higher when ratio is close to ideal 3.0
            ratio_err = abs(ratio - 3.0) / 3.0
            confidence = float(np.clip(1.0 - 0.5 * ratio_err, 0.55, 0.99))
        else:
            # Only dots OR only dashes (e.g., "EEE", "SSS", "TTT", "OOO")
            med_mark = float(np.median(marks))
            if len(spaces) > 0:
                min_space = float(np.percentile(spaces, 20))
                # If marks are ~3x the smallest intra-symbol gap, they are dashes!
                if med_mark / max(min_space, 1.0) >= 2.1:
                    dot_ms = med_mark / 3.0
                else:
                    dot_ms = med_mark
            else:
                # Single lone mark: assume dot if <= 140 ms, else dash
                dot_ms = med_mark if med_mark <= 140.0 else med_mark / 3.0
            dot_dash_thresh = 2.0 * dot_ms
            confidence = 0.65

    # Clamp dot_ms to sane CW range (4 WPM = 300ms .. 60 WPM = 20ms)
    dot_ms = float(np.clip(dot_ms, 20.0, 350.0))
    wpm = 1200.0 / dot_ms

    # Adaptive gap thresholds (handles standard ITU and Farnsworth spacing)
    char_gap_thresh = 2.0 * dot_ms
    word_gap_thresh = 5.0 * dot_ms

    if len(spaces) >= 3:
        # Find inter-character spaces (anything clearly > 1.85 * dot_ms)
        long_spaces = spaces[spaces > 1.85 * dot_ms]
        short_spaces = spaces[spaces <= 1.85 * dot_ms]

        if len(short_spaces) > 0 and len(long_spaces) > 0:
            med_intra = float(np.median(short_spaces))
            min_inter = float(np.percentile(long_spaces, 15))
            # Geometric mean between intra-char gap and smallest inter-char gap
            char_gap_thresh = float(np.clip(np.sqrt(med_intra * min_inter), 1.75 * dot_ms, 2.5 * dot_ms))

        # Check if long_spaces split into char gaps and word gaps (including Farnsworth)
        if len(long_spaces) >= 2:
            s1_log, s2_log = _kmeans_1d_two(np.log(long_spaces))
            c_char = float(np.exp(s1_log))
            c_word = float(np.exp(s2_log))
            if c_word / max(c_char, 1.0) >= 1.75 and c_word >= 4.5 * dot_ms:
                word_gap_thresh = float(np.sqrt(c_char * c_word))
            else:
                # No distinct word gap cluster; keep threshold safe against Farnsworth char gaps
                med_long = float(np.median(long_spaces))
                if med_long <= 4.5 * dot_ms:
                    word_gap_thresh = max(5.0 * dot_ms, 1.7 * med_long)
                else:
                    # Could be all word gaps (single-letter words) or wide Farnsworth
                    word_gap_thresh = 5.0 * dot_ms

    # Build Morse string from segments
    tokens: list[str] = []
    current_char: list[str] = []

    for is_mark, dur_ms in trimmed:
        if is_mark:
            if dur_ms < 0.25 * dot_ms:
                continue
            current_char.append("-" if dur_ms >= dot_dash_thresh else ".")
        else:
            if dur_ms >= word_gap_thresh:
                if current_char:
                    tokens.append("".join(current_char))
                    current_char = []
                if tokens and tokens[-1] != "/":
                    tokens.append("/")
            elif dur_ms >= char_gap_thresh:
                if current_char:
                    tokens.append("".join(current_char))
                    current_char = []

    if current_char:
        tokens.append("".join(current_char))

    # Strip trailing '/' if any
    while tokens and tokens[-1] == "/":
        tokens.pop()

    morse_str = " ".join(tokens)

    return TimingResult(
        morse=morse_str,
        dot_ms=round(dot_ms, 2),
        wpm=round(wpm, 1),
        dot_dash_threshold_ms=round(dot_dash_thresh, 2),
        char_gap_threshold_ms=round(char_gap_thresh, 2),
        word_gap_threshold_ms=round(word_gap_thresh, 2),
        confidence=round(confidence, 2),
    )
