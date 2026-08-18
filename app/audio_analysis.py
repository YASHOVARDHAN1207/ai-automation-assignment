"""Extract measurable properties from a submitted audio file.

What the assignment asks for: duration, sample rate (kHz), bitrate, loudness
(dB), and a rough noise/quality estimate.

Two things are worth knowing before reading the code:

**Why loudness is reported three ways.** "Loudness in dB" is not one number.
Peak dBFS answers "did it clip", RMS dBFS answers "how hot is the signal", and
LUFS answers "how loud does this sound to a human" - which is the only one that
compares two different recordings fairly, because it weights frequencies the way
an ear does and ignores silence. A quiet clip with one loud cough has a high
peak and a low LUFS. All three are stored.

**Why there are two decode paths.** The browser recorder in this app produces
16-bit PCM WAV, which Python's stdlib `wave` module can read with no external
binary at all - so the core flow works on a machine with nothing installed but
Flask and numpy. Uploaded mp3/m4a/webm files cannot be decoded in pure Python,
so those go through ffmpeg when it is available, and degrade to
container-metadata-only (duration/bitrate/sample rate, no loudness) when it is
not. The app never fails a submission because a codec was inconvenient; it
records what it could measure and says what it could not.
"""
import json
import math
import os
import shutil
import struct
import subprocess
import wave

import numpy as np

# --- tunables --------------------------------------------------------------

FRAME_MS = 50               # analysis frame for noise-floor / silence stats
NOISE_PERCENTILE = 10       # the quietest tenth of frames is taken as noise
MIN_SEPARATION_DB = 6       # below this spread, signal and noise are inseparable
MAX_SPLIT_DB = 15           # ceiling on the adaptive signal/noise split point
SILENCE_DBFS = -50.0        # below this a frame is silence
CLIP_THRESHOLD = 0.98       # |sample| at or above this is treated as clipped
MAX_ANALYSIS_SECONDS = 600  # guard against a pathological upload

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class AudioAnalysisError(Exception):
    pass


# ---------------------------------------------------------------------------
# container metadata
# ---------------------------------------------------------------------------

