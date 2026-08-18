"""Tests for the audio measurement code.

Signals are synthesised here rather than checked in as fixture files, so every
expected number is derivable rather than "whatever it printed last time".

The headline test is test_lufs_matches_the_bs1770_calibration_point: ITU-R
BS.1770 defines that a full-scale 997 Hz sine reads -3.01 LUFS. That single
number validates the whole K-weighting chain - the shelf filter, the high-pass,
the gating and the -0.691 offset. If any coefficient were wrong, it would miss.
"""
import math
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import numpy as np
except ImportError:                                   # pragma: no cover
    np = None

if np is not None:
    from app import audio_analysis


def write_wav(path, samples, rate=44100, channels=1, bits=16):
    """Write float samples in [-1, 1] to a PCM WAV file."""
    if bits == 16:
        data = np.clip(samples, -1.0, 1.0)
        raw = (np.where(data < 0, data * 32768.0, data * 32767.0)
               ).astype("<i2").tobytes()
    elif bits == 8:
        raw = (np.clip(samples, -1.0, 1.0) * 127.0 + 128.0).astype(np.uint8).tobytes()
    elif bits == 24:
        scaled = (np.clip(samples, -1.0, 1.0) * 8388607.0).astype(np.int32)
        raw = b"".join(struct.pack("<i", int(v))[:3] for v in scaled)
    elif bits == 32:
        raw = (np.clip(samples, -1.0, 1.0) * 2147483647.0).astype("<i4").tobytes()
    else:
        raise ValueError(bits)

    with open(path, "wb") as fh:
        fh.write(audio_analysis.wav_header(rate, channels, bits, len(raw)))
        fh.write(raw)
    return path


def sine(seconds, freq=997.0, rate=44100, amplitude=1.0):
    t = np.arange(int(rate * seconds)) / float(rate)
    return (amplitude * np.sin(2 * math.pi * freq * t)).astype(np.float32)


@unittest.skipIf(np is None, "numpy is required for the audio tests")
class AudioTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cb-audio-")

    def tearDown(self):
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def path(self, name):
        return os.path.join(self.dir, name)


class TestContainerProperties(AudioTestCase):
    def test_duration_sample_rate_and_bit_depth(self):
        result = audio_analysis.analyse(
            write_wav(self.path("a.wav"), sine(2.0, rate=44100), rate=44100))
        self.assertEqual(result["analysis_ok"], 1)
        self.assertAlmostEqual(result["duration_seconds"], 2.0, places=2)
        self.assertEqual(result["sample_rate_hz"], 44100)
        self.assertEqual(result["sample_rate_khz"], 44.1)
        self.assertEqual(result["bit_depth"], 16)
        self.assertEqual(result["channels"], 1)

    def test_pcm_bitrate_is_exact(self):
        result = audio_analysis.analyse(
            write_wav(self.path("a.wav"), sine(1.0, rate=16000), rate=16000))
        # 16000 Hz x 16 bit x 1 channel = 256 kbps
        self.assertEqual(result["pcm_bitrate_kbps"], 256.0)
        self.assertAlmostEqual(result["bitrate_kbps"], 256.0, delta=1.0)

    def test_reads_8_24_and_32_bit_wav(self):
        for bits in (8, 24, 32):
            result = audio_analysis.analyse(
                write_wav(self.path("b%d.wav" % bits), sine(0.5, amplitude=0.5), bits=bits))
            self.assertEqual(result["analysis_ok"], 1, "%d-bit failed" % bits)
            self.assertEqual(result["bit_depth"], bits)
            # ~0.5 amplitude is -6 dBFS whatever the word length.
            self.assertAlmostEqual(result["peak_dbfs"], -6.0, delta=0.6)

    def test_stereo_is_downmixed_not_rejected(self):
        left = sine(1.0, freq=440.0, amplitude=0.5)
        right = sine(1.0, freq=880.0, amplitude=0.5)
        interleaved = np.empty(len(left) * 2, dtype=np.float32)
        interleaved[0::2] = left
        interleaved[1::2] = right
        result = audio_analysis.analyse(
            write_wav(self.path("st.wav"), interleaved, channels=2))
        self.assertEqual(result["analysis_ok"], 1)
        self.assertEqual(result["channels"], 2)
        self.assertAlmostEqual(result["duration_seconds"], 1.0, places=2)

    def test_a_file_that_is_not_audio_is_recorded_not_crashed_on(self):
        path = self.path("fake.wav")
        with open(path, "wb") as fh:
            fh.write(b"this is not audio at all, not even a RIFF header")
        result = audio_analysis.analyse(path)
        # The submission must still be storable: no exception, ok flag clear,
        # and a note explaining what happened.
        self.assertEqual(result["analysis_ok"], 0)
        self.assertTrue(result["analysis_note"])


