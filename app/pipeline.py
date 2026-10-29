"""Inference + tracking + counting worker.

Runs on a dedicated daemon thread so heavy work never touches the asyncio
event loop. Each frame it re-reads:

* the active model (``ModelManager.get``) -> hot swaps apply on the next frame
* the config snapshot (``ConfigStore.get``) -> lines/conf/classes apply live

Tracking uses Ultralytics' BYTETracker / BOTSORT directly (instead of
``model.track``) so the tracker lifecycle is owned here and can be reset
cleanly on model or tracker swaps.
"""

from __future__ import annotations

import csv
import inspect
import io
import threading
import time
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Deque, Dict, List, Optional, Tuple

import cv2
import numpy as np
import yaml

from .config import ConfigStore, EngineConfig
from .counter import CrossingEvent, Detection, LineCounter, LineSpec
from .geometry import normal
from .models import ModelManager
from .sources import FrameSource

WHITE = (245, 245, 245)
BLACK = (10, 10, 10)
GREY = (160, 160, 160)
AMBER = (0, 176, 255)  # single signal color (BGR) for crossing flashes


# ---------------------------------------------------------------------------
# Frame broadcast (worker thread -> any number of HTTP stream consumers)
# ---------------------------------------------------------------------------

class FrameBroadcaster:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._jpeg: Optional[bytes] = None
        self.seq = 0

    def publish(self, jpeg: bytes) -> None:
        with self._cond:
            self._jpeg = jpeg
            self.seq += 1
            self._cond.notify_all()

    def wait(self, last_seq: int, timeout: float = 0.5) -> Tuple[Optional[bytes], int]:
        with self._cond:
            if self.seq == last_seq:
                self._cond.wait(timeout)
            if self.seq == last_seq:
                return None, last_seq
            return self._jpeg, self.seq

    def latest(self) -> Optional[bytes]:
        with self._cond:
            return self._jpeg


# ---------------------------------------------------------------------------
# Tracker factory
# ---------------------------------------------------------------------------

