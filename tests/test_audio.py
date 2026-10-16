"""Exercise the real decoder with generated audio, without downloading a model."""

import os
from pathlib import Path
import tempfile
import unittest
import wave

from audio_decoder import decode_audio

try:
    import av
    import numpy as np
except ImportError:
    av = np = None


@unittest.skipIf(av is None or np is None, "Run with .venv/bin/python after make install for real decoder tests")
class AudioTests(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ.get("HOLODECK", "~/.holodeck")).expanduser() / "voice-memo-transcriber"
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.wav"

    def write_wav(self, samples, rate, channels=1):
        with wave.open(str(self.path), "wb") as output:
            output.setnchannels(channels)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(samples.astype("<i2").tobytes())

    def test_mono_samples_keep_amplitude(self):
        original = (np.sin(np.arange(16000) * 2 * np.pi * 440 / 16000) * 10000).astype("int16")
        self.write_wav(original, 16000)
        actual = decode_audio(self.path)
        self.assertEqual(actual.dtype, np.float32)
        self.assertEqual(actual.shape, (16000,))
        np.testing.assert_allclose(actual, original.astype(np.float32) / 32768, atol=1e-6)

    def test_stereo_resampling_preserves_duration_and_tone(self):
        original = np.sin(np.arange(48000) * 2 * np.pi * 440 / 48000) * 10000
        self.write_wav(np.column_stack([original, original]), 48000, channels=2)
        actual = decode_audio(self.path)
        self.assertEqual(actual.shape, (16000,))
        self.assertTrue(np.isfinite(actual).all())
        peak_hz = np.argmax(np.abs(np.fft.rfft(actual)))
        self.assertEqual(peak_hz, 440)

    def test_silence_remains_silence(self):
        self.write_wav(np.zeros(44100), 44100)
        actual = decode_audio(self.path)
        self.assertEqual(actual.shape, (16000,))
        self.assertTrue((actual == 0).all())

    def test_invalid_audio_reports_error(self):
        self.path.write_bytes(b"not audio")
        with self.assertRaises(av.error.InvalidDataError):
            decode_audio(self.path)

    def test_empty_audio_reports_error(self):
        self.write_wav(np.zeros(0), 16000)
        with self.assertRaisesRegex(ValueError, "No audio samples"):
            decode_audio(self.path)
