"""Pure-NumPy DSP pipeline for CW tone detection, I/Q demodulation, and pulse extraction."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class DSPAnalysis:
    tone_hz: float
    snr_db: float
    envelope: np.ndarray
    envelope_rate: float
    threshold_low: float
    threshold_high: float
    segments: list[tuple[bool, float]]  # (is_mark, duration_ms)
    duration_sec: float


def _fft_convolve_same(signal: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    """Fast 1D convolution with 'same' output size using FFT (supports complex signal)."""
    n = len(signal)
    m = len(kernel)
    if n == 0 or m == 0:
        return signal.copy()

    fft_len = 1 << (n + m - 1).bit_length()
    if np.iscomplexobj(signal):
        out = np.fft.ifft(np.fft.fft(signal, fft_len) * np.fft.fft(kernel, fft_len))
    else:
        out = np.fft.irfft(np.fft.rfft(signal, fft_len) * np.fft.rfft(kernel, fft_len), fft_len)

    start = (m - 1) // 2
    return out[start : start + n]


def _decimate_if_needed(samples: np.ndarray, sample_rate: int, max_sr: int = 16000) -> tuple[np.ndarray, int]:
    """Integer-factor anti-aliased decimation when sample rate is higher than needed."""
    if sample_rate <= max_sr:
        return samples.astype(np.float64, copy=False), sample_rate

    factor = max(1, sample_rate // max_sr)
    if factor == 1:
        return samples.astype(np.float64, copy=False), sample_rate

    # Low-pass FIR filter before decimation (cutoff at 0.4 * new_sr)
    taps = factor * 8 + 1
    cutoff = 0.42 / factor
    n = np.arange(taps) - (taps - 1) / 2.0
    kernel = 2.0 * cutoff * np.sinc(2.0 * cutoff * n) * np.blackman(taps)
    kernel /= np.sum(kernel)

    filtered = _fft_convolve_same(samples.astype(np.float64), kernel)
    return filtered[::factor], sample_rate // factor


def detect_tone_frequency(
    samples: np.ndarray,
    sample_rate: int,
    min_hz: float = 300.0,
    max_hz: float = 2500.0,
) -> tuple[float, float]:
    """Auto-detect the dominant CW tone frequency (Hz) and estimated narrowband SNR (dB).

    Uses windowed STFT peak-prominence accumulation so long silences or broadband noise
    do not drown out intermittent Morse beeps.
    """
    n_samples = len(samples)
    if n_samples == 0:
        return 700.0, 0.0

    # Choose FFT size ~32-48 ms for good frequency resolution (~20-30 Hz bins)
    target_win = max(256, int(sample_rate * 0.04))
    win_size = 1 << (target_win - 1).bit_length()
    if win_size > n_samples:
        win_size = 1 << max(6, n_samples.bit_length() - 1)

    hop = max(64, win_size // 4)
    window = np.hanning(win_size)
    freqs = np.fft.rfftfreq(win_size, d=1.0 / sample_rate)

    nyquist = sample_rate * 0.48
    hi_hz = min(max_hz, nyquist)
    lo_hz = min(min_hz, hi_hz * 0.5)
    band_mask = (freqs >= lo_hz) & (freqs <= hi_hz)
    if not np.any(band_mask):
        return 700.0, 0.0

    band_indices = np.nonzero(band_mask)[0]

    # Build frames
    if n_samples <= win_size:
        frame = np.zeros(win_size, dtype=np.float64)
        frame[:n_samples] = samples
        frames = frame[np.newaxis, :]
    else:
        n_frames = 1 + (n_samples - win_size) // hop
        starts = np.arange(n_frames) * hop
        idx = starts[:, np.newaxis] + np.arange(win_size)[np.newaxis, :]
        frames = samples[idx]

    spectra = np.abs(np.fft.rfft(frames * window[np.newaxis, :], axis=1)) ** 2
    band_power = spectra[:, band_indices]

    # Weight frames by their spectral tonality (peak / median in band)
    frame_peaks = np.max(band_power, axis=1)
    frame_medians = np.median(band_power, axis=1) + 1e-18
    tonality = frame_peaks / frame_medians

    # Use top 25% most tonal frames (where the CW key is likely down)
    k = max(1, int(len(tonality) * 0.25))
    top_idx = np.argpartition(tonality, -k)[-k:]
    weights = np.maximum(tonality[top_idx] - 1.0, 0.0)
    if np.sum(weights) <= 1e-12:
        agg_spectrum = np.mean(band_power, axis=0)
    else:
        agg_spectrum = np.average(band_power[top_idx], axis=0, weights=weights)

    peak_local = int(np.argmax(agg_spectrum))
    peak_bin = int(band_indices[peak_local])

    # Parabolic interpolation around peak bin for sub-bin accuracy
    if 0 < peak_local < len(agg_spectrum) - 1:
        y0 = np.log(agg_spectrum[peak_local - 1] + 1e-18)
        y1 = np.log(agg_spectrum[peak_local] + 1e-18)
        y2 = np.log(agg_spectrum[peak_local + 1] + 1e-18)
        denom = y0 - 2.0 * y1 + y2
        delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-12 else 0.0
        delta = float(np.clip(delta, -0.5, 0.5))
    else:
        delta = 0.0

    bin_hz = sample_rate / win_size
    tone_hz = float((peak_bin + delta) * bin_hz)

    # Estimate narrowband SNR (peak power vs median floor outside +/- 100 Hz of peak)
    band_freqs = freqs[band_indices]
    noise_mask = np.abs(band_freqs - tone_hz) > max(80.0, 2.5 * bin_hz)
    if np.any(noise_mask):
        noise_floor = float(np.median(agg_spectrum[noise_mask])) + 1e-18
    else:
        noise_floor = float(np.median(agg_spectrum)) + 1e-18

    peak_power = float(agg_spectrum[peak_local]) + 1e-18
    snr_db = float(10.0 * np.log10(peak_power / noise_floor))

    return tone_hz, snr_db


def extract_envelope(
    samples: np.ndarray,
    sample_rate: int,
    tone_hz: float,
    bandwidth_hz: float = 120.0,
    target_env_rate: float = 500.0,
) -> tuple[np.ndarray, float]:
    """Extract narrowband CW amplitude envelope via quadrature (I/Q) demodulation."""
    n = len(samples)
    if n == 0:
        return np.zeros(0, dtype=np.float64), target_env_rate

    t = np.arange(n, dtype=np.float64) / sample_rate
    iq = samples * np.exp(-2j * np.pi * tone_hz * t)

    # Low-pass FIR filter (Blackman-windowed sinc)
    # Cutoff = half bandwidth (one-sided around DC after mixing)
    cutoff_hz = max(30.0, min(bandwidth_hz * 0.65, sample_rate * 0.25))
    # Kernel duration ~ 1.6 / cutoff_hz gives clean selectivity without smearing 40 WPM dots (30ms)
    taps = int(round(sample_rate * min(0.014, max(0.006, 1.2 / cutoff_hz))))
    if taps % 2 == 0:
        taps += 1
    taps = max(11, taps)

    norm_cut = cutoff_hz / sample_rate
    k = np.arange(taps) - (taps - 1) / 2.0
    kernel = 2.0 * norm_cut * np.sinc(2.0 * norm_cut * k) * np.blackman(taps)
    kernel /= np.sum(kernel)

    filtered_iq = _fft_convolve_same(iq, kernel)
    env = np.abs(filtered_iq)

    # Downsample envelope to ~500 Hz (2 ms per sample) for fast, crisp pulse detection
    step = max(1, int(round(sample_rate / target_env_rate)))
    if step > 1:
        n_blocks = len(env) // step
        if n_blocks > 0:
            env = env[: n_blocks * step].reshape(n_blocks, step).mean(axis=1)
        env_rate = sample_rate / step
    else:
        env_rate = float(sample_rate)

    # Normalize envelope to [0, 1]
    peak = float(np.percentile(env, 99.5)) if len(env) > 0 else 0.0
    if peak > 1e-12:
        env = np.clip(env / peak, 0.0, 1.5)

    return env, env_rate


def _compute_hysteresis_thresholds(env: np.ndarray) -> tuple[float, float, bool]:
    """Compute adaptive low/high Schmitt-trigger thresholds using 1D 2-means clustering.

    Returns (thresh_low, thresh_high, has_signal).
    """
    if len(env) < 10:
        return 0.35, 0.55, False

    p_max = float(np.max(env))
    if p_max < 1e-6:
        return 0.35, 0.55, False

    # Trim long leading/trailing silence before clustering so silence doesn't skew centroids
    rough_floor = float(np.percentile(env, 15))
    rough_peak = float(np.percentile(env, 98))
    if rough_peak - rough_floor < 0.08:
        return 0.35, 0.55, False

    active = np.nonzero(env > rough_floor + 0.25 * (rough_peak - rough_floor))[0]
    if len(active) == 0:
        return 0.35, 0.55, False

    pad = min(25, len(env) // 10)
    lo_idx = max(0, int(active[0]) - pad)
    hi_idx = min(len(env), int(active[-1]) + pad + 1)
    roi = env[lo_idx:hi_idx]

    c_low = float(np.percentile(roi, 15))
    c_high = float(np.percentile(roi, 90))

    for _ in range(15):
        mid = 0.5 * (c_low + c_high)
        low_vals = roi[roi <= mid]
        high_vals = roi[roi > mid]
        if len(low_vals) == 0 or len(high_vals) == 0:
            break
        new_low = float(np.mean(low_vals))
        new_high = float(np.mean(high_vals))
        if abs(new_low - c_low) < 1e-4 and abs(new_high - c_high) < 1e-4:
            c_low, c_high = new_low, new_high
            break
        c_low, c_high = new_low, new_high

    span = c_high - c_low
    if span < 0.12 or c_high < 0.15:
        return 0.35, 0.55, False

    thresh_low = c_low + 0.38 * span
    thresh_high = c_low + 0.58 * span
    return thresh_low, thresh_high, True


def _extract_segments_with_hysteresis(
    env: np.ndarray,
    env_rate: float,
    thresh_low: float,
    thresh_high: float,
    min_glitch_ms: float = 12.0,
) -> list[tuple[bool, float]]:
    """Run Schmitt trigger on envelope and de-glitch tiny spikes/dropouts."""
    if len(env) == 0:
        return []

    state = False
    binary = np.empty(len(env), dtype=bool)
    for i, val in enumerate(env):
        if not state and val >= thresh_high:
            state = True
        elif state and val <= thresh_low:
            state = False
        binary[i] = state

    # Run-length encode
    changes = np.nonzero(binary[1:] != binary[:-1])[0] + 1
    starts = np.concatenate(([0], changes))
    ends = np.concatenate((changes, [len(binary)]))

    ms_per_sample = 1000.0 / env_rate
    raw_segments: list[tuple[bool, float]] = [
        (bool(binary[s]), float((e - s) * ms_per_sample)) for s, e in zip(starts, ends)
    ]

    # De-glitch: absorb tiny spikes/dropouts shorter than min_glitch_ms into neighbors
    cleaned: list[tuple[bool, float]] = []
    for is_mark, dur_ms in raw_segments:
        if dur_ms < min_glitch_ms and cleaned:
            prev_mark, prev_dur = cleaned[-1]
            cleaned[-1] = (prev_mark, prev_dur + dur_ms)
        elif dur_ms < min_glitch_ms and not cleaned:
            # Tiny leading artifact: ignore
            continue
        else:
            if cleaned and cleaned[-1][0] == is_mark:
                prev_mark, prev_dur = cleaned[-1]
                cleaned[-1] = (prev_mark, prev_dur + dur_ms)
            else:
                cleaned.append((is_mark, dur_ms))

    return cleaned


def analyze_audio_dsp(
    samples: np.ndarray,
    sample_rate: int,
    freq_hz: float | None = None,
    min_hz: float = 300.0,
    max_hz: float = 2500.0,
    bandwidth_hz: float = 120.0,
    min_glitch_ms: float = 12.0,
) -> DSPAnalysis:
    """Run full DSP analysis: decimate -> tone detect -> I/Q envelope -> hysteresis segments."""
    if len(samples) == 0 or sample_rate <= 0:
        return DSPAnalysis(
            tone_hz=freq_hz or 700.0,
            snr_db=0.0,
            envelope=np.zeros(0, dtype=np.float64),
            envelope_rate=500.0,
            threshold_low=0.35,
            threshold_high=0.55,
            segments=[],
            duration_sec=0.0,
        )

    duration_sec = len(samples) / float(sample_rate)

    # Remove DC and normalize
    work = np.asarray(samples, dtype=np.float64)
    work = work - np.mean(work)
    peak = float(np.max(np.abs(work)))
    if peak < 1e-7:
        return DSPAnalysis(
            tone_hz=freq_hz or 700.0,
            snr_db=0.0,
            envelope=np.zeros(0, dtype=np.float64),
            envelope_rate=500.0,
            threshold_low=0.35,
            threshold_high=0.55,
            segments=[],
            duration_sec=duration_sec,
        )
    work = work / peak

    work, sr = _decimate_if_needed(work, sample_rate, max_sr=16000)

    detected_hz, snr_db = detect_tone_frequency(work, sr, min_hz=min_hz, max_hz=max_hz)
    use_hz = float(freq_hz) if freq_hz is not None else detected_hz

    env, env_rate = extract_envelope(work, sr, tone_hz=use_hz, bandwidth_hz=bandwidth_hz)
    thresh_low, thresh_high, has_signal = _compute_hysteresis_thresholds(env)

    # Reject pure noise (very low narrowband SNR when frequency was auto-detected)
    if not has_signal or (freq_hz is None and snr_db < 4.0):
        return DSPAnalysis(
            tone_hz=use_hz,
            snr_db=snr_db,
            envelope=env,
            envelope_rate=env_rate,
            threshold_low=thresh_low,
            threshold_high=thresh_high,
            segments=[],
            duration_sec=duration_sec,
        )

    segments = _extract_segments_with_hysteresis(
        env,
        env_rate,
        thresh_low=thresh_low,
        thresh_high=thresh_high,
        min_glitch_ms=min_glitch_ms,
    )

    return DSPAnalysis(
        tone_hz=use_hz,
        snr_db=snr_db,
        envelope=env,
        envelope_rate=env_rate,
        threshold_low=thresh_low,
        threshold_high=thresh_high,
        segments=segments,
        duration_sec=duration_sec,
    )
