"""A short, locally generated bell played at the frame's normal speaker volume."""

import asyncio
import logging
import math
import struct
import tempfile
import wave
from pathlib import Path


def write_chime(path: Path):
    rate = 22050
    samples = bytearray()
    for i in range(int(rate * 0.9)):
        t = i / rate
        sample = 0.0
        for start, frequency in ((0, 1046.5), (0.12, 1568)):
            age = t - start
            if age >= 0:
                envelope = min(1, age / 0.008) * math.exp(-7 * age)
                sample += 0.22 * envelope * math.sin(2 * math.pi * frequency * age)
        samples.extend(struct.pack("<h", int(32767 * sample)))
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(samples)


async def play_chime(sink: str):
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="frame-chime-") as directory:
            path = Path(directory) / "message.wav"
            write_chime(path)
            process = await asyncio.create_subprocess_exec(
                "paplay", f"--device={sink}", str(path),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            code = await asyncio.wait_for(process.wait(), 5)
            if code:
                logging.getLogger(__name__).warning("Message chime exited with code %s", code)
    except (OSError, asyncio.TimeoutError):
        logging.getLogger(__name__).exception("Cannot play message chime")
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
