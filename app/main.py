"""FastAPI application: REST + MJPEG + WebSocket telemetry.

Blocking work (model loading, video decoding, git) is always pushed to worker
threads: either explicitly via ``asyncio.to_thread`` or implicitly by
declaring the route as a plain ``def`` (Starlette runs those in a threadpool).
The inference loop lives on its own thread inside ``Pipeline``.
"""

from __future__ import annotations

import asyncio
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, gitsync, live
from .config import ROOT, VIDEOS_DIR, ConfigStore, Line
from .models import ModelManager
from .pipeline import FrameBroadcaster, Pipeline
from .sources import VIDEO_EXTS, FrameSeeker, VideoFileSource, list_videos, resolve_video

STATIC_DIR = ROOT / "static"

config = ConfigStore()
models = ModelManager()
broadcaster = FrameBroadcaster()
pipeline = Pipeline(models, config, broadcaster)
seeker = FrameSeeker()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Restore the last used model in the background; the UI shows "loading".
    cfg = config.get()
    if cfg.model:
        try:
            models.resolve(cfg.model)
            asyncio.get_running_loop().run_in_executor(None, _safe_load, cfg.model)
        except FileNotFoundError:
            pass
    yield
    pipeline.stop()
    seeker.release()


def _safe_load(name: str) -> None:
    try:
        models.load(name)
    except Exception:
        pass  # error is recorded on the manager and surfaced via /api/models


app = FastAPI(title="Trackline", version=__version__, lifespan=lifespan)


# ---------------------------------------------------------------- schemas
class ModelSelect(BaseModel):
    name: str


class ConfigPatch(BaseModel):
    model_config = {"extra": "forbid"}

    video: Optional[str] = None
    conf: Optional[float] = None
    iou: Optional[float] = None
    imgsz: Optional[int] = None
    classes: Optional[List[int]] = None
    tracker: Optional[str] = None
    anchor: Optional[str] = None
    realtime: Optional[bool] = None
    lines: Optional[List[Line]] = None
    git_autocommit: Optional[bool] = None


class SaveRequest(BaseModel):
    commit: Optional[bool] = None  # defaults to config.git_autocommit
    message: Optional[str] = None


class RunRequest(BaseModel):
    video: Optional[str] = None


class LiveConnect(BaseModel):
    uri: str


# ----------------------------------------------------------------- system
@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "version": __version__, "device": models.device}


# ----------------------------------------------------------------- models
@app.get("/api/models")
def get_models() -> Dict[str, Any]:
    return {"available": models.scan(), **models.info()}


@app.post("/api/models/select")
async def select_model(req: ModelSelect) -> Dict[str, Any]:
    try:
        models.resolve(req.name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    try:
        info = await asyncio.to_thread(models.load, req.name)
    except Exception as e:
        raise HTTPException(500, f"failed to load {req.name}: {e}")
    config.update({"model": req.name})
    return info


# ----------------------------------------------------------------- config
@app.get("/api/config")
def get_config() -> Dict[str, Any]:
    return {"config": config.get().model_dump(mode="json"), "version": config.version}


@app.put("/api/config")
def put_config(patch: ConfigPatch) -> Dict[str, Any]:
    data = patch.model_dump(exclude_unset=True, mode="json")
    try:
        cfg = config.update(data)
    except Exception as e:
        raise HTTPException(422, str(e))
    return {"config": cfg.model_dump(mode="json"), "version": config.version}


@app.post("/api/config/save")
async def save_config(req: SaveRequest) -> Dict[str, Any]:
    path = await asyncio.to_thread(config.save)
    cfg = config.get()
    result: Dict[str, Any] = {"saved": str(path.name), "git": None}
    if req.commit if req.commit is not None else cfg.git_autocommit:
        msg = req.message or (
            f"config: {len(cfg.lines)} line(s), model={cfg.model}, conf={cfg.conf:.2f} "
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}]"
        )
        result["git"] = await asyncio.to_thread(gitsync.commit_file, path, msg)
    return result


# ----------------------------------------------------------------- videos
@app.get("/api/videos")
def get_videos() -> Dict[str, Any]:
    return {"videos": list_videos()}


