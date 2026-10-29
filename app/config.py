"""Engine configuration: schema, thread-safe store and YAML persistence."""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("TRACKLINE_CONFIG", ROOT / "config.yaml"))
MODELS_DIR = Path(os.environ.get("TRACKLINE_MODELS", ROOT / "models"))
VIDEOS_DIR = Path(os.environ.get("TRACKLINE_VIDEOS", ROOT / "videos"))


class Line(BaseModel):
    """Counting line. Coordinates are normalized to [0, 1] of frame width/height
    so a zone stays valid regardless of display or source resolution."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = "line"
    p1: Tuple[float, float]
    p2: Tuple[float, float]

    @field_validator("p1", "p2")
    @classmethod
    def _clamp(cls, v: Tuple[float, float]) -> Tuple[float, float]:
        return (min(max(float(v[0]), 0.0), 1.0), min(max(float(v[1]), 0.0), 1.0))


class EngineConfig(BaseModel):
    model: Optional[str] = None
    video: Optional[str] = None
    conf: float = Field(0.35, ge=0.01, le=1.0)
    iou: float = Field(0.5, ge=0.05, le=0.95)
    imgsz: int = Field(640, ge=160, le=1920)
    classes: List[int] = Field(default_factory=list)  # empty = all classes
    tracker: Literal["bytetrack", "botsort"] = "bytetrack"
    anchor: Literal["center", "bottom"] = "center"
    realtime: bool = False  # pace processing to source FPS
    lines: List[Line] = Field(default_factory=list)
    git_autocommit: bool = False


class ConfigStore:
    """Holds the live config. Readers get immutable snapshots; every update bumps
    ``version`` so the pipeline can pick changes up on the very next frame."""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._cfg = self._load()
        self.version = 0

    def _load(self) -> EngineConfig:
        if self.path.exists():
            data = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            return EngineConfig.model_validate(data)
        return EngineConfig()

    def get(self) -> EngineConfig:
        with self._lock:
            return self._cfg

    def update(self, patch: Dict[str, Any]) -> EngineConfig:
        with self._lock:
            merged = self._cfg.model_dump()
            merged.update({k: v for k, v in patch.items() if k in EngineConfig.model_fields})
            self._cfg = EngineConfig.model_validate(merged)
            self.version += 1
            return self._cfg

    def save(self) -> Path:
        cfg = self.get()
        data = cfg.model_dump(mode="json")
        # Store tuples as flow lists for readable diffs.
        text = "# Trackline engine configuration (managed by the Studio UI)\n"
        text += yaml.safe_dump(data, sort_keys=False, default_flow_style=None)
        tmp = self.path.with_suffix(".yaml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)
        return self.path
