# TRACKLINE

Modular, hot-swappable object counting and tracking engine.
**Ultralytics YOLO + OpenCV + FastAPI**, with a single-page vanilla JS/Canvas UI.

```
 STUDIO  ─ pick weights, scrub to any frame, draw counting lines, tune params, save config
 EXECUTE ─ tracked MJPEG output, live telemetry over WebSocket, CSV event export
 LIVE    ─ RTSP / webcam placeholder (stubbed backend + UI, ready for expansion)
```

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   |  source .venv/bin/activate  (Linux/macOS)
pip install -r requirements.txt
python scripts/fetch_assets.py     # yolo11n.pt -> models/, sample clip -> videos/
python run.py                      # http://127.0.0.1:8000
```

> GPU: `requirements.txt` installs whatever PyTorch build pip resolves (CPU on Windows).
> For CUDA, install torch first from https://pytorch.org/get-started/locally/. Device is auto-selected (`cuda` > `mps` > `cpu`).

Drop any `*.pt` detection weights into `models/` and any clips into `videos/` (or use **UPLOAD**).
Both folders are rescanned on demand, so you never need to restart.

## Workflow

1. **Studio** - choose a video, drag the scrubber (or `←/→`, `Shift` = 10 frames) to a representative frame.
   Drag on the canvas to draw a line. Drag handles to edit, drag the body to move, `Del` removes the selected line, and **FLIP** swaps the IN/OUT direction (the arrow points to IN).
2. Select weights in **MODEL**. Loading runs off the event loop; the active model swaps atomically.
3. Set confidence, IoU, tracker (ByteTrack / BoT-SORT), image size, anchor point and target classes (none checked = all classes).
4. **SAVE CONFIG** writes `config.yaml` atomically to disk.
5. **Execute** - press **START**. Edits made in Studio (lines, thresholds, classes, model, tracker) apply on the next frame of the running job.
   **EXPORT CSV** downloads the full crossing log.

## Architecture

```
app/
  main.py       FastAPI routes: REST, MJPEG stream, /ws/telemetry, static UI
  pipeline.py   worker thread: predict -> tracker.update -> LineCounter -> overlay -> JPEG broadcast
  counter.py    per-track / per-line crossing state machine
  geometry.py   pure 2D vector math (cross products, segment intersection)
  models.py     *.pt discovery + thread-safe hot swap (generation counter)
  config.py     pydantic schema, thread-safe store, atomic YAML save
  sources.py    FrameSource ABC, VideoFileSource, random-access FrameSeeker
  live.py       LiveStreamSource stub + roadmap
static/         index.html, app.css, app.js (no framework, no build step)
```

**Concurrency model**

| Work | Where it runs |
|---|---|
| Inference, tracking, counting, JPEG encode | dedicated `trackline-pipeline` daemon thread |
| Model load + warmup, config save | `asyncio.to_thread` |
| Frame seeking, uploads, CSV | sync `def` routes (Starlette threadpool) |
| MJPEG fan-out | `threading.Condition` broadcaster, awaited via `to_thread` with timeout |
| Telemetry | async WebSocket push at 4 Hz from locked snapshots |

Every frame, the worker reads the latest config snapshot and the active model reference.
If the model's `generation` or the tracker type changes, it creates a new tracker and clears the trajectory state. Counts are kept.
Trackers (`BYTETracker` / `BOTSORT`) are created directly instead of going through `model.track()`, so the pipeline controls their lifecycle.

**Line-crossing logic** (`app/counter.py`)

- Anchor = box centroid (or bottom-center, for objects on the ground plane).
- Side = sign of the signed perpendicular distance to the line, with a ±2 px deadband to absorb jitter.
- A crossing counts only when the confident side flips **and** the motion segment from the last confident position intersects the *finite* line segment, so objects walking past the line's end are not counted.
- `-1 → +1` = **IN**, `+1 → -1` = **OUT**. Each track is counted at most once per line per direction.
- Class = majority vote over the track's lifetime, which handles detector class flicker.
- Lines use normalized `[0,1]` coordinates, so they work at any resolution. Moving a line resets side history for that line only.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/models` | scan `models/`, active model state + class names |
| POST | `/api/models/select` | `{name}` hot-swap weights |
| GET/PUT | `/api/config` | read / patch live config |
| POST | `/api/config/save` | write `config.yaml` atomically |
| GET | `/api/videos` | list `videos/` |
| POST | `/api/videos/upload` | multipart upload |
| GET | `/api/videos/{name}/meta` | fps, frames, resolution |
| GET | `/api/videos/{name}/frame?index=N` | JPEG of frame N |
| POST | `/api/run/start\|stop\|pause\|resume\|reset` | job control |
| GET | `/api/run/status` | telemetry snapshot |
| GET | `/api/run/stream.mjpg` | annotated MJPEG stream |
| GET | `/api/run/events.csv` | crossing event log |
| WS | `/ws/telemetry` | snapshot push (run + model) |
| GET/POST | `/api/live/status`, `/api/live/connect` | live-mode stub (`501`) |

CSV columns: `timestamp, frame, video_time_s, track_id, class_id, class_name, line_id, line_name, direction`.

## Configuration

Paths can be overridden with `TRACKLINE_CONFIG`, `TRACKLINE_MODELS`, `TRACKLINE_VIDEOS`.
`config.yaml` is versioned in git. Weights and videos are git-ignored.

## Tests

```bash
pip install pytest
pytest -q
```

## License

The application code is yours to license. Note that **Ultralytics YOLO is AGPL-3.0**; commercial closed-source use needs an Ultralytics Enterprise license.
