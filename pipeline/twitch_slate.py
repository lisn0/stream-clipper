"""Detect Twitch's commercial-break card by comparing downscaled RGB frames."""

import base64
import subprocess


# 16×9 RGB sample of Twitch's "Commercial break in progress" card.
SLATE = base64.b64decode(
    "NxtmPiJtTSOEVCqLUzGgWjinRVG3UFvCLnbGM3vLGpLQHpbTGZfQE5HKIH6+E3GyNRhfPiFo"
    "ViGGXimPXy2rZTOwVlOfbWu2P4GvQ4SzEqLaF6ffFKjZDaHSH4S+DXOtMxNbPyBoYh+SbSmd"
    "cCm3dS68T0zCWFbLI4HKL4zWC7DfErfmEbblCK3bIoS+CWulNxFiSCFzdx6ygSe7iCfMiinP"
    "YETLZ0vSJn3RMYndBbfkD8HtDcHrArXfJXqzBVqTRhJ6WiaNlyXGrjzesD7grz3fhFbUhVfW"
    "Q4TXSInbIbXiL8PwKcTyDajWJWSbBUR8WxeRbyqlriLXtyzgtyzgtSnehELTg0HSO2vPQnHW"
    "E5jaGp/hHJnWCITBJU1/CjNlbRukfCqzqyPWsCnbsirdryjafz/Sfj7RQWDKRGTNGX/MG4LP"
    "H3q2D2mmJUNtEC5YdCCsfyy3nifNoizSoC/Xni3UdT7MdT7MRlrFRlrFJW29Imu7ImWeF1mS"
    "Ij9pEzBacyWuey22kCjGlC3LjTPQjDHPbT/KbT/KSFTARlO+LWCwKV2tJVmSG1CJI0FrGDVf"
)


def is_slate(frame):
    return len(frame) == len(SLATE) and sum(
        abs(pixel - sample) for pixel, sample in zip(frame, SLATE)
    ) <= 12 * len(SLATE)


def contains_slate(ffmpeg, path):
    frames = subprocess.run([
        ffmpeg, "-nostdin", "-loglevel", "error", "-threads", "2", "-i", str(path),
        "-an", "-vf", "fps=1,scale=16:9,format=rgb24", "-pix_fmt", "rgb24",
        "-f", "rawvideo", "-",
    ], check=True, timeout=60, stdout=subprocess.PIPE, start_new_session=True).stdout
    frame_size = len(SLATE)
    return any(is_slate(frames[i:i + frame_size]) for i in range(0, len(frames), frame_size))
