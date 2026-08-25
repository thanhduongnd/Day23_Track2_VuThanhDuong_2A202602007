"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append một event có timestamp vào log failover và stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        **kw,
    }
    with LOG.open("a", encoding="utf-8") as log:
        log.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("FAILOVER", json.dumps(rec, ensure_ascii=False), flush=True)
    return rec


def state_of(region: str, timeout: float = 2.0) -> dict:
    """Đọc trạng thái compute/state của một region qua API công khai."""
    response = httpx.get(f"{URL[region]}/v1/state", timeout=timeout)
    response.raise_for_status()
    return response.json()


def _restore_pool_state(target: str, previous: str | None):
    """Rollback bước scale khi readiness timeout, không đụng tới DNS."""
    if previous:
        pathlib.Path(f"state/region-{target}/pool_state").write_text(
            previous + "\n", encoding="utf-8")


def failover(target: str, backend: str, wait: float) -> dict:
    """Restore, warm up và chỉ cut over sau khi target thực sự ready."""
    if target not in URL:
        return {"ok": False, "target": target, "error": "unknown_target"}
    if backend not in {"fs", "minio"}:
        return {"ok": False, "target": target, "error": "unknown_backend"}
    if wait < 0:
        return {"ok": False, "target": target, "error": "wait_must_be_non_negative"}

    # 1. Target process phải truy cập được, nhưng chưa cần ready ở thời điểm này.
    try:
        initial_state = state_of(target)
        emit(step="1_verify_target", ok=True, target=target, state=initial_state)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="1_verify_target", ok=False, target=target, reason=reason)
        return {"ok": False, "target": target, "failed_step": "1_verify_target",
                "error": reason}

    # 2. Restore cả vector DB, weights và version; sau đó đo data loss thật.
    try:
        restored = snapshot.get(target, backend)
        source_region = restored.get("source_region") or ("b" if target == "a" else "a")
        rpo = snapshot.rpo(
            pathlib.Path(f"state/region-{source_region}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        restore_result = {**restored, **rpo}
        emit(
            step="2_restore_snapshot",
            ok=True,
            target=target,
            backend=backend,
            rpo_seconds=rpo.get("rpo_seconds"),
            docs_lost=rpo.get("docs_lost"),
            embed_model_version=restored.get("embed_model_version"),
            snapshot_at=restored.get("snapshot_at"),
            restored_at=restored.get("restored_at"),
        )
    except (Exception, SystemExit) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="2_restore_snapshot", ok=False, target=target,
             backend=backend, reason=reason)
        return {"ok": False, "target": target, "failed_step": "2_restore_snapshot",
                "error": reason}

    # 3. Việc đọc /v1/state ở bước 1 đã giúp serving process ghi nhận trạng thái
    # warm hiện tại; đổi sang full lúc này mới kích hoạt bộ đếm warm-up.
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    previous_pool = str(initial_state.get("pool_state") or "warm")
    try:
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        pool_file.write_text("full\n", encoding="utf-8")
        emit(step="3_scale_pool", ok=True, target=target,
             **{"from": previous_pool, "to": "full"})
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="3_scale_pool", ok=False, target=target, reason=reason)
        return {"ok": False, "target": target, "failed_step": "3_scale_pool",
                "error": reason}

    # 4. Readiness bao gồm pool, weights và vector count. Timeout => rollback
    # pool state và tuyệt đối không ghi edge/active_region.
    wait_started = time.monotonic()
    deadline = wait_started + wait
    attempts = 0
    last_reason = "ready_timeout"
    ready_body = None
    while True:
        remaining = deadline - time.monotonic()
        if attempts > 0 and remaining <= 0:
            break
        attempts += 1
        try:
            response = httpx.get(
                f"{URL[target]}/readyz",
                timeout=max(0.05, min(2.0, max(remaining, 0.05))),
            )
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError):
                body = {}
            if response.status_code == 200 and body.get("ready", True) is True:
                ready_body = body
                break
            reasons = body.get("reasons")
            last_reason = (";".join(str(item) for item in reasons)
                           if isinstance(reasons, list) and reasons
                           else f"http_status={response.status_code}")
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: {exc}"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.5, remaining))

    waited_s = round(time.monotonic() - wait_started, 3)
    if ready_body is None:
        try:
            _restore_pool_state(target, previous_pool)
            rolled_back_to = previous_pool
        except Exception:
            rolled_back_to = None
        emit(step="4_wait_ready", ok=False, target=target, waited_s=waited_s,
             attempts=attempts, reason=last_reason, pool_rolled_back_to=rolled_back_to)
        return {
            "ok": False,
            "target": target,
            "failed_step": "4_wait_ready",
            "error": last_reason,
            "waited_s": waited_s,
            "restore": restore_result,
        }

    try:
        final_state = state_of(target)
    except Exception as exc:
        reason = f"post_ready_state_check_failed: {type(exc).__name__}: {exc}"
        _restore_pool_state(target, previous_pool)
        emit(step="4_wait_ready", ok=False, target=target, waited_s=waited_s,
             attempts=attempts, reason=reason, pool_rolled_back_to=previous_pool)
        return {"ok": False, "target": target, "failed_step": "4_wait_ready",
                "error": reason, "waited_s": waited_s, "restore": restore_result}

    emit(step="4_wait_ready", ok=True, target=target, waited_s=waited_s,
         attempts=attempts, readiness=ready_body, state=final_state)

    # 5. Đây là lần ghi DNS/LB duy nhất và chỉ xảy ra sau readiness thành công.
    try:
        active_file = pathlib.Path("edge/active_region")
        active_file.parent.mkdir(parents=True, exist_ok=True)
        active_file.write_text(target + "\n", encoding="utf-8")
        cutover = {"ok": True, "active_region": target, "path": str(active_file)}
        emit(step="5_dns_cutover", target=target, **cutover)
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        emit(step="5_dns_cutover", ok=False, target=target, reason=reason)
        return {"ok": False, "target": target, "failed_step": "5_dns_cutover",
                "error": reason, "waited_s": waited_s, "restore": restore_result,
                "target_state": final_state}

    return {
        "ok": True,
        "target": target,
        "backend": backend,
        "initial_state": initial_state,
        "restore": restore_result,
        "target_state": final_state,
        "waited_s": waited_s,
        "cutover": cutover,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