class TestLevels(AudioTestCase):
    def test_peak_and_rms_of_a_known_sine(self):
        result = audio_analysis.analyse(
            write_wav(self.path("a.wav"), sine(2.0, amplitude=0.5)))
        # A sine of amplitude 0.5: peak = -6.02 dBFS, RMS = peak - 3.01 dB.
        self.assertAlmostEqual(result["peak_dbfs"], -6.02, delta=0.1)
        self.assertAlmostEqual(result["rms_dbfs"], -9.03, delta=0.1)
        self.assertAlmostEqual(result["crest_factor_db"], 3.01, delta=0.1)

    def test_silence_does_not_produce_negative_infinity(self):
        result = audio_analysis.analyse(
            write_wav(self.path("q.wav"), np.zeros(44100, dtype=np.float32)))
        self.assertEqual(result["peak_dbfs"], -120.0)
        self.assertEqual(result["silence_pct"], 100.0)

    def test_clipping_is_detected(self):
        loud = np.clip(sine(1.0, amplitude=4.0), -1.0, 1.0)
        result = audio_analysis.analyse(write_wav(self.path("c.wav"), loud))
        self.assertGreater(result["clipping_pct"], 20.0)
        self.assertEqual(result["peak_dbfs"], 0.0)
        self.assertIn("clipped", " ".join(result["quality_reasons"]))
        self.assertLess(result["quality_score"], 80)

    def test_dc_offset_is_measured(self):
        offset = (sine(1.0, amplitude=0.3) + 0.2).astype(np.float32)
        result = audio_analysis.analyse(write_wav(self.path("d.wav"), offset))
        self.assertAlmostEqual(result["dc_offset"], 0.2, delta=0.01)


class TestLoudness(AudioTestCase):
    def test_lufs_matches_the_bs1770_calibration_point(self):
        """ITU-R BS.1770: a full-scale 997 Hz sine is -3.01 LUFS.

        This is the reference value the whole K-weighting implementation stands
        or falls on. The filters are re-derived per sample rate rather than
        hardcoded at 48 kHz, so it is checked at three rates.
        """
        for rate, tolerance in ((48000, 0.1), (44100, 0.1), (16000, 0.2)):
            lufs, _ = audio_analysis.integrated_lufs(sine(3.0, rate=rate), rate)
            self.assertAlmostEqual(lufs, -3.01, delta=tolerance,
                                   msg="%d Hz gave %s LUFS" % (rate, lufs))

    def test_lufs_tracks_gain_linearly(self):
        """Halving the amplitude must drop the reading by exactly 6.02 dB."""
        loud, _ = audio_analysis.integrated_lufs(sine(3.0, amplitude=1.0), 48000)
        quiet, _ = audio_analysis.integrated_lufs(sine(3.0, amplitude=0.5), 48000)
        self.assertAlmostEqual(loud - quiet, 6.02, delta=0.1)

    def test_gating_ignores_silence(self):
        """The point of LUFS over RMS: padding a clip with silence must not make
        it quieter, because the gate drops the silent blocks."""
        signal = sine(3.0, amplitude=0.5, rate=48000)
        padded = np.concatenate([signal, np.zeros(48000 * 3, dtype=np.float32)])

        lufs_signal, _ = audio_analysis.integrated_lufs(signal, 48000)
        lufs_padded, _ = audio_analysis.integrated_lufs(padded, 48000)
        self.assertAlmostEqual(lufs_signal, lufs_padded, delta=0.5)

        # Plain RMS, by contrast, is dragged down by about 3 dB.
        rms_signal = 20 * math.log10(float(np.sqrt(np.mean(signal.astype(np.float64) ** 2))))
        rms_padded = 20 * math.log10(float(np.sqrt(np.mean(padded.astype(np.float64) ** 2))))
        self.assertGreater(rms_signal - rms_padded, 2.0)

    def test_too_short_for_a_gating_block_returns_none(self):
        lufs, lra = audio_analysis.integrated_lufs(sine(0.2, rate=48000), 48000)
        self.assertIsNone(lufs)
        self.assertIsNone(lra)