def probe_container(path):
    """Ask ffprobe what the container claims. Returns {} when unavailable."""
    if not FFPROBE:
        return {}
    try:
        raw = subprocess.check_output(
            [FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", "-select_streams", "a:0", path],
            stderr=subprocess.STDOUT, timeout=30)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return {}

    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {}

    streams = payload.get("streams") or [{}]
    stream, fmt = streams[0], payload.get("format", {})

    def num(value, cast=float):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return None

    return {
        "codec": stream.get("codec_name"),
        "codec_long": stream.get("codec_long_name"),
        "container": fmt.get("format_name"),
        "sample_rate_hz": num(stream.get("sample_rate"), int),
        "channels": num(stream.get("channels"), int),
        "duration_seconds": num(stream.get("duration")) or num(fmt.get("duration")),
        "bitrate_bps": num(stream.get("bit_rate"), int) or num(fmt.get("bit_rate"), int),
    }


# ---------------------------------------------------------------------------
# decoding to float32 mono
# ---------------------------------------------------------------------------

def _read_wav(path):
    """Decode PCM WAV with the standard library only."""
    with wave.open(path, "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(min(handle.getnframes(), rate * MAX_ANALYSIS_SECONDS))

    if width == 1:                                    # unsigned 8-bit
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:                                  # packed 24-bit
        raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        packed = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        packed = np.where(packed & 0x800000, packed - 0x1000000, packed)
        data = packed.astype(np.float32) / 8388608.0
    elif width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise AudioAnalysisError("unsupported WAV sample width: %d bytes" % width)

    if channels > 1:
        usable = (len(data) // channels) * channels
        data = data[:usable].reshape(-1, channels).mean(axis=1)
    return data, rate, channels, width * 8


def _decode_with_ffmpeg(path, target_rate=None):
    """Decode anything ffmpeg understands into float32 mono."""
    if not FFMPEG:
        raise AudioAnalysisError(
            "this file is not PCM WAV and ffmpeg is not installed, so it cannot "
            "be decoded for loudness analysis")
    command = [FFMPEG, "-v", "error", "-i", path, "-map", "a:0",
               "-t", str(MAX_ANALYSIS_SECONDS), "-ac", "1", "-f", "s16le"]
    if target_rate:
        command += ["-ar", str(target_rate)]
    command.append("-")
    try:
        raw = subprocess.check_output(command, stderr=subprocess.PIPE, timeout=120)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise AudioAnalysisError("ffmpeg could not decode this file: %s" % (
            detail[-1] if detail else "unknown error"))
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise AudioAnalysisError("ffmpeg failed: %s" % exc)

    if not raw:
        raise AudioAnalysisError("the file contains no decodable audio")
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def load_samples(path, container):
    """Return (samples, sample_rate, channels, bit_depth, backend).

    Samples are float32 mono in [-1, 1]. The WAV path is tried first because it
    needs no external binary at all.
    """
    try:
        samples, rate, channels, bit_depth = _read_wav(path)
        return samples, rate, channels, bit_depth, "stdlib wave"
    except (wave.Error, EOFError, AudioAnalysisError):
        pass    # not a PCM WAV - fall through to ffmpeg

    rate = container.get("sample_rate_hz") or 48000
    samples = _decode_with_ffmpeg(path, target_rate=rate)
    return samples, rate, container.get("channels") or 1, 16, "ffmpeg"


# ---------------------------------------------------------------------------
# dsp helpers
# ---------------------------------------------------------------------------

def _db(value, floor=-120.0):
    """Amplitude ratio -> dB, with a floor so silence does not become -inf."""
    if value is None:
        return None
    if value <= 1e-12:
        return floor
    return round(20.0 * math.log10(value), 2)


def _biquad(b, a, x):
    """Apply one biquad section. Uses scipy when present, else a direct loop.

    scipy is optional on purpose: it is a 30MB dependency for one function, and
    the fallback is exact - just slower. Recordings here are seconds long.
    """
    try:
        from scipy.signal import lfilter
        return lfilter(b, a, x)
    except ImportError:
        pass

    y = np.empty_like(x)
    x1 = x2 = y1 = y2 = 0.0
    b0, b1, b2 = b
    _, a1, a2 = a
    for i in range(len(x)):
        x0 = float(x[i])
        y0 = b0 * x0 + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        y[i] = y0
        x2, x1 = x1, x0
        y2, y1 = y1, y0
    return y


def _k_weighting_coefficients(rate):
    """ITU-R BS.1770 K-weighting: a high shelf plus a high-pass.

    The published coefficients are specified at 48 kHz. Rather than resample
    every clip to 48 kHz, the two filters are re-derived for the actual sample
    rate through the bilinear transform, which is what a browser recording at
    44.1 or 16 kHz needs.
    """
    # Stage 1: high-frequency shelf, +4 dB above ~1.7 kHz (head-shadow model).
    f0, gain_db, q = 1681.9744509555319, 3.99984385397, 0.7071752369554193
    k = math.tan(math.pi * f0 / rate)
    vh = 10.0 ** (gain_db / 20.0)
    vb = vh ** 0.4996667741545416
    denom = 1.0 + k / q + k * k
    shelf_b = [(vh + vb * k / q + k * k) / denom,
               2.0 * (k * k - vh) / denom,
               (vh - vb * k / q + k * k) / denom]
    shelf_a = [1.0, 2.0 * (k * k - 1.0) / denom, (1.0 - k / q + k * k) / denom]

    # Stage 2: high-pass at ~38 Hz, removing rumble the ear does not weigh.
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / rate)
    denom = 1.0 + k / q + k * k
    hp_b = [1.0, -2.0, 1.0]
    hp_a = [1.0, 2.0 * (k * k - 1.0) / denom, (1.0 - k / q + k * k) / denom]

    return (shelf_b, shelf_a), (hp_b, hp_a)


def integrated_lufs(samples, rate):
    """Gated integrated loudness, BS.1770-style, mono.

    Implemented rather than imported so the gating is visible: 400 ms blocks
    overlapping by 75%, an absolute gate at -70 LUFS to drop silence, then a
    relative gate 10 LU below the ungated mean so a long pause cannot drag the
    figure down. This is why LUFS compares two recordings fairly and plain RMS
    does not.

    Returns (lufs, loudness_range_lu) or (None, None) when the clip is shorter
    than one gating block.
    """
    if len(samples) < int(0.4 * rate):
        return None, None

    (shelf_b, shelf_a), (hp_b, hp_a) = _k_weighting_coefficients(rate)
    weighted = _biquad(hp_b, hp_a, _biquad(shelf_b, shelf_a, samples.astype(np.float64)))

    block = int(0.4 * rate)
    step = max(1, block // 4)
    starts = range(0, len(weighted) - block + 1, step)
    powers = np.array([np.mean(weighted[s:s + block] ** 2) for s in starts])
    powers = powers[powers > 0]
    if not len(powers):
        return None, None

    block_lufs = -0.691 + 10.0 * np.log10(powers)

    above_absolute = powers[block_lufs > -70.0]
    if not len(above_absolute):
        return None, None
    relative_gate = (-0.691 + 10.0 * math.log10(float(np.mean(above_absolute)))) - 10.0

    kept = powers[block_lufs > max(-70.0, relative_gate)]
    if not len(kept):
        kept = above_absolute
    lufs = -0.691 + 10.0 * math.log10(float(np.mean(kept)))

    # Loudness range: spread of the gated blocks, a proxy for dynamics.
    gated_block_lufs = block_lufs[block_lufs > max(-70.0, relative_gate)]
    lra = None
    if len(gated_block_lufs) >= 4:
        lra = float(np.percentile(gated_block_lufs, 95) - np.percentile(gated_block_lufs, 10))

    return round(lufs, 2), (round(lra, 2) if lra is not None else None)


def frame_statistics(samples, rate):
    """Per-frame RMS, and the noise/silence figures derived from it."""
    frame = max(1, int(rate * FRAME_MS / 1000.0))
    usable = (len(samples) // frame) * frame
    if usable < frame:
        return {}

    frames = samples[:usable].reshape(-1, frame)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    rms_db = 20.0 * np.log10(np.maximum(rms, 1e-12))

    # The noise floor is the quiet tail of the distribution, not the minimum -
    # a single dropped frame should not define it.
    noise_floor_db = float(np.percentile(rms_db, NOISE_PERCENTILE))
    dynamic_range_db = float(np.percentile(rms_db, 95) - noise_floor_db)

    stats = {
        "noise_floor_dbfs": round(noise_floor_db, 2),
        "frame_dynamic_range_db": round(dynamic_range_db, 2),
        "silence_pct": round(100.0 * float((rms_db < SILENCE_DBFS).mean()), 2),
        "frame_count": int(len(rms)),
        "estimated_snr_db": None,
        "speech_ratio_pct": None,
        "snr_note": None,
    }

    # SNR here is estimated by splitting frames into "signal" and "background",
    # which only means anything if the frame energies are actually bimodal -
    # speech is loud-quiet-loud, so they are. A stationary signal (a test tone,
    # continuous hiss, a hum) has one flat energy level, and any split would put
    # every frame on the same side and then report 0 dB. That would read as "very
    # noisy" when the truth is "this method does not apply here", so it returns
    # unknown instead of a number it cannot justify.
    if dynamic_range_db < MIN_SEPARATION_DB:
        stats["snr_note"] = (
            "signal is stationary (only %.1f dB of frame-to-frame variation), so "
            "speech-versus-background separation is not meaningful" % dynamic_range_db)
        return stats

    # The split point adapts to the spread that is actually there. A fixed
    # "floor + 10 dB" threshold abstained on genuinely noisy clips - exactly the
    # ones worth flagging - because at 9 dB SNR nothing clears the bar. Half the
    # measured spread sits between the two clusters wherever they are, and the
    # cap stops a loud burst over true silence from pushing the line above quiet
    # speech.
    split_db = min(max(3.0, dynamic_range_db * 0.5), MAX_SPLIT_DB)
    speech_mask = rms_db > (noise_floor_db + split_db)
    if not speech_mask.any() or speech_mask.all():
        stats["snr_note"] = "could not separate signal frames from background frames"
        return stats

    speech_db = 10.0 * math.log10(float(np.mean(rms[speech_mask] ** 2)))
    noise_db = 10.0 * math.log10(float(np.mean(rms[~speech_mask] ** 2)))
    stats["estimated_snr_db"] = round(speech_db - noise_db, 2)
    stats["speech_ratio_pct"] = round(100.0 * float(speech_mask.mean()), 2)
    return stats


# ---------------------------------------------------------------------------
# quality scoring
# ---------------------------------------------------------------------------

def score_quality(metrics):
    """Turn the measurements into one 0-100 number plus the reasons for it.

    The reasons matter more than the score: "72/100" tells a reviewer nothing,
    "clipped on 3.4% of samples" tells them to re-record closer to the mic.
    """
    score = 100
    reasons = []

    snr = metrics.get("estimated_snr_db")
    if snr is None:
        # Unknown is not the same as bad. Penalising a measurement that was
        # never taken is how a scoring function starts lying.
        if metrics.get("snr_note"):
            reasons.append("SNR not estimated: %s" % metrics["snr_note"])
    elif snr < 10:
        score -= 30
        reasons.append("very noisy: only %.1f dB between speech and background" % snr)
    elif snr < 20:
        score -= 15
        reasons.append("noisy background (%.1f dB SNR)" % snr)

    clipping = metrics.get("clipping_pct")
    if clipping:
        if clipping > 1.0:
            score -= 25
            reasons.append("clipped on %.2f%% of samples - recorded too hot" % clipping)
        elif clipping > 0.1:
            score -= 10
            reasons.append("occasional clipping (%.2f%% of samples)" % clipping)

    rms = metrics.get("rms_dbfs")
    if rms is not None and rms < -40:
        score -= 20
        reasons.append("very quiet (%.1f dBFS RMS) - likely far from the mic" % rms)

    duration = metrics.get("duration_seconds")
    if duration is not None and duration < 1.0:
        score -= 20
        reasons.append("shorter than one second")

    rate = metrics.get("sample_rate_hz")
    if rate and rate < 16000:
        score -= 15
        reasons.append("sample rate %d Hz is below speech-recognition quality" % rate)

    silence = metrics.get("silence_pct")
    if silence is not None and silence > 60:
        score -= 15
        reasons.append("%.0f%% of the clip is silence" % silence)

    dc = metrics.get("dc_offset")
    if dc is not None and abs(dc) > 0.02:
        score -= 5
        reasons.append("DC offset of %.3f suggests a hardware fault" % dc)

    score = max(0, min(100, score))
    if score >= 85:
        label = "excellent"
    elif score >= 70:
        label = "good"
    elif score >= 50:
        label = "fair"
    else:
        label = "poor"

    if not reasons:
        reasons.append("no problems detected")
    return score, label, reasons


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def analyse(path):
    """Measure everything measurable about one audio file.

    Never raises for a merely awkward file: `analysis_ok` says whether the
    waveform could be decoded, and `analysis_note` says why not. A submission is
    still recorded either way - losing a gig worker's audio because the server
    lacked a codec would be the worse failure.
    """
    file_size = os.path.getsize(path)
    container = probe_container(path)

    result = {
        "file_size_bytes": file_size,
        "codec": container.get("codec"),
        "container_format": container.get("container"),
        "duration_seconds": container.get("duration_seconds"),
        "sample_rate_hz": container.get("sample_rate_hz"),
        "channels": container.get("channels"),
        "bit_depth": None,
        "bitrate_kbps": (round(container["bitrate_bps"] / 1000.0, 1)
                         if container.get("bitrate_bps") else None),
        "peak_dbfs": None, "rms_dbfs": None, "crest_factor_db": None,
        "loudness_lufs": None, "loudness_range_lu": None,
        "noise_floor_dbfs": None, "estimated_snr_db": None, "snr_note": None,
        "frame_dynamic_range_db": None,
        "speech_ratio_pct": None, "silence_pct": None,
        "clipping_pct": None, "dc_offset": None,
        "analysis_ok": 0, "analysis_note": None,
        "analysis_backend": "ffprobe" if container else "none",
    }

    try:
        samples, rate, channels, bit_depth, backend = load_samples(path, container)
    except AudioAnalysisError as exc:
        result["analysis_note"] = str(exc)
        if result["duration_seconds"] and not result["bitrate_kbps"]:
            result["bitrate_kbps"] = round(
                file_size * 8.0 / result["duration_seconds"] / 1000.0, 1)
        return result

    if not len(samples):
        result["analysis_note"] = "decoded to zero samples"
        return result

    result["analysis_backend"] = backend
    result["sample_rate_hz"] = rate
    result["sample_rate_khz"] = round(rate / 1000.0, 3)
    result["channels"] = channels
    result["bit_depth"] = bit_depth
    result["duration_seconds"] = round(len(samples) / float(rate), 3)

    if not result["bitrate_kbps"]:
        # For PCM this is exact; for anything else it is the file's average.
        result["bitrate_kbps"] = round(
            file_size * 8.0 / max(result["duration_seconds"], 1e-6) / 1000.0, 1)
    result["pcm_bitrate_kbps"] = (round(rate * bit_depth * channels / 1000.0, 1)
                                  if bit_depth else None)

    peak = float(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    result["peak_dbfs"] = _db(peak)
    result["rms_dbfs"] = _db(rms)
    if rms > 0 and peak > 0:
        result["crest_factor_db"] = round(result["peak_dbfs"] - result["rms_dbfs"], 2)
    result["dc_offset"] = round(float(np.mean(samples)), 5)
    result["clipping_pct"] = round(
        100.0 * float(np.mean(np.abs(samples) >= CLIP_THRESHOLD)), 4)

    lufs, lra = integrated_lufs(samples, rate)
    result["loudness_lufs"] = lufs
    result["loudness_range_lu"] = lra

    result.update(frame_statistics(samples, rate))

    score, label, reasons = score_quality(result)
    result["quality_score"] = score
    result["quality_label"] = label
    result["quality_reasons"] = reasons
    result["analysis_ok"] = 1
    return result


def _is_wav(path):
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    except OSError:
        return False


def wav_header(sample_rate, channels, bits, data_bytes):
    """Build a 44-byte PCM WAV header. Used by the test fixtures."""
    byte_rate = sample_rate * channels * bits // 8
    return b"".join([
        b"RIFF", struct.pack("<I", 36 + data_bytes), b"WAVEfmt ",
        struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate,
                    channels * bits // 8, bits),
        b"data", struct.pack("<I", data_bytes),
    ])


if __name__ == "__main__":       # pragma: no cover - manual probing aid
    import sys
    for target in sys.argv[1:]:
        print(target)
        print(json.dumps(analyse(target), indent=2))
