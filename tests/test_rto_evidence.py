"""Gate chấm điểm: evidence có thật hay không. Chạy: pytest tests/ -v

Không test "code đẹp". Test đúng một câu hỏi: những con số trong
reports/rto-evidence.md có truy được về dòng log thật không.
"""
import json
import pathlib
import re
import sys

import pytest

sys.path.insert(0, ".")
from tools.measure_rto import measure  # noqa: E402

R = pathlib.Path("reports")
TARGET_RTO = 300.0


def jsonl(p):
    p = pathlib.Path(p)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


@pytest.fixture(scope="module")
def m2():
    return measure(R / "drill-2-withdr.jsonl", "chaos/chaos-events.jsonl",
                   R / "health-events.jsonl", R / "failover-events.jsonl", TARGET_RTO)


def test_drill1_ton_tai_va_khong_phuc_hoi():
    """Baseline phải CHỨNG MINH được là nó không sống nổi, không phải chỉ nói."""
    m1 = measure(R / "drill-1-nodr.jsonl", "chaos/chaos-events.jsonl",
                 R / "health-events.jsonl", R / "failover-events.jsonl", TARGET_RTO)
    assert m1["requests_failed"] > 0, "drill 1 khong co request nao fail -> chaos khong an"
    assert m1["rto_verdict"] == "NO_RECOVERY", "drill 1 khong duoc tu phuc hoi"


def test_drill2_hop_le(m2):
    assert m2["valid"], m2["invalid_reasons"]
    assert not m2["warnings"], m2["warnings"]


def test_rto_do_duoc_bang_timestamp(m2):
    """ĐIỀU KIỆN TRƯỢT CỨNG: không có RTO đo được từ log = trượt."""
    assert m2["rto_measured_s"] is not None, "khong tinh duoc RTO tu log"
    assert m2["recovered_by_region"] != m2["killed_region"]


def test_health_check_interval_duoc_ghi_lai(m2):
    """RTO mà không biết interval/threshold thì không giải thích được nó đến từ đâu."""
    c = m2["health_check_config"]
    assert c["interval_s"] and c["threshold"], "health log thieu interval_s/threshold"
    assert m2["breakdown_seconds_from_t0"]["health_check_phat_hien"] >= c["detect_floor_s"] - 1, \
        "detect nhanh hon interval*threshold la bat kha thi -> log bi sua tay"


def test_rpo_duoc_do_chu_khong_uoc_luong(m2):
    assert m2["rpo_at_restore_s"] is not None, "thieu rpo_seconds trong 2_restore_snapshot"
    assert m2["docs_lost"] is not None, "thieu docs_lost — RPO phai co ca con so document"


def test_evidence_table_tro_vao_file_that():
    """Mọi Evidence cell phải là path[:line] tồn tại thật, không phải mô tả chung."""
    f = R / "rto-evidence.md"
    assert f.exists(), "thieu reports/rto-evidence.md"
    refs = re.findall(r"`([\w./-]+\.(?:jsonl|json|md|log|py))(?::(\d+))?`", f.read_text())
    assert len(refs) >= 5, f"chi tim thay {len(refs)} evidence path, can >= 5"
    for path, line in refs:
        p = pathlib.Path(path)
        assert p.exists(), f"evidence tro vao file khong ton tai: {path}"
        if line:
            n = len(p.read_text().splitlines())
            assert int(line) <= n, f"{path}:{line} vuot qua so dong that ({n})"


def test_chaos_khong_giet_ca_hai_region():
    kills = [e for e in jsonl("chaos/chaos-events.jsonl") if e.get("action") == "kill"]
    assert kills, "khong co su kien kill"
    for k in kills:
        assert not k.get("forced_both"), "da dung --i-really-want-both -> drill INVALID"
        assert k.get("other_alive"), "kill khi region con lai da chet -> double outage"


def test_evidence_table_da_dien_that(m2):
    """Template chua dien KHONG duoc tinh la evidence."""
    t = (R / "rto-evidence.md").read_text()
    assert "TEMPLATE" not in t, "van con la template chua dien"
    assert "`__" not in t and "| `__s`" not in t and "+__s" not in t, \
        "con placeholder __ trong bang evidence"


def test_so_trong_bang_khop_voi_so_trong_log(m2):
    """ĐIỀU KIỆN TRƯỢT CỨNG: con số bạn viết phải bằng con số log tính ra.

    Đây là chỗ chặn 'đoán RTO rồi viết vào bảng'. Sai lệch > 1s = số tự nghĩ ra.
    """
    assert m2["rto_measured_s"] is not None, \
        "chua do duoc RTO (drill 2 chua chay hoac chua hop le) -- chua the doi chieu so"
    t = (R / "rto-evidence.md").read_text()
    nums = [float(x) for x in re.findall(r"(\d+\.?\d*)\s*s", t)]
    rto = m2["rto_measured_s"]
    assert any(abs(n - rto) <= 1.0 for n in nums), \
        f"khong tim thay RTO {rto}s trong rto-evidence.md — so trong bang khong khop log"
    rpo = m2["rpo_at_restore_s"]
    assert any(abs(n - rpo) <= 1.0 for n in nums), \
        f"khong tim thay RPO {rpo}s trong rto-evidence.md"


def test_postmortem_co_gap_analysis():
    f = R / "postmortem.md"
    assert f.exists(), "thieu reports/postmortem.md"
    t = f.read_text().lower()
    for k in ["rto", "rpo", "gap", "action item"]:
        assert k in t, f"postmortem thieu phan '{k}'"