def make_tracker(kind: str, fps: float) -> Any:
    from ultralytics.utils.checks import check_yaml

    cfg = yaml.safe_load(open(check_yaml(f"{kind}.yaml"), encoding="utf-8"))
    args = SimpleNamespace(**cfg)
    if kind == "botsort":
        from ultralytics.trackers.bot_sort import BOTSORT as cls

        args.with_reid = False  # ReID needs an extra model; keep it lightweight
    else:
        from ultralytics.trackers.byte_tracker import BYTETracker as cls

    # Older Ultralytics releases take ``frame_rate``; newer ones only ``args``.
    if "frame_rate" in inspect.signature(cls.__init__).parameters:
        return cls(args=args, frame_rate=max(int(round(fps)), 1))
    return cls(args=args)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class Pipeline:
    MAX_EVENTS = 250_000

    def __init__(self, models: ModelManager, config: ConfigStore, broadcaster: FrameBroadcaster) -> None:
        self.models = models
        self.config = config
        self.out = broadcaster

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._resume = threading.Event()
        self._resume.set()
        self._lock = threading.Lock()  # guards telemetry fields below
        self._reset_req = threading.Event()

        self.counter = LineCounter()
        self.events: Deque[Dict[str, Any]] = deque(maxlen=self.MAX_EVENTS)
        self._flash: Dict[str, int] = {}

        self.state = "idle"  # idle | starting | running | paused | completed | stopped | error
        self.source_label: Optional[str] = None
        self.error: Optional[str] = None
        self.frame_idx = 0
        self.total_frames: Optional[int] = None
        self.src_fps = 0.0
        self.fps = 0.0
        self.infer_ms = 0.0
        self.started_at: Optional[float] = None
        self.model_name: Optional[str] = None
        self.resolution: Tuple[int, int] = (0, 0)

    # ------------------------------------------------------------- controls
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, source: FrameSource) -> None:
        if self.running:
            raise RuntimeError("pipeline already running")
        model, _, _ = self.models.get()
        if model is None:
            raise RuntimeError("no model loaded - select weights in Studio first")
        self._stop.clear()
        self._resume.set()
        with self._lock:
            self.counter.reset_all()
            self.events.clear()
            self._flash.clear()
            self.state, self.error = "starting", None
            self.frame_idx, self.fps, self.infer_ms = 0, 0.0, 0.0
            self.source_label = source.label
            self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, args=(source,), name="trackline-pipeline", daemon=True)
        self._thread.start()

    def stop(self, wait: float = 5.0) -> None:
        self._stop.set()
        self._resume.set()
        if self._thread is not None:
            self._thread.join(timeout=wait)

    def pause(self) -> None:
        if self.running:
            self._resume.clear()
            with self._lock:
                self.state = "paused"

    def resume(self) -> None:
        if self.running:
            self._resume.set()
            with self._lock:
                self.state = "running"

    def reset_counts(self) -> None:
        # Applied by the worker between frames to avoid racing the counter.
        if self.running:
            self._reset_req.set()
        else:
            with self._lock:
                self.counter.reset_all()
                self.events.clear()

    # ----------------------------------------------------------- worker loop
    def _run(self, source: FrameSource) -> None:
        tracker = None
        tracker_key: Tuple[Any, ...] = ()
        t_last = time.perf_counter()
        fps_ema = 0.0
        try:
            source.open()
            with self._lock:
                self.total_frames = source.total_frames
                self.src_fps = source.fps
                self.resolution = (source.width, source.height)
                self.state = "running"
            frame_period = 1.0 / source.fps if source.fps > 0 else 0.0

            while not self._stop.is_set():
                if not self._resume.is_set():
                    self._resume.wait(0.25)
                    continue
                t_frame = time.perf_counter()

                ok, frame = source.read()
                if not ok or frame is None:
                    if source.is_live:
                        continue
                    with self._lock:
                        self.state = "completed"
                    break

                cfg: EngineConfig = self.config.get()
                model, model_name, gen = self.models.get()

                # Reset tracker on model swap or tracker-type change.
                key = (gen, cfg.tracker)
                if tracker is None or key != tracker_key:
                    tracker = make_tracker(cfg.tracker, source.fps)
                    tracker_key = key
                    self.counter.reset_tracks()

                if self._reset_req.is_set():
                    self._reset_req.clear()
                    with self._lock:
                        self.counter.reset_all()
                        self.events.clear()

                h, w = frame.shape[:2]
                names = self.models.names
                valid_classes = [c for c in cfg.classes if c in names] or None

                t0 = time.perf_counter()
                result = model.predict(
                    frame, conf=cfg.conf, iou=cfg.iou, imgsz=cfg.imgsz, classes=valid_classes,
                    device=self.models.device, verbose=False,
                )[0]
                det = result.boxes.cpu().numpy()
                tracks = np.asarray(tracker.update(det, frame), dtype=np.float32)
                if tracks.ndim != 2:
                    tracks = tracks.reshape(0, 8)
                infer_ms = (time.perf_counter() - t0) * 1000

                lines = [
                    LineSpec(l.id, l.name, (l.p1[0] * w, l.p1[1] * h), (l.p2[0] * w, l.p2[1] * h))
                    for l in cfg.lines
                ]
                dets: List[Detection] = []
                boxes: List[Tuple[np.ndarray, int, int]] = []
                for row in tracks:
                    x1, y1, x2, y2, tid, _score, cls = row[:7]
                    tid, cls = int(tid), int(cls)
                    cy = (y1 + y2) / 2 if cfg.anchor == "center" else y2
                    dets.append(Detection(tid, cls, ((x1 + x2) / 2, cy)))
                    boxes.append((row[:4], tid, cls))

                with self._lock:
                    new_events = self.counter.update(self.frame_idx, lines, dets)
                    self._record(new_events, names, source.fps)

                self._draw(frame, lines, boxes, dets, names)
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    self.out.publish(buf.tobytes())

                now = time.perf_counter()
                inst = 1.0 / max(now - t_last, 1e-6)
                fps_ema = inst if fps_ema == 0 else fps_ema * 0.9 + inst * 0.1
                t_last = now
                with self._lock:
                    self.frame_idx += 1
                    self.fps = fps_ema
                    self.infer_ms = infer_ms
                    self.model_name = model_name

                if cfg.realtime and frame_period:
                    slack = frame_period - (time.perf_counter() - t_frame)
                    if slack > 0:
                        time.sleep(slack)
            else:
                with self._lock:
                    if self.state not in ("completed", "error"):
                        self.state = "stopped"
        except Exception as exc:  # surface any failure to the UI
            with self._lock:
                self.state = "error"
                self.error = f"{type(exc).__name__}: {exc}"
        finally:
            source.close()

    def _record(self, events: List[CrossingEvent], names: Dict[int, str], fps: float) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        for e in events:
            self._flash[e.line_id] = 8
            self.events.append({
                "timestamp": now,
                "frame": e.frame,
                "video_time_s": round(e.frame / fps, 3) if fps else None,
                "track_id": e.track_id,
                "class_id": e.cls,
                "class_name": names.get(e.cls, str(e.cls)),
                "line_id": e.line_id,
                "line_name": e.line_name,
                "direction": e.direction,
            })

    # ---------------------------------------------------------------- overlay
    def _draw(self, img: np.ndarray, lines: List[LineSpec], boxes, dets: List[Detection], names) -> None:
        h, w = img.shape[:2]
        s = max(w, h) / 1280.0
        th = max(1, int(round(2 * s)))
        font = cv2.FONT_HERSHEY_SIMPLEX
        fs = 0.5 * s

        for (xyxy, tid, cls), d in zip(boxes, dets):
            x1, y1, x2, y2 = map(int, xyxy)
            cv2.rectangle(img, (x1, y1), (x2, y2), WHITE, th)
            label = f"#{tid} {names.get(cls, cls)}"
            (tw, tht), _ = cv2.getTextSize(label, font, fs, 1)
            cv2.rectangle(img, (x1, y1 - tht - 6), (x1 + tw + 6, y1), BLACK, -1)
            cv2.putText(img, label, (x1 + 3, y1 - 4), font, fs, WHITE, 1, cv2.LINE_AA)
            cv2.circle(img, (int(d.point[0]), int(d.point[1])), th + 2, WHITE, -1)

        per_line = self.counter.per_line()
        for ln in lines:
            a = (int(ln.a[0]), int(ln.a[1]))
            b = (int(ln.b[0]), int(ln.b[1]))
            flashing = self._flash.get(ln.id, 0) > 0
            if flashing:
                self._flash[ln.id] -= 1
            color = AMBER if flashing else WHITE
            cv2.line(img, a, b, BLACK, th * 4, cv2.LINE_AA)
            cv2.line(img, a, b, color, th * 2, cv2.LINE_AA)
            # "IN" direction arrow from the midpoint along the positive normal.
            nx, ny = normal(ln.a, ln.b)
            mx, my = (ln.a[0] + ln.b[0]) / 2, (ln.a[1] + ln.b[1]) / 2
            tip = (int(mx + nx * 28 * s), int(my + ny * 28 * s))
            cv2.arrowedLine(img, (int(mx), int(my)), tip, color, th, cv2.LINE_AA, tipLength=0.35)
            c = per_line.get(ln.id, {"in": 0, "out": 0})
            label = f"{ln.name}  IN {c['in']}  OUT {c['out']}"
            (tw, tht), _ = cv2.getTextSize(label, font, fs, 1)
            org = (min(max(a[0], 4), w - tw - 8), max(a[1] - 10, tht + 8))
            cv2.rectangle(img, (org[0] - 4, org[1] - tht - 6), (org[0] + tw + 4, org[1] + 4), BLACK, -1)
            cv2.putText(img, label, org, font, fs, color, 1, cv2.LINE_AA)

        t = self.counter.totals()
        hud = f"FPS {self.fps:5.1f}  FRAME {self.frame_idx}  IN {t['in']}  OUT {t['out']}"
        (tw, tht), _ = cv2.getTextSize(hud, font, fs, 1)
        cv2.rectangle(img, (0, 0), (tw + 16, tht + 14), BLACK, -1)
        cv2.putText(img, hud, (8, tht + 6), font, fs, WHITE, 1, cv2.LINE_AA)

    # -------------------------------------------------------------- reporting
    def snapshot(self, recent: int = 20) -> Dict[str, Any]:
        with self._lock:
            names = self.models.names
            per_class = self.counter.per_class()
            cfg = self.config.get()
            line_names = {l.id: l.name for l in cfg.lines}
            per_line = self.counter.per_line()
            elapsed = time.time() - self.started_at if self.started_at else 0.0
            progress = (self.frame_idx / self.total_frames) if self.total_frames else None
            return {
                "state": self.state,
                "error": self.error,
                "source": self.source_label,
                "model": self.model_name,
                "fps": round(self.fps, 2),
                "infer_ms": round(self.infer_ms, 1),
                "src_fps": round(self.src_fps, 2),
                "resolution": list(self.resolution),
                "frame": self.frame_idx,
                "total_frames": self.total_frames,
                "progress": round(progress, 4) if progress is not None else None,
                "elapsed_s": round(elapsed, 1),
                "active_tracks": self.counter.active_tracks,
                "totals": self.counter.totals(),
                "classes": sorted(
                    (
                        {"id": c, "name": names.get(c, str(c)), "in": v["in"], "out": v["out"], "total": v["in"] + v["out"]}
                        for c, v in per_class.items()
                    ),
                    key=lambda r: -r["total"],
                ),
                "lines": [
                    {"id": lid, "name": line_names.get(lid, lid), **v} for lid, v in per_line.items()
                ],
                "event_count": len(self.events),
                "recent_events": list(self.events)[-recent:][::-1],
            }

    CSV_FIELDS = ["timestamp", "frame", "video_time_s", "track_id", "class_id", "class_name", "line_id", "line_name", "direction"]

    def events_csv(self) -> str:
        with self._lock:
            rows = list(self.events)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=self.CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue()
