"""API contract tests for FastAPI routes (config, models, live stub, CSV)."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_and_models():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True

    m = client.get("/api/models")
    assert m.status_code == 200
    assert "available" in m.json()


def test_config_roundtrip():
    r = client.get("/api/config")
    assert r.status_code == 200
    orig = r.json()["config"]

    patch = {
        "conf": 0.42,
        "iou": 0.55,
        "tracker": "bytetrack",
        "lines": [{"id": "t1", "name": "gate-1", "p1": [0.1, 0.2], "p2": [0.9, 0.8]}],
    }
    u = client.put("/api/config", json=patch)
    assert u.status_code == 200
    updated = u.json()["config"]
    assert updated["conf"] == 0.42
    assert updated["lines"][0]["name"] == "gate-1"

    # restore original
    client.put("/api/config", json={"conf": orig["conf"], "iou": orig["iou"], "tracker": orig["tracker"], "lines": orig["lines"]})


def test_live_stub_contract():
    st = client.get("/api/live/status")
    assert st.status_code == 200
    assert st.json()["enabled"] is False
    assert st.json()["state"] == "not_implemented"

    bad = client.post("/api/live/connect", json={"uri": "not-a-valid-uri"})
    assert bad.status_code == 422

    stub = client.post("/api/live/connect", json={"uri": "rtsp://127.0.0.1:554/stream"})
    assert stub.status_code == 501


def test_events_csv_headers():
    r = client.get("/api/run/events.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    first_line = r.text.splitlines()[0]
    assert first_line == "timestamp,frame,video_time_s,track_id,class_id,class_name,line_id,line_name,direction"
