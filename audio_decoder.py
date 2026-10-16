"""Decode audio into the mono float32 samples accepted by Whisper."""

from __future__ import annotations

import io
from pathlib import Path


def decode_audio(source: Path, sampling_rate: int = 16000):
    # Lazy imports keep library discovery and access diagnostics dependency-free.
    import av
    import numpy as np

    # Own the decoding boundary: faster-whisper 1.2.1's file decoder passes
    # metadata_errors to av.open, an argument removed in PyAV 19.
    resampler = av.AudioResampler(format="flt", layout="mono", rate=sampling_rate)
    with io.BytesIO() as samples:
        with av.open(str(source), mode="r") as container:
            if not container.streams.audio:
                raise ValueError(f"No audio stream in {source.name}")
            for frame in container.decode(audio=0):
                frame.pts = None
                for converted in resampler.resample(frame):
                    samples.write(converted.to_ndarray().tobytes())
            # Emit the remaining samples held by the resampling filter.
            for converted in resampler.resample(None):
                samples.write(converted.to_ndarray().tobytes())
        audio = np.frombuffer(samples.getvalue(), dtype=np.float32).copy()
    if not audio.size:
        raise ValueError(f"No audio samples in {source.name}")
    return audio
