"""Chaos script: giết 1 region. [CÓ SẴN — đọc kỹ phần AN TOÀN trước khi chạy]

Hai chế độ hỏng, cố ý khác nhau vì chúng cho RTO khác nhau (§6 Chaos Engineering):
  --mode stop      : process/container chết -> ConnectError ngay (fail nhanh, dễ phát hiện)
  --mode netblock  : cổng bị DROP -> request TREO tới timeout (fail chậm, health check
                     interval + timeout cộng thẳng vào RTO)

Hai backend:
  --backend bare   : uvicorn chạy trực tiếp, PID trong run/region-<r>.pid  (mặc định khi --mock)
  --backend docker : docker compose stop / iptables DROP trong container

--mock: pin mọi tham số thời gian (không phụ thuộc máy nhanh/chậm) -> chấm điểm reproducible.

AN TOÀN (đọc §6 "Nguyên tắc an toàn"):
  * Script TỪ CHỐI giết region nếu region còn lại không healthy -> không bao giờ tự
    tay tạo double-region outage rồi ngồi đo một con số vô nghĩa.
  * `--i-really-want-both` bỏ chặn đó, nhưng ghi cờ vào chaos-events.jsonl và
    tools/measure_rto.py sẽ đánh dấu drill là INVALID.
  * `restore` là kill switch: luôn chạy được, không cần điều kiện gì.

    python chaos/kill_region.py --region a --mode netblock --mock
    python chaos/kill_region.py restore --region a --backend bare

LƯU Ý: `restore` không có cờ `--mock` để tự suy ra backend như `kill` -- ở bare mode
PHẢI truyền `--backend bare` tường minh, nếu không nó mặc định `docker` và sẽ báo lỗi
trên máy không có Docker daemon (`docker compose ... start` thất bại).
"""
import argparse
import json
import os
import pathlib
import signal
import subprocess
import time

import httpx

EVENTS = pathlib.Path("chaos/chaos-events.jsonl")
PID_DIR = pathlib.Path("run")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def event(**kw):
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with EVENTS.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("CHAOS", json.dumps(rec))
    return rec


def is_ready(region: str, timeout=1.5) -> bool:
    try:
        return httpx.get(f"{URL[region]}/readyz", timeout=timeout).status_code == 200
    except Exception:
        return False


def is_alive(region: str, timeout=1.5) -> bool:
    try:
        return httpx.get(f"{URL[region]}/healthz", timeout=timeout).status_code == 200
    except Exception:
        return False


def pid_of(region: str) -> int | None:
    f = PID_DIR / f"region-{region}.pid"
    if not f.exists():
        return None
    pid = int(f.read_text().strip())
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def kill(region: str, mode: str, backend: str, force_both: bool, mock: bool):
    other = "b" if region == "a" else "a"
    other_alive = is_alive(other)
    if not other_alive and not force_both:
        event(action="refused", region=region, mode=mode,
              reason=f"region-{other} khong phan hoi /healthz -> giet region-{region} nua "
                     f"la double outage, RTO do duoc se vo nghia")
        raise SystemExit(
            f"CHAN LAI: region-{other} dang khong sống. Chạy `restore --region {other}` trước.\n"
            f"(Muốn ép: --i-really-want-both, nhưng drill sẽ bị đánh dấu INVALID.)")

    ev = event(action="kill", region=region, mode=mode, backend=backend, mock=mock,
               other_region=other, other_alive=other_alive, forced_both=force_both,
               note="t_outage_start — moc 0 cua RTO clock")
    if backend == "bare":
        pid = pid_of(region)
        if pid is None:
            raise SystemExit(f"khong tim thay PID cua region-{region} trong {PID_DIR}")
        # netblock: SIGSTOP -> TCP handshake vẫn xong nhưng không ai trả lời => request TREO
        #           (đúng hành vi của iptables DROP ở tầng app)
        # stop    : SIGKILL -> cổng đóng => ConnectError ngay
        os.kill(pid, signal.SIGSTOP if mode == "netblock" else signal.SIGKILL)
    else:
        svc = f"serving-{region}"
        if mode == "stop":
            subprocess.run(["docker", "compose", "stop", svc], check=True)
        else:
            subprocess.run(["docker", "exec", "--privileged", svc, "iptables", "-A", "INPUT",
                            "-p", "tcp", "--dport", "8000", "-j", "DROP"], check=True)
    return ev


def restore(region: str, backend: str):
    if backend == "bare":
        pid = pid_of(region)
        if pid:
            os.kill(pid, signal.SIGCONT)
            return event(action="restore", region=region, method="SIGCONT", pid=pid)
        return event(action="restore", region=region, method="need_manual_start",
                     note="process da bi SIGKILL, chay `make up-bare` lai")
    subprocess.run(["docker", "compose", "start", f"serving-{region}"], check=False)
    subprocess.run(["docker", "exec", "--privileged", f"serving-{region}", "iptables", "-F",
                    "INPUT"], check=False)
    return event(action="restore", region=region, method="docker_start+iptables_flush")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("cmd", nargs="?", default="kill", choices=["kill", "restore", "status"])
    p.add_argument("--region", default="a", choices=["a", "b"])
    p.add_argument("--mode", default="netblock", choices=["stop", "netblock"])
    p.add_argument("--backend", default=None, choices=["bare", "docker"])
    p.add_argument("--mock", action="store_true",
                   help="pin tham so thoi gian -> cham diem reproducible; ham y --backend bare")
    p.add_argument("--i-really-want-both", action="store_true")
    a = p.parse_args()
    backend = a.backend or ("bare" if a.mock else "docker")
    if a.cmd == "status":
        print(json.dumps({r: {"alive": is_alive(r), "ready": is_ready(r)} for r in "ab"}, indent=2))
    elif a.cmd == "restore":
        restore(a.region, backend)
    else:
        kill(a.region, a.mode, backend, a.i_really_want_both, a.mock)
