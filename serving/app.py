"""Mock inference API cho 1 "region". [CÓ SẴN — không sửa]

Mỗi region chạy 1 instance với REGION=a|b và cổng khác nhau.
Serving là stateless, nhưng nó *đọc* 2 loại state:
  - vector DB   : state/region-<r>/vectors.sqlite  (§3 Vector DB Backup)
  - model weight: state/region-<r>/weights/model.bin (§2 Cross-Region Replication)

Pool state (§4 GPU Pool Warm-Up) đọc từ file state/region-<r>/pool_state:
  cold | warm | full   -- /readyz chỉ 200 khi "full" VÀ đã hết warmup.
"""
import json
import os
import pathlib
import sqlite3
import time

from fastapi import FastAPI, Response

REGION = os.environ.get("REGION", "a")
STATE = pathlib.Path(os.environ.get("STATE_DIR", f"state/region-{REGION}"))
WARMUP = float(os.environ.get("WARMUP_SECONDS", "6"))
MIN_VECTORS = int(os.environ.get("MIN_VECTORS", "1"))

app = FastAPI(title=f"serving-region-{REGION}")
_scale = {"state": None, "since": 0.0}


def pool_state() -> str:
    f = STATE / "pool_state"
    s = f.read_text().strip() if f.exists() else "cold"
    if s != _scale["state"]:
        # Lần đọc đầu tiên = trạng thái lúc boot -> không tính warmup.
        # Chỉ transition warm->full LÚC CHẠY mới phải chờ (mô phỏng §4 GPU pool warm-up).
        first = _scale["state"] is None
        _scale["state"] = s
        _scale["since"] = 0.0 if first else time.time()
    return s


def vector_stats() -> dict:
    db = STATE / "vectors.sqlite"
    if not db.exists():
        return {"count": 0, "latest_doc_ts": None}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        n, ts = con.execute("SELECT COUNT(*), MAX(ingested_at) FROM docs").fetchone()
    finally:
        con.close()
    return {"count": n or 0, "latest_doc_ts": ts}


def weights_ok() -> bool:
    return (STATE / "weights" / "model.bin").exists()


@app.get("/healthz")
def healthz():
    """Liveness: process còn sống. KHÔNG kiểm tra state -> đừng failover dựa vào cái này."""
    return {"region": REGION, "alive": True, "ts": time.time()}


@app.get("/readyz")
def readyz(response: Response):
    """Readiness: region này serve được traffic thật hay chưa."""
    ps = pool_state()
    warm_left = max(0.0, (_scale["since"] + WARMUP) - time.time())
    v = vector_stats()
    reasons = []
    if ps != "full":
        reasons.append(f"pool_state={ps}")
    if warm_left > 0:
        reasons.append(f"warming_up_{warm_left:.1f}s_left")
    if not weights_ok():
        reasons.append("model_weights_missing")
    if v["count"] < MIN_VECTORS:
        reasons.append(f"vector_db_empty(count={v['count']})")
    body = {"region": REGION, "ready": not reasons, "reasons": reasons,
            "pool_state": ps, "vectors": v, "ts": time.time()}
    response.status_code = 200 if not reasons else 503
    return body


@app.get("/v1/state")
def state():
    return {"region": REGION, "pool_state": pool_state(), "weights": weights_ok(),
            **vector_stats(), "ts": time.time()}


@app.post("/v1/infer")
@app.get("/v1/infer")
def infer(q: str = "hoa don thang 7", response: Response = None):
    """Mock inference: retrieve top-1 doc từ vector DB rồi "sinh" câu trả lời."""
    r = readyz(response)
    if not r["ready"]:
        return {"error": "region_not_ready", "reasons": r["reasons"], "region": REGION}
    db = STATE / "vectors.sqlite"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT doc_id, body, ingested_at FROM docs ORDER BY ABS(LENGTH(body)-?) LIMIT 1",
            (len(q),),
        ).fetchone()
    finally:
        con.close()
    return {"region": REGION, "answer": f"[{REGION}] {row[1][:60]}",
            "doc_id": row[0], "doc_ingested_at": row[2], "ts": time.time()}
