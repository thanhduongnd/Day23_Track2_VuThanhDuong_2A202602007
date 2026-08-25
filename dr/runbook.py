"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi một bước runbook làm timeline cho postmortem."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("RUNBOOK", json.dumps(rec, ensure_ascii=False), flush=True)
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """CI được tự xác nhận; operator thật phải chủ động trả lời y/yes."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _latest_outage(primary: str) -> dict | None:
    events = pathlib.Path("chaos/chaos-events.jsonl")
    if not events.exists():
        return None
    latest = None
    for line in events.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("action") == "kill" and event.get("region") == primary:
            latest = event
    return latest


def _confirm_outage(primary: str, target: str) -> tuple[bool, list[dict]]:
    """Ba lần fail liên tiếp, theo cadence 5s giống health checker mặc định."""
    observations = []
    consecutive = 0
    next_probe = time.monotonic()
    for attempt in range(1, 4):
        primary_ready, primary_reason = hc.probe(primary, timeout=2.0)
        target_ready, target_reason = hc.probe(target, timeout=2.0)
        consecutive = consecutive + 1 if not primary_ready else 0
        observations.append({
            "attempt": attempt,
            "primary_ready": primary_ready,
            "primary_reason": primary_reason,
            "target_ready": target_ready,
            "target_reason": target_reason,
            "consecutive_primary_fails": consecutive,
        })
        if attempt < 3:
            next_probe += 5.0
            sleep_for = max(0.0, next_probe - time.monotonic())
            if sleep_for:
                time.sleep(sleep_for)
    return consecutive >= 3, observations


def _golden_signals(target: str, count: int = 10) -> dict:
    latencies = []
    failures = 0
    errors = []
    for _ in range(count):
        started = time.monotonic()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=3.0)
            latency_ms = (time.monotonic() - started) * 1000
            latencies.append(latency_ms)
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError):
                body = {}
            if (response.status_code != 200 or body.get("region") != target
                    or body.get("error")):
                failures += 1
                errors.append(body.get("error") or f"http_status={response.status_code}")
        except Exception as exc:
            latencies.append((time.monotonic() - started) * 1000)
            failures += 1
            errors.append(f"{type(exc).__name__}: {exc}")

    ordered = sorted(latencies)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    p95_ms = round(ordered[p95_index], 2) if ordered else None
    error_rate = round(failures / count, 4) if count else 0.0
    return {
        "requests": count,
        "successes": count - failures,
        "failures": failures,
        "error_rate": error_rate,
        "p95_ms": p95_ms,
        "errors": errors,
    }


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Thực thi runbook bán tự động, gọi failover đúng một lần."""
    if primary not in URL or target not in URL:
        return {"ok": False, "error": "unknown_region"}
    if primary == target:
        return {"ok": False, "error": "primary_and_target_must_differ"}

    run_started = time.time()

    # 1. Không tin một probe đơn lẻ; Region B có thể chưa ready vì chưa restore,
    # nhưng process của nó vẫn được failover kiểm tra ở bước con 1_verify_target.
    outage_confirmed, observations = _confirm_outage(primary, target)
    step(1, "xac_nhan_outage", ok=outage_confirmed, primary=primary,
         target=target, consecutive_failures=observations[-1][
             "consecutive_primary_fails"], probes=observations)
    if not outage_confirmed:
        return {"ok": False, "failed_step": 1, "error": "outage_not_confirmed",
                "probes": observations}

    # 2. Ghi cả mốc outage và mốc operator nhận biết để đo notification delay.
    outage = _latest_outage(primary)
    operator_notified_ts = time.time()
    outage_ts = outage.get("ts") if outage else None
    incident = step(
        2,
        "thong_bao_incident",
        ok=True,
        primary=primary,
        target=target,
        outage_ts=outage_ts,
        outage_iso=outage.get("iso") if outage else None,
        operator_notified_ts=operator_notified_ts,
        notification_delay_s=(None if outage_ts is None
                              else round(operator_notified_ts - outage_ts, 3)),
    )

    if not confirm(auto, f"Xác nhận failover region-{primary} sang region-{target}?"):
        return {"ok": False, "failed_step": 2, "error": "operator_declined",
                "incident": incident}

    # 3. Điểm duy nhất gọi fo.failover(); các bước 4-5 chỉ đọc dict trả về.
    failover_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", ok=bool(failover_result.get("ok")),
         target=target, backend=backend,
         failover_failed_step=failover_result.get("failed_step"),
         waited_s=failover_result.get("waited_s"))
    if not failover_result.get("ok"):
        return {"ok": False, "failed_step": 3, "incident": incident,
                "failover": failover_result}

    # 4. Không probe/call failover lần nữa: chỉ xác nhận state mà failover trả về.
    target_state = failover_result.get("target_state") or {}
    vector_count = target_state.get("count")
    weights = target_state.get("weights")
    replica_ok = bool(weights and isinstance(vector_count, int) and vector_count > 0)
    step(4, "verify_state_replica", ok=replica_ok, target=target,
         vector_count=vector_count, weights=weights,
         pool_state=target_state.get("pool_state"),
         embed_model_version=(failover_result.get("restore") or {}).get(
             "embed_model_version"))

    # 5. DNS result cũng lấy từ cùng failover_result.
    cutover = failover_result.get("cutover") or {}
    cutover_ok = bool(cutover.get("ok") and cutover.get("active_region") == target)
    step(5, "dns_cutover", ok=cutover_ok, target=target,
         active_region=cutover.get("active_region"), path=cutover.get("path"))

    # 6. Golden signals phải là request thật tới chính region phụ.
    signals = _golden_signals(target, count=10)
    signals_ok = signals["failures"] == 0
    step(6, "verify_golden_signals", ok=signals_ok, target=target, **signals)

    # 7. Tóm tắt và để lại lệnh đo RTO có thể copy-paste.
    now = time.time()
    summary_ok = replica_ok and cutover_ok and signals_ok
    measure_command = (
        "python3 tools/measure_rto.py --loadgen "
        "reports/drill-2-withdr.jsonl --target-rto 300"
    )
    step(7, "post_incident", ok=summary_ok,
         elapsed_s=round(now - run_started, 3),
         since_outage_s=(None if outage_ts is None else round(now - outage_ts, 3)),
         measure_rto_command=measure_command)

    return {
        "ok": summary_ok,
        "primary": primary,
        "target": target,
        "incident": incident,
        "failover": failover_result,
        "golden_signals": signals,
        "elapsed_s": round(now - run_started, 3),
        "measure_rto_command": measure_command,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
