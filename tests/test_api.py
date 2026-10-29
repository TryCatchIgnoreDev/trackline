"""API contract tests for FastAPI routes & handlers (config, models, live stub, CSV)."""

import asyncio

import pytest
from fastapi import HTTPException

from app.main import (
    ConfigPatch,
    LiveConnect,
    get_config,
    get_models,
    health,
    live_connect,
    live_status,
    put_config,
    run_events_csv,
)


def test_health_and_models():
    h = health()
    assert h["ok"] is True
    m = get_models()
    assert "available" in m


def test_config_roundtrip():
    orig = get_config()["config"]
    patch = ConfigPatch(
        conf=0.42,
        iou=0.55,
        tracker="bytetrack",
        lines=[{"id": "t1", "name": "gate-1", "p1": (0.1, 0.2), "p2": (0.9, 0.8)}],
    )
    updated = put_config(patch)["config"]
    assert updated["conf"] == 0.42
    assert updated["lines"][0]["name"] == "gate-1"

    put_config(
        ConfigPatch(
            conf=orig["conf"],
            iou=orig["iou"],
            tracker=orig["tracker"],
            lines=orig["lines"],
        )
    )


def test_live_stub_contract():
    st = live_status()
    assert st["enabled"] is False
    assert st["state"] == "not_implemented"

    with pytest.raises(HTTPException) as bad:
        live_connect(LiveConnect(uri="not-a-valid-uri"))
    assert bad.value.status_code == 422

    with pytest.raises(HTTPException) as stub:
        live_connect(LiveConnect(uri="rtsp://127.0.0.1:554/stream"))
    assert stub.value.status_code == 501


def test_events_csv_headers():
    resp = run_events_csv()
    assert resp.status_code == 200
    assert "text/csv" in resp.media_type
    first_line = resp.body.decode().splitlines()[0]
    assert first_line == "timestamp,frame,video_time_s,track_id,class_id,class_name,line_id,line_name,direction"
