"""Vòng lặp replication: snapshot region chính lên object store mỗi N giây. [CÓ SẴN]

N chính là RPO trên lý thuyết của bạn (§3 Backup Schedule Cheatsheet: vector DB mỗi 6h).
Đặt N=30 để trong 2h lab vẫn thấy được lag thật.

    python state/replicate.py --every 30 --duration 300 --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

LOG = pathlib.Path("reports/replication.jsonl")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--every", type=float, default=30)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--region", default="a")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    a = p.parse_args()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    end = time.time() + a.duration
    with LOG.open("a") as f:
        while time.time() < end:
            t = time.time()
            m = snapshot.put(a.region, a.backend)
            m["every_s"] = a.every
            f.write(json.dumps(m) + "\n")
            f.flush()
            print("REPLICATE", json.dumps(m))
            time.sleep(max(0.0, a.every - (time.time() - t)))
