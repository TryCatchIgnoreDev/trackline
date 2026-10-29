"""Download starter assets: YOLO weights into ./models and a sample clip into ./videos.

Usage:
    python scripts/fetch_assets.py                 # yolo11n.pt + sample video
    python scripts/fetch_assets.py --model yolo11s.pt --no-video
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_VIDEO = "https://github.com/intel-iot-devkit/sample-videos/raw/master/person-bicycle-car-detection.mp4"


def fetch_model(name: str) -> None:
    dest = ROOT / "models" / name
    if dest.exists():
        print(f"[skip] {dest.relative_to(ROOT)} exists")
        return
    from ultralytics.utils.downloads import attempt_download_asset

    path = Path(attempt_download_asset(name))  # downloads into CWD (or cache)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), dest)
    print(f"[ok]   {dest.relative_to(ROOT)}")


def fetch_video(url: str) -> None:
    dest = ROOT / "videos" / Path(url).name
    if dest.exists():
        print(f"[skip] {dest.relative_to(ROOT)} exists")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"[get]  {url}")
    with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh)
    print(f"[ok]   {dest.relative_to(ROOT)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="yolo11n.pt")
    ap.add_argument("--video", default=SAMPLE_VIDEO)
    ap.add_argument("--no-video", action="store_true")
    a = ap.parse_args()
    try:
        fetch_model(a.model)
        if not a.no_video:
            fetch_video(a.video)
    except Exception as e:
        sys.exit(f"[err]  {e}")
