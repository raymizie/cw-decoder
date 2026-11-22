"""Audio I/O utilities: native WAV read/write and ffmpeg conversion for OGG/MP3/M4A/etc."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

SUPPORTED_AUDIO_EXTENSIONS = {
    ".wav",
    ".ogg",
    ".oga",
    ".opus",
    ".mp3",
    ".m4a",
    ".aac",
    ".amr",
    ".flac",
    ".webm",
    ".wma",
}


class AudioLoadError(RuntimeError):
    """Raised when an audio file cannot be read or converted."""


def find_ffmpeg() -> str | None:
    """Locate ffmpeg executable via env var, PATH, or optional imageio_ffmpeg."""
    env_bin = os.environ.get("FFMPEG_PATH", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin

    which_bin = shutil.which("ffmpeg")
    if which_bin:
        return which_bin

    try:
        import imageio_ffmpeg  # type: ignore

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:
        pass

    return None


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a PCM WAV file natively using Python's wave module + NumPy.

    Returns (mono_float32_samples_in_-1_to_1, sample_rate).
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise AudioLoadError(f"Fail audio tidak dijumpai: {file_path}")

    try:
        with wave.open(str(file_path), "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            sample_rate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except wave.Error as exc:
        raise AudioLoadError(f"Fail WAV tidak sah atau format mampat ({exc})") from exc

    if n_frames == 0:
        return np.zeros(0, dtype=np.float32), sample_rate

    if sampwidth == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        data = (data - 128.0) / 128.0
    elif sampwidth == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 3:
        # 24-bit signed little-endian PCM
        raw_u8 = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        padded = np.zeros((raw_u8.shape[0], 4), dtype=np.uint8)
        padded[:, 1:4] = raw_u8
        data = padded.view("<i4").reshape(-1).astype(np.float32) / 2147483648.0
    elif sampwidth == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise AudioLoadError(f"Lebar sampel WAV tidak disokong: {sampwidth} bait")

    if n_channels > 1:
        data = data.reshape(-1, n_channels).mean(axis=1)

    return data.astype(np.float32), int(sample_rate)


def write_wav(path: str | Path, samples: np.ndarray, sample_rate: int = 16000) -> Path:
    """Write mono float samples [-1.0, 1.0] to a 16-bit PCM WAV file."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype("<i2")

    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm16.tobytes())

    return out_path


def convert_to_wav(input_path: str | Path, output_path: str | Path, sample_rate: int = 16000) -> Path:
    """Convert any audio format (OGG, Opus, MP3, M4A, AMR, etc.) to mono PCM WAV using ffmpeg."""
    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        raise AudioLoadError(
            "Format audio bukan WAV memerlukan FFmpeg (untuk OGG/MP3/M4A/Opus dari WhatsApp & Telegram). "
            "Sila pasang FFmpeg pada PATH atau pasang pakej `pip install imageio-ffmpeg`, "
            "atau tetapkan pembolehubah FFMPEG_PATH."
        )

    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-acodec",
        "pcm_s16le",
        "-f",
        "wav",
        str(output_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = proc.stderr.strip() or f"exit code {proc.returncode}"
        raise AudioLoadError(f"FFmpeg gagal menukar audio: {err}")

    return Path(output_path)


def load_audio(path: str | Path, target_sr: int = 16000) -> tuple[np.ndarray, int]:
    """Load WAV natively or convert other formats via FFmpeg into mono float32 samples."""
    file_path = Path(path)
    if not file_path.is_file():
        raise AudioLoadError(f"Fail audio tidak dijumpai: {file_path}")

    # Try native WAV first if extension is .wav or RIFF header matches
    is_wav_ext = file_path.suffix.lower() == ".wav"
    header = file_path.read_bytes()[:12]
    is_riff_wav = header[:4] == b"RIFF" and header[8:12] == b"WAVE"

    if is_wav_ext or is_riff_wav:
        try:
            return read_wav(file_path)
        except AudioLoadError:
            # Might be compressed WAV (e.g., IEEE float or ADPCM); fall back to ffmpeg
            if find_ffmpeg() is None:
                raise

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_wav = Path(tmp.name)

    try:
        convert_to_wav(file_path, tmp_wav, sample_rate=target_sr)
        return read_wav(tmp_wav)
    finally:
        try:
            tmp_wav.unlink(missing_ok=True)
        except OSError:
            pass
