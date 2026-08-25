"""Đo RTO/RPO từ log — KHÔNG từ cảm giác. [CÓ SẴN — dụng cụ chấm điểm]

Đọc 4 nguồn log, tất cả đều là timestamp thật:
  loadgen JSONL          -> t_first_fail, t_recovered  (trải nghiệm của USER)
  chaos/chaos-events     -> t_outage                   (mốc 0)
  reports/health-events  -> t_detect                   (health check bắt được lúc nào)
  reports/failover-events-> t_cutover                  (DNS đổi lúc nào)

    python tools/measure_rto.py --loadgen reports/drill-2.jsonl --target-rto 300
"""
import argparse
import json
import pathlib


def jsonl(p) -> list[dict]:
    p = pathlib.Path(p)
    if not p.exists():
        return []
    out = []
    for i, line in enumerate(p.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            raise SystemExit(f"{p}:{i} khong phai JSONL hop le. Ban dua file gi vao day? "
                             f"(--loadgen can file cua loadgen/traffic.py, moi request 1 dong)")
    return out


def _empty_result(why, target_rto):
    """Shape CHUẨN, dùng cho mọi đường early-return.

    Nếu return thiếu key thì test/report code phía sau ăn KeyError thay vì
    AssertionError có message rõ ràng — sinh viên đọc traceback không hiểu vì sao.
    Luôn trả đủ field, giá trị None/0 ở chỗ chưa đo được.
    """
    return {
        "valid": False, "invalid_reasons": [why], "warnings": [], "why": why,
        "killed_region": None, "recovered_by_region": None,
        "t_outage_iso": None, "chaos_mode": None,
        "breakdown_seconds_from_t0": {
            "user_thay_loi_dau_tien": None, "health_check_phat_hien": None,
            "dns_cutover": None, "request_thanh_cong_dau_tien": None,
        },
        "health_check_config": {"interval_s": None, "threshold": None, "detect_floor_s": None},
        "rto_measured_s": None, "rto_target_s": target_rto, "rto_verdict": None,
        "rpo_at_restore_s": None, "docs_lost": None, "requests_failed": 0,
        "evidence": {"loadgen": None, "chaos": None, "health": None, "failover": None},
    }


def measure(loadgen_path, chaos_path, health_path, failover_path, target_rto):
    reqs = jsonl(loadgen_path)
    kills = [e for e in jsonl(chaos_path) if e.get("action") == "kill"]
    if not reqs:
        return _empty_result(f"{loadgen_path} rong -> khong co dong ho -> khong co RTO "
                             f"(ban da chay `loadgen/traffic.py` chua?)", target_rto)
    if not kills:
        return _empty_result("chaos-events.jsonl khong co su kien kill (ban da chay "
                             "`chaos/kill_region.py` chua?)", target_rto)
    # Chọn đúng sự kiện kill THUỘC cửa sổ thời gian của file loadgen này.
    # (chaos-events.jsonl tích luỹ cả nhiều drill — lấy bừa kills[-1] là so drill 1
    #  với t_outage của drill 2 và ra kết quả vô nghĩa.)
    lo, hi = reqs[0]["ts"], reqs[-1]["ts"]
    in_window = [e for e in kills if lo <= e["ts"] <= hi]
    if not in_window:
        return _empty_result(f"khong co su kien kill nao trong cua so cua "
                f"{loadgen_path} ({lo:.0f}..{hi:.0f}) -> loadgen va chaos khong chay cung luc",
                target_rto)
    k = in_window[-1]
    t0, dead = k["ts"], k["region"]
    invalid = []
    if k.get("forced_both"):
        invalid.append("chaos ep giet ca 2 region (--i-really-want-both) -> drill INVALID")
    if not k.get("other_alive", True):
        invalid.append(f"luc kill, region con lai khong alive -> double outage")

    after = [r for r in reqs if r["ts"] >= t0]
    if not after:
        invalid.append("loadgen ket thuc TRUOC luc kill -> khong do duoc gi")
    fails = [r for r in after if not r.get("ok")]
    t_first_fail = fails[0]["ts"] if fails else None
    t_recovered = next((r["ts"] for r in after
                        if r.get("ok") and r["ts"] > (t_first_fail or t0)), None)
    survivor = next((r.get("served_by") for r in after
                     if r.get("ok") and r["ts"] > (t_first_fail or t0)), None)
    if t_first_fail is None:
        invalid.append(f"khong co request nao fail sau khi kill region-{dead} -> "
                       f"chaos khong tac dung, hoac loadgen khong di qua edge")
    if survivor == dead:
        invalid.append(f"request phuc hoi van duoc serve boi region-{dead} -> region chua chet that")

    # health-events.jsonl và failover-events.jsonl mở bằng mode "a" -> tích luỹ qua
    # nhiều lần chạy. Chặn CẢ HAI đầu bằng cửa sổ của loadgen (lo..hi), không chỉ chặn
    # dưới bằng t0 -- nếu không, đo lại drill 1 SAU KHI đã chạy drill 2 sẽ vô tình nhặt
    # nhầm health/failover event của drill 2 (t_detect/t_cutover của drill 2 đều > t0
    # của drill 1) và báo cáo sai hoàn toàn con số cho drill 1.
    hev = [e for e in jsonl(health_path)
           if e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
           and e.get("region") == dead and t0 <= e["ts"] <= hi]
    t_detect = hev[0]["ts"] if hev else None
    interval = hev[0].get("interval_s") if hev else None
    threshold = hev[0].get("threshold") if hev else None

    fev = [e for e in jsonl(failover_path)
           if e.get("step") == "5_dns_cutover" and t0 <= e["ts"] <= hi]
    t_cutover = fev[0]["ts"] if fev else None
    rest = [e for e in jsonl(failover_path)
            if e.get("step") == "2_restore_snapshot" and t0 <= e["ts"] <= hi]
    rpo = rest[0].get("rpo_seconds") if rest else None
    docs_lost = rest[0].get("docs_lost") if rest else None

    warn = []
    if t_detect and t_cutover and t_cutover < t_detect:
        warn.append("t_cutover < t_detect: ban cutover TRUOC khi health check phat hien -> "
                    "so nay do tay nguoi, khong do automation. Chay lai drill.")
    if t_detect is None:
        warn.append(f"khong co su kien UNHEALTHY cho region-{dead} trong health log -> "
                    f"health checker khong chay, hoac chi log region phu")
    if t_cutover is None:
        warn.append("khong co su kien 5_dns_cutover -> failover chay tay, khong tai lap duoc")

    def d(t):
        return None if t is None else round(t - t0, 1)

    rto = d(t_recovered)
    return {
        "valid": not invalid, "invalid_reasons": invalid, "warnings": warn,
        "killed_region": dead, "recovered_by_region": survivor,
        "t_outage_iso": k.get("iso"), "chaos_mode": k.get("mode"),
        "breakdown_seconds_from_t0": {
            "user_thay_loi_dau_tien": d(t_first_fail),
            "health_check_phat_hien": d(t_detect),
            "dns_cutover": d(t_cutover),
            "request_thanh_cong_dau_tien": rto,
        },
        "health_check_config": {"interval_s": interval, "threshold": threshold,
                                "detect_floor_s": None if not (interval and threshold)
                                else round(interval * threshold, 1)},
        "rto_measured_s": rto, "rto_target_s": target_rto,
        "rto_verdict": "NO_RECOVERY" if rto is None else ("PASS" if rto <= target_rto else "FAIL"),
        "rpo_at_restore_s": rpo, "docs_lost": docs_lost,
        "requests_failed": len(fails),
        "evidence": {"loadgen": str(loadgen_path), "chaos": str(chaos_path),
                     "health": str(health_path), "failover": str(failover_path)},
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--loadgen", required=True)
    p.add_argument("--chaos", default="chaos/chaos-events.jsonl")
    p.add_argument("--health", default="reports/health-events.jsonl")
    p.add_argument("--failover", default="reports/failover-events.jsonl")
    p.add_argument("--target-rto", type=float, default=300)
    a = p.parse_args()
    out = measure(a.loadgen, a.chaos, a.health, a.failover, a.target_rto)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    raise SystemExit(0 if out["valid"] else 2)
