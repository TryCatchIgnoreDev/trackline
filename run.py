"""Entry point: ``python run.py [--host 0.0.0.0] [--port 8000]``."""

import argparse

import uvicorn

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Trackline video analytics engine")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true", help="dev auto-reload")
    a = ap.parse_args()
    uvicorn.run("app.main:app", host=a.host, port=a.port, reload=a.reload, log_level="info")
