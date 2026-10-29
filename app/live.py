"""Live Stream Mode - architectural stub (WIP).

Live inputs plug into the exact same ``Pipeline`` via the ``FrameSource``
interface. What is still missing for production use is captured below so the
expansion path is explicit:

TODO(live):
  * Threaded grabber that always keeps only the newest frame (drop stale
    frames instead of queueing; RTSP latency otherwise grows unbounded).
  * Reconnect with exponential backoff on stream loss.
  * Wall-clock timestamps for events instead of frame/fps.
  * Optional FFmpeg/GStreamer backend selection (``cv2.CAP_FFMPEG`` with
    ``OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp"``).
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

import numpy as np

from .sources import FrameSource

LIVE_ENABLED = False
_URL_RE = re.compile(r"^(rtsp|rtsps|rtmp|http|https)://", re.IGNORECASE)


class LiveStreamSource(FrameSource):
    """RTSP/HTTP/webcam source. Not yet enabled - see module docstring."""

    is_live = True

    def __init__(self, uri: str) -> None:
        self.uri = uri

    @property
    def label(self) -> str:
        return self.uri

    @staticmethod
    def parse(uri: str) -> Tuple[str, object]:
        """Classify an input string: webcam index or network URL."""
        uri = uri.strip()
        if uri.isdigit():
            return "webcam", int(uri)
        if _URL_RE.match(uri):
            return "network", uri
        raise ValueError("expected an RTSP/HTTP URL or a webcam index (e.g. 0)")

    def open(self) -> None:
        raise NotImplementedError("live stream pipeline is not implemented yet")

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        raise NotImplementedError

    def close(self) -> None:
        pass


def status() -> Dict:
    return {
        "enabled": LIVE_ENABLED,
        "state": "not_implemented",
        "supported_inputs": ["rtsp://", "rtsps://", "http(s):// (MJPEG)", "webcam index"],
        "detail": "Live capture is stubbed. Pipeline and FrameSource contract are ready for it.",
    }
