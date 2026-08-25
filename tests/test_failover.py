"""Test đơn vị cho 3 file sinh viên viết. Chạy được KHÔNG cần server đang chạy."""
import itertools
import json
import pathlib
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402


def _db(tmp, ts_list):
    tmp.mkdir(parents=True, exist_ok=True)
    p = tmp / "v.sqlite"
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE docs(doc_id TEXT PRIMARY KEY, body TEXT, embedding BLOB, ingested_at REAL);")
    con.executemany("INSERT INTO docs VALUES (?,?,?,?)",
                    [(f"d{i}", "x", b"e", t) for i, t in enumerate(ts_list)])
    con.commit()
    con.close()
    return p


def test_rpo_dem_dung_so_doc_bi_mat(tmp_path):
    """RPO không phải 'tuổi của snapshot' — là dữ liệu primary có mà bản restore không có."""
    prim = _db(tmp_path / "a", [100, 110, 120, 130])
    rest = _db(tmp_path / "b", [100, 110])
    r = snapshot.rpo(prim, rest)
    assert r["rpo_seconds"] == 20.0
    assert r["docs_lost"] == 2


def test_health_checker_can_threshold_lien_tiep(monkeypatch):
    """1 lần fail KHÔNG phải outage. Chống flapping là yêu cầu, không phải tuỳ chọn."""
    hc = pytest.importorskip("dr.health_checker")
    # True/False xen ke roi fail lien tiep mai mai -> checker phai chiu duoc ca 2 giai doan
    seq = itertools.chain([True, True, False, True, False], itertools.repeat(False))
    monkeypatch.setattr(hc, "probe", lambda r, t: (next(seq), "mock"))
    out = pathlib.Path("reports/_test-health.jsonl")
    out.unlink(missing_ok=True)
    hc.run(interval=0.01, timeout=0.01, threshold=3, duration=0.2, out=out)
    ev = [json.loads(l) for l in out.read_text().splitlines()]
    changes = [e for e in ev if e.get("event") == "state_change"]
    assert changes, "khong ghi state_change nao"
    assert changes[0]["to"] == "UNHEALTHY"
    assert changes[0]["consecutive_fails"] >= 3


def test_failover_khong_cutover_khi_target_chua_ready(monkeypatch):
    """Đổi DNS trước khi region phụ ready = 503 từ cả hai phía.

    Test này PHẢI tự mô phỏng "target không bao giờ ready" bằng monkeypatch, không
    được dựa vào việc port 8002 tình cờ không có ai lắng nghe -- trong lab thật, stack
    bare mode gần như luôn đang chạy song song (Bước 0-4 đều cần nó sống), nên nếu test
    chỉ trông chờ ConnectError từ việc "không có server", nó sẽ liên hệ nhầm vào server
    THẬT đang chạy và cho kết quả sai tuỳ máy/tuỳ thời điểm.
    """
    fo = pytest.importorskip("dr.failover")
    active = pathlib.Path("edge/active_region")
    before = active.read_text() if active.exists() else "a"
    monkeypatch.setattr(fo.snapshot, "get", lambda *a, **k: {
        "snapshot_at": time.time(), "latest_doc_ts": time.time(),
        "embed_model_version": "test"})
    # `state_of` la mot helper NOI BO minh dat ten khi viet reference — docstring cua
    # dr/failover.py khong bat buoc ban phai co ham nay voi dung ten do. Chi patch neu
    # no ton tai, de test khong fail oan mot implementation dung nhung to chuc code khac.
    if hasattr(fo, "state_of"):
        monkeypatch.setattr(fo, "state_of", lambda r: {"region": r, "pool_state": "warm"})
    if hasattr(fo, "httpx"):
        def never_ready(*a, **k):
            raise fo.httpx.ConnectError("mocked: target khong bao gio ready")
        monkeypatch.setattr(fo.httpx, "get", never_ready)
    r = fo.failover("b", "fs", wait=1.0)
    assert not r.get("ok")
    assert (active.read_text() if active.exists() else "a") == before, \
        "da doi active_region du target chua ready"
