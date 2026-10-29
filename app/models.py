"""Hot-swappable YOLO model registry.

Weights are discovered by scanning ``models/`` for ``*.pt`` files on every
request, so new files dropped in are picked up without a restart. Loading
happens off the event loop; the swap itself is an atomic reference exchange
guarded by a lock, and a ``generation`` counter lets consumers detect swaps.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .config import MODELS_DIR


def _pick_device() -> str:
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda:0"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:  # pragma: no cover - torch import edge cases
        pass
    return "cpu"


class ModelManager:
    def __init__(self, models_dir: Path = MODELS_DIR) -> None:
        self.models_dir = models_dir
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.device = _pick_device()
        self._lock = threading.Lock()  # guards the active reference
        self._load_lock = threading.Lock()  # serializes concurrent loads
        self._model: Any = None
        self._name: Optional[str] = None
        self._names: Dict[int, str] = {}
        self.generation = 0
        self.state = "empty"  # empty | loading | ready | error
        self.loading: Optional[str] = None
        self.error: Optional[str] = None
        self.load_ms: Optional[float] = None

    # ------------------------------------------------------------- discovery
    def scan(self) -> List[Dict[str, Any]]:
        out = []
        for p in sorted(self.models_dir.glob("*.pt")):
            st = p.stat()
            out.append({"name": p.name, "size_mb": round(st.st_size / 1e6, 1), "mtime": st.st_mtime})
        return out

    def resolve(self, name: str) -> Path:
        path = (self.models_dir / Path(name).name).resolve()
        if path.parent != self.models_dir.resolve() or path.suffix != ".pt" or not path.is_file():
            raise FileNotFoundError(f"model not found: {name}")
        return path

    # ---------------------------------------------------------------- loading
    def load(self, name: str) -> Dict[str, Any]:
        """Blocking load + warmup. Call via ``asyncio.to_thread``."""
        path = self.resolve(name)
        with self._load_lock:
            self.state, self.loading, self.error = "loading", name, None
            t0 = time.perf_counter()
            try:
                from ultralytics import YOLO

                model = YOLO(str(path))
                # Warm up so the first streamed frame doesn't stall.
                model.predict(np.zeros((320, 320, 3), dtype=np.uint8), device=self.device, verbose=False)
                names = {int(k): str(v) for k, v in model.names.items()}
            except Exception as exc:
                with self._lock:
                    self.state = "ready" if self._model is not None else "error"
                    self.error = f"{type(exc).__name__}: {exc}"
                    self.loading = None
                raise
            with self._lock:
                self._model, self._name, self._names = model, name, names
                self.generation += 1
                self.state, self.loading = "ready", None
                self.load_ms = round((time.perf_counter() - t0) * 1000, 1)
        return self.info()

    def get(self) -> Tuple[Any, Optional[str], int]:
        with self._lock:
            return self._model, self._name, self.generation

    @property
    def names(self) -> Dict[int, str]:
        with self._lock:
            return dict(self._names)

    def info(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active": self._name,
                "state": self.state,
                "loading": self.loading,
                "error": self.error,
                "device": self.device,
                "generation": self.generation,
                "load_ms": self.load_ms,
                "classes": {str(k): v for k, v in self._names.items()},
            }