@app.post("/api/videos/upload")
def upload_video(file: UploadFile = File(...)) -> Dict[str, Any]:
    name = Path(file.filename or "").name
    if not name or Path(name).suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(400, f"unsupported file type; allowed: {sorted(VIDEO_EXTS)}")
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    dest = VIDEOS_DIR / name
    tmp = dest.with_suffix(dest.suffix + ".part")
    with tmp.open("wb") as fh:
        shutil.copyfileobj(file.file, fh, length=4 * 1024 * 1024)
    seeker.release(dest.resolve())
    tmp.replace(dest)
    return {"name": name, "videos": list_videos()}


def _video(name: str) -> Path:
    try:
        return resolve_video(name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@app.get("/api/videos/{name}/meta")
def video_meta(name: str) -> Dict[str, Any]:
    try:
        return seeker.meta(_video(name))
    except RuntimeError as e:
        raise HTTPException(422, str(e))


@app.get("/api/videos/{name}/frame")
def video_frame(name: str, index: int = 0) -> Response:
    try:
        jpeg, idx = seeker.frame_jpeg(_video(name), index)
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    return Response(jpeg, media_type="image/jpeg", headers={"X-Frame-Index": str(idx), "Cache-Control": "no-store"})


# -------------------------------------------------------------- execution
@app.post("/api/run/start")
def run_start(req: RunRequest) -> Dict[str, Any]:
    name = req.video or config.get().video
    if not name:
        raise HTTPException(400, "no video selected")
    path = _video(name)
    config.update({"video": name})
    try:
        pipeline.start(VideoFileSource(path))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return pipeline.snapshot()


@app.post("/api/run/stop")
def run_stop() -> Dict[str, Any]:
    pipeline.stop()
    return pipeline.snapshot()


@app.post("/api/run/pause")
def run_pause() -> Dict[str, Any]:
    pipeline.pause()
    return pipeline.snapshot()


@app.post("/api/run/resume")
def run_resume() -> Dict[str, Any]:
    pipeline.resume()
    return pipeline.snapshot()


@app.post("/api/run/reset")
def run_reset() -> Dict[str, Any]:
    pipeline.reset_counts()
    return pipeline.snapshot()


@app.get("/api/run/status")
def run_status() -> Dict[str, Any]:
    return pipeline.snapshot()


@app.get("/api/run/events.csv")
def run_events_csv() -> PlainTextResponse:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return PlainTextResponse(
        pipeline.events_csv(), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="trackline-events-{stamp}.csv"'},
    )


@app.get("/api/run/frame.jpg")
def run_last_frame() -> Response:
    jpeg = broadcaster.latest()
    if jpeg is None:
        raise HTTPException(404, "no frame yet")
    return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/run/stream.mjpg")
async def run_stream(request: Request) -> StreamingResponse:
    async def gen():
        last = -1
        # Replay the latest frame immediately so a (re)connecting client isn't blank.
        jpeg = broadcaster.latest()
        if jpeg is not None:
            last = broadcaster.seq
            yield _part(jpeg)
        while not await request.is_disconnected():
            jpeg, seq = await asyncio.to_thread(broadcaster.wait, last, 0.5)
            if jpeg is None:
                continue
            last = seq
            yield _part(jpeg)

    return StreamingResponse(
        gen(), media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _part(jpeg: bytes) -> bytes:
    return b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"


@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            await ws.send_json({"run": pipeline.snapshot(), "model": models.info()})
            await asyncio.sleep(0.25)
    except (WebSocketDisconnect, RuntimeError):
        pass


# ------------------------------------------------------------------- live
@app.get("/api/live/status")
def live_status() -> Dict[str, Any]:
    return live.status()


@app.post("/api/live/connect")
def live_connect(req: LiveConnect) -> Dict[str, Any]:
    try:
        kind, _ = live.LiveStreamSource.parse(req.uri)
    except ValueError as e:
        raise HTTPException(422, str(e))
    raise HTTPException(501, f"live {kind} input is not implemented yet")


# ----------------------------------------------------------------- static
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
