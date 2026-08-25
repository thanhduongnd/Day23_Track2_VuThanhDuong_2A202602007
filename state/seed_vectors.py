"""Seed "vector DB" (SQLite) + model weights cho 1 region. [CÓ SẴN]

Vector DB thật (Qdrant/Weaviate) chỉ khác ở API — cái lab cần là: có state trên đĩa,
có timestamp ingest để đo RPO, và có snapshot copy được.

    python state/seed_vectors.py --region a --docs 200
    python state/seed_vectors.py --region b --docs 0     # region phụ khởi đầu RỖNG
"""
import argparse
import pathlib
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
  doc_id      TEXT PRIMARY KEY,
  body        TEXT NOT NULL,
  embedding   BLOB NOT NULL,
  ingested_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingested ON docs(ingested_at);
"""


def seed(region: str, n: int, weights_mb: int) -> pathlib.Path:
    d = pathlib.Path(f"state/region-{region}")
    (d / "weights").mkdir(parents=True, exist_ok=True)
    w = d / "weights" / "model.bin"
    if weights_mb and not w.exists():
        w.write_bytes(b"\x00" * (weights_mb * 1024 * 1024))  # "model weights" giả
        (d / "weights" / "VERSION").write_text("embed-model=vi-e5-base@v3\n")
    con = sqlite3.connect(d / "vectors.sqlite")
    con.executescript(SCHEMA)
    now = time.time()
    rows = [(f"{region}-doc-{i:04d}",
             f"Ticket #{i}: khach hoi ve hoa don thang {i % 12 + 1}, so tien {i * 1000}d",
             bytes([i % 256]) * 32, now - (n - i) * 0.5) for i in range(n)]
    con.executemany("INSERT OR REPLACE INTO docs VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    (d / "pool_state").write_text("full" if region == "a" else "warm")
    return d


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--region", required=True, choices=["a", "b"])
    p.add_argument("--docs", type=int, default=200)
    p.add_argument("--weights-mb", type=int, default=2,
                   help="0 = KHONG tao model weights (region phu khoi dau trong)")
    a = p.parse_args()
    d = seed(a.region, a.docs, a.weights_mb)
    print(f"seeded region-{a.region}: {a.docs} docs -> {d}")
