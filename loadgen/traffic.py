"""Load generator: 2 req/s vào edge, ghi MỖI request 1 dòng JSONL. [CÓ SẴN]

Đây là ĐỒNG HỒ của bài lab. RTO đo được = khoảng giữa 2 timestamp trong file này,
không phải cảm giác của bạn. Không có file này -> không có bằng chứng -> trượt.

    python loadgen/traffic.py --duration 300 --rps 2 --out reports/drill-2.jsonl
"""
import argparse
import json
import pathlib
import time

import httpx

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8080/v1/infer")
    p.add_argument("--rps", type=float, default=2.0)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--timeout", type=float, default=3.0)
    p.add_argument("--out", required=True)
    a = p.parse_args()

    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    period, end, n = 1.0 / a.rps, time.time() + a.duration, 0
    with out.open("w") as f, httpx.Client(timeout=a.timeout) as c:
        while time.time() < end:
            t0 = time.time()
            rec = {"seq": n, "ts": t0}
            try:
                r = c.get(a.url, params={"q": f"hoa don thang {n % 12 + 1}"})
                body = r.json()
                rec.update(status=r.status_code, ok=r.status_code == 200,
                           served_by=body.get("region"), edge_region=body.get("edge_region"),
                           doc_ingested_at=body.get("doc_ingested_at"),
                           error=body.get("error"))
            except Exception as e:
                rec.update(status=None, ok=False, error=type(e).__name__)
            rec["latency_ms"] = round((time.time() - t0) * 1000, 1)
            f.write(json.dumps(rec) + "\n")
            f.flush()
            n += 1
            time.sleep(max(0.0, period - (time.time() - t0)))
    print(f"{n} requests -> {out}")
