"""Frame sources. The pipeline only depends on the ``FrameSource`` interface,
so live inputs (see ``live.py``) can be slotted in without touching it."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from .config import VIDEOS_DIR

VIDEO_EXTS = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv"}


class FrameSource(ABC):
    """Minimal contract every input must satisfy."""

    is_live: bool = False
    fps: float = 30.0
    width: int = 0
    height: int = 0
    total_frames: Optional[int] = None

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def read(self) -> Tuple[bool, Optional[np.ndarray]]: ...

    @abstractmethod
    def close(self) -> None: ...

    @property
    def label(self) -> str:
        return self.__class__.__name__


class VideoFileSource(FrameSource):
    def __init__(self, path: Path) -> None:
        self.path = path
        self._cap: Optional[cv2.VideoCapture] = None

    @property
    def label(self) -> str:
        return self.path.name

    def open(self) -> None:
        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            raise RuntimeError(f"cannot open video: {self.path.name}")
        self._cap = cap
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.total_frames = n if n > 0 else None

    def read(self) -> Tuple[bool, Optional[np.ndarray]]:
        if self._cap is None:
            return False, None
        return self._cap.read()

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


# ---------------------------------------------------------------------------
# Video library helpers (used by Studio mode)
# ---------------------------------------------------------------------------

def list_videos(videos_dir: Path = VIDEOS_DIR) -> List[Dict]:
    videos_dir.mkdir(parents=True, exist_ok=True)
    return [
        {"name": p.name, "size_mb": round(p.stat().st_size / 1e6, 1)}
        for p in sorted(videos_dir.iterdir())
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    ]


def resolve_video(name: str, videos_dir: Path = VIDEOS_DIR) -> Path:
    path = (videos_dir / Path(name).name).resolve()
    if path.parent != videos_dir.resolve() or not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
        raise FileNotFoundError(f"video not found: {name}")
    return path


class FrameSeeker:
    """Caches one open capture per video for fast random-access scrubbing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._caps: Dict[Path, cv2.VideoCapture] = {}

    def _cap(self, path: Path) -> cv2.VideoCapture:
        cap = self._caps.get(path)
        if cap is None or not cap.isOpened():
            cap = cv2.VideoCapture(str(path))
            if not cap.isOpened():
                raise RuntimeError(f"cannot open video: {path.name}")
            # Keep the cache small; scrubbing is one video at a time.
            for old in list(self._caps.values()):
                old.release()
            self._caps = {path: cap}
        return cap

    def meta(self, path: Path) -> Dict:
        with self._lock:
            cap = self._cap(path)
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            return {
                "name": path.name,
                "fps": round(fps, 3),
                "frames": max(frames, 1),
                "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
                "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                "duration_s": round(frames / fps, 2) if frames > 0 else None,
            }

    def frame_jpeg(self, path: Path, index: int, quality: int = 85) -> Tuple[bytes, int]:
        with self._lock:
            cap = self._cap(path)
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            index = max(0, min(index, total - 1 if total > 0 else index))
            # Frame counts from container metadata can overshoot; walk back if needed.
            for idx in (index, index - 1, index - 5, 0):
                if idx < 0:
                    continue
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
                if ok and frame is not None:
                    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                    return buf.tobytes(), idx
            raise RuntimeError(f"cannot decode frame {index} of {path.name}")

    def release(self, path: Optional[Path] = None) -> None:
        with self._lock:
            for p, cap in list(self._caps.items()):
                if path is None or p == path:
                    cap.release()
                    del self._caps[p]