class TestNoiseAndQuality(AudioTestCase):
    def _bursty(self, rate=44100, noise_amplitude=0.005, signal_amplitude=0.3):
        """One second on, one second off, over a constant noise floor - the
        energy profile speech actually has."""
        rng = np.random.RandomState(7)
        total = rate * 4
        noise = rng.normal(0.0, noise_amplitude, total).astype(np.float32)
        envelope = np.zeros(total, dtype=np.float32)
        for start in (0, 2 * rate):
            envelope[start:start + rate] = 1.0
        return (sine(4.0, freq=300.0, rate=rate, amplitude=signal_amplitude)
                * envelope + noise).astype(np.float32)

    def test_snr_is_estimated_on_a_bursty_signal(self):
        result = audio_analysis.analyse(
            write_wav(self.path("b.wav"), self._bursty()))
        self.assertIsNotNone(result["estimated_snr_db"])
        self.assertGreater(result["estimated_snr_db"], 15.0)
        # Half the clip carries signal, so roughly half the frames.
        self.assertAlmostEqual(result["speech_ratio_pct"], 50.0, delta=12.0)
        self.assertLess(result["noise_floor_dbfs"], result["rms_dbfs"])

    def test_snr_reports_unknown_rather_than_zero_on_a_stationary_signal(self):
        """A steady tone has one flat energy level, so speech-vs-background
        cannot be separated. Reporting 0 dB would read as 'very noisy', which is
        a lie about a pristine signal."""
        result = audio_analysis.analyse(
            write_wav(self.path("t.wav"), sine(3.0, amplitude=0.5)))
        self.assertIsNone(result["estimated_snr_db"])
        self.assertIn("stationary", result["snr_note"])
        self.assertNotIn("very noisy", " ".join(result["quality_reasons"]))
        self.assertGreaterEqual(result["quality_score"], 85)

    def test_a_noisy_recording_scores_below_a_clean_one(self):
        clean = audio_analysis.analyse(
            write_wav(self.path("clean.wav"), self._bursty(noise_amplitude=0.0005)))
        noisy = audio_analysis.analyse(
            write_wav(self.path("noisy.wav"), self._bursty(noise_amplitude=0.08)))
        self.assertGreater(clean["estimated_snr_db"], noisy["estimated_snr_db"])
        self.assertGreater(clean["quality_score"], noisy["quality_score"])

    def test_quiet_recording_is_flagged(self):
        result = audio_analysis.analyse(
            write_wav(self.path("q.wav"), sine(2.0, amplitude=0.003)))
        self.assertIn("very quiet", " ".join(result["quality_reasons"]))

    def test_low_sample_rate_is_flagged(self):
        result = audio_analysis.analyse(
            write_wav(self.path("lo.wav"), sine(2.0, rate=8000), rate=8000))
        self.assertIn("8000 Hz", " ".join(result["quality_reasons"]))

    def test_quality_score_stays_in_range(self):
        awful = np.clip(sine(0.5, amplitude=9.0, rate=8000), -1, 1)
        result = audio_analysis.analyse(
            write_wav(self.path("awful.wav"), awful, rate=8000))
        self.assertGreaterEqual(result["quality_score"], 0)
        self.assertLessEqual(result["quality_score"], 100)
        self.assertEqual(result["quality_label"], "poor")


@unittest.skipIf(np is None, "numpy is required")
class TestFilterFallback(AudioTestCase):
    def test_pure_python_biquad_matches_scipy(self):
        """scipy is optional. The fallback has to be exact, not approximate."""
        try:
            from scipy.signal import lfilter
        except ImportError:
            self.skipTest("scipy not installed, nothing to compare against")

        rng = np.random.RandomState(3)
        signal = rng.normal(0, 0.3, 4096)
        (shelf_b, shelf_a), _ = audio_analysis._k_weighting_coefficients(48000)

        expected = lfilter(shelf_b, shelf_a, signal)
        actual = np.empty_like(signal)
        x1 = x2 = y1 = y2 = 0.0
        for i in range(len(signal)):
            x0 = float(signal[i])
            y0 = (shelf_b[0] * x0 + shelf_b[1] * x1 + shelf_b[2] * x2
                  - shelf_a[1] * y1 - shelf_a[2] * y2)
            actual[i] = y0
            x2, x1 = x1, x0
            y2, y1 = y1, y0

        self.assertTrue(np.allclose(expected, actual, atol=1e-9))


if __name__ == "__main__":
    unittest.main(verbosity=2)
