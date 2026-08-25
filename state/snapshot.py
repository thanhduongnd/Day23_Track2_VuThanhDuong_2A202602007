"""Snapshot vector DB + model weights lên object store. [CÓ SẴN]

Hai backend, cùng interface — đây là chỗ §2 "Cross-Region Model Weight Replication"
và §3 "Vector DB Backup -> S3" chạy được offline:

  --backend minio : S3-compatible thật (MinIO local, cần docker) qua boto3
  --backend fs    : thư mục state/_replica/ (dùng cho --mock / máy không có docker)

    python state/snapshot.py put --region a --backend fs
    python state/snapshot.py get --region b --backend fs   # restore vào region phụ
    python state/snapshot.py lag --backend fs              # RPO hiện tại (giây)
"""
import argparse
import json
import os
import pathlib
import shutil
import sqlite3
import time

BUCKET = os.environ.get("DR_BUCKET", "dr-artifacts")


def _fs_root() -> pathlib.Path:
    r = pathlib.Path("state/_replica") / BUCKET
    r.mkdir(parents=True, exist_ok=True)
    return r


def _s3():
    import boto3  # chỉ import khi thật sự dùng MinIO
    return boto3.client("s3", endpoint_url=os.environ.get("MINIO_URL", "http://127.0.0.1:9000"),
                        aws_access_key_id=os.environ.get("MINIO_USER", "minioadmin"),
                        aws_secret_access_key=os.environ.get("MINIO_PASS", "minioadmin"))


def latest_doc_ts(db: pathlib.Path):
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT MAX(ingested_at) FROM docs").fetchone()[0]
    finally:
        con.close()


def rpo(primary_db: pathlib.Path, restored_db: pathlib.Path) -> dict:
    """RPO thật = dữ liệu có ở primary mà bản restore KHÔNG có.

    Đo 2 cách, cả hai đều phải ghi vào evidence table:
      rpo_seconds : khoảng thời gian dữ liệu bị mất
      docs_lost   : số document bị mất (con số khách hàng quan tâm)
    """
    a_ts, b_ts = latest_doc_ts(primary_db), latest_doc_ts(restored_db)
    lost = None
    if b_ts is not None and primary_db.exists():
        con = sqlite3.connect(f"file:{primary_db}?mode=ro", uri=True)
        try:
            lost = con.execute("SELECT COUNT(*) FROM docs WHERE ingested_at > ?", (b_ts,)).fetchone()[0]
        finally:
            con.close()
    return {"primary_latest_doc_ts": a_ts, "restored_latest_doc_ts": b_ts,
            "rpo_seconds": None if (a_ts is None or b_ts is None) else round(a_ts - b_ts, 2),
            "docs_lost": lost}


def put(region: str, backend: str) -> dict:
    src = pathlib.Path(f"state/region-{region}")
    meta = {"snapshot_at": time.time(), "source_region": region,
            "latest_doc_ts": latest_doc_ts(src / "vectors.sqlite"),
            "embed_model_version": (src / "weights" / "VERSION").read_text().strip()}
    if backend == "fs":
        dst = _fs_root()
        shutil.copy2(src / "vectors.sqlite", dst / "vectors.sqlite")
        shutil.copy2(src / "weights" / "model.bin", dst / "model.bin")
        (dst / "MANIFEST.json").write_text(json.dumps(meta, indent=2))
    else:
        c = _s3()
        c.upload_file(str(src / "vectors.sqlite"), BUCKET, "vectors.sqlite")
        c.upload_file(str(src / "weights" / "model.bin"), BUCKET, "model.bin")
        c.put_object(Bucket=BUCKET, Key="MANIFEST.json", Body=json.dumps(meta).encode())
    return meta


def get(region: str, backend: str) -> dict:
    dst = pathlib.Path(f"state/region-{region}")
    (dst / "weights").mkdir(parents=True, exist_ok=True)
    if backend == "fs":
        src = _fs_root()
        manifest = src / "MANIFEST.json"
        if not manifest.exists():
            raise SystemExit(
                f"khong tim thay {manifest} -> chua tung co `put` nao chay. "
                f"Chay `python3 state/snapshot.py put --region a --backend fs` hoac "
                f"`python3 state/replicate.py --every 30 --duration <N> --backend fs` "
                f"TRUOC khi goi failover/get.")
        meta = json.loads(manifest.read_text())
        shutil.copy2(src / "vectors.sqlite", dst / "vectors.sqlite")
        shutil.copy2(src / "model.bin", dst / "weights" / "model.bin")
    else:
        c = _s3()
        meta = json.loads(c.get_object(Bucket=BUCKET, Key="MANIFEST.json")["Body"].read())
        c.download_file(BUCKET, "vectors.sqlite", str(dst / "vectors.sqlite"))
        c.download_file(BUCKET, "model.bin", str(dst / "weights" / "model.bin"))
    (dst / "weights" / "VERSION").write_text(meta["embed_model_version"] + "\n")
    meta["restored_at"] = time.time()
    return meta


def lag(backend: str) -> dict:
    a = latest_doc_ts(pathlib.Path("state/region-a/vectors.sqlite"))
    if backend == "fs":
        f = _fs_root() / "MANIFEST.json"
        snap = json.loads(f.read_text())["latest_doc_ts"] if f.exists() else None
    else:
        snap = json.loads(_s3().get_object(Bucket=BUCKET, Key="MANIFEST.json")["Body"].read())["latest_doc_ts"]
    return {"primary_latest_doc_ts": a, "snapshot_latest_doc_ts": snap,
            "rpo_seconds": None if (a is None or snap is None) else round(a - snap, 2)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["put", "get", "lag"])
    p.add_argument("--region", default="a", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    a = p.parse_args()
    out = {"put": lambda: put(a.region, a.backend), "get": lambda: get(a.region, a.backend),
           "lag": lambda: lag(a.backend)}[a.cmd]()
    print(json.dumps(out, indent=2))
