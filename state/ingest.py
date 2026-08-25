"""Ingest liên tục vào vector DB của region chính. [CÓ SẴN]

Không có ingest liên tục thì RPO luôn = 0 và cả khái niệm RPO thành vô nghĩa.
Chạy song song với drill:  python state/ingest.py --region a --rate 0.5 --duration 300
"""
import argparse
import sqlite3
import time

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--region", default="a")
    p.add_argument("--rate", type=float, default=0.5, help="doc/giay")
    p.add_argument("--duration", type=float, default=300)
    a = p.parse_args()
    con = sqlite3.connect(f"state/region-{a.region}/vectors.sqlite")
    end, i = time.time() + a.duration, 0
    while time.time() < end:
        t = time.time()
        con.execute("INSERT OR REPLACE INTO docs VALUES (?,?,?,?)",
                    (f"live-{a.region}-{int(t*1000)}",
                     f"Ticket live #{i}: khach moi hoi luc {time.strftime('%H:%M:%S')}",
                     bytes([i % 256]) * 32, t))
        con.commit()
        i += 1
        time.sleep(max(0.0, 1.0 / a.rate - (time.time() - t)))
    con.close()
    print(f"ingested {i} live docs vào region-{a.region}")
