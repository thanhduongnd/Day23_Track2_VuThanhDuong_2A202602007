# Runbook một trang — Region chính down

**Phạm vi:** bare mode, primary `a`, DR target `b`, snapshot backend `fs`.
**Nguyên tắc:** không cutover nếu Region B chưa trả `200` ở `/readyz`; không chạy
failover lần thứ hai khi lần đầu đang thực thi. Incident Commander (IC) là người duy
nhất có quyền phê duyệt cutover hoặc rollback.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Ai làm |
|---:|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | Region A có `ready:false` qua 3 probe liên tiếp; Region B vẫn có `alive:true`. Không tiếp tục nếu cả hai region đều down. | On-call SRE |
| 2 | Mở incident, bấm giờ RTO và yêu cầu xác nhận | `python3 dr/runbook.py --primary a --target b --backend fs` | Có event `name:"thong_bao_incident"` trong `reports/runbook-run.jsonl`; operator kiểm tra target rồi nhập `y`. Không dùng `--auto` khi vận hành thủ công. | Incident Commander |
| 3 | Restore state ở Region B | `tail -n 5 reports/failover-events.jsonl` | Event `step:"2_restore_snapshot"` có `ok:true`, `rpo_seconds`, `docs_lost` và `embed_model_version`. Nếu `ok:false`, dừng; không tự chạy lại failover. | DR/Storage operator |
| 4 | Scale pool và chờ Region B ready | `curl -sf http://127.0.0.1:8002/readyz` | HTTP 200, `ready:true`, `pool_state:"full"`, vector count lớn hơn 0 và không còn lý do warm-up/thiếu weights. | ML Platform on-call |
| 5 | Xác nhận DNS/LB cutover | `curl -sf http://127.0.0.1:8080/edge/state` | `active_region:"b"`; đồng thời log có `step:"5_dns_cutover", "ok":true`. Nếu bước 4 chưa đạt thì event này không được phép tồn tại. | Network/SRE on-call |
| 6 | Verify golden signals | `for i in $(seq 1 10); do curl -sf http://127.0.0.1:8002/v1/infer || echo REQUEST_FAILED; done; tail -n 2 reports/runbook-run.jsonl` | Cả 10 request thành công từ Region B; event `verify_golden_signals` có `failures:0`, `error_rate:0.0` và `p95_ms` được ghi lại. | Observability on-call |
| 7 | Đo RTO/RPO và mở postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300 \| tee reports/measure-drill-2.json` | Kết quả có `valid:true`, `warnings:[]`, `recovered_by_region:"b"`, `rto_verdict:"PASS"`, cùng `rpo_at_restore_s` và `docs_lost` khác `null`. | IC + SRE owner |

## Điều kiện dừng và escalation

- Dừng ngay nếu Region B không alive, snapshot không tồn tại, restore lỗi, `/readyz`
  không đạt trước timeout, hoặc cả hai region cùng down.
- Không sửa tay `edge/active_region` trong graded drill. Chỉ
  `dr/failover.py` được thực hiện cutover sau readiness.
- Nếu failover dừng trước DNS, giữ traffic khỏi Region B và chuyển sự cố cho DR/Storage
  owner cùng ML Platform owner điều tra `reports/failover-events.jsonl` và `run/*.log`.

## Rollback / failback về Region A

Không rollback tự động. IC chỉ cho phép failback khi Region A đã được restore và trả
`/readyz` thành công 3 lần liên tiếp, dữ liệu từ Region B đã được snapshot/reconcile,
model version khớp, và 10 request golden-signal trực tiếp tới A đều thành công.

Chuẩn bị và thực hiện failback có kiểm soát:

```bash
python3 chaos/kill_region.py restore --region a --backend bare
python3 state/snapshot.py put --region b --backend fs
python3 dr/failover.py --target a --backend fs
curl -sf http://127.0.0.1:8080/edge/state
```

Hoàn thành khi `5_dns_cutover` có `target:"a", ok:true`, edge báo
`active_region:"a"`, và golden signals qua edge không có lỗi. Nếu bất kỳ điều kiện
nào thất bại, IC giữ Region B làm active region; chỉ IC được phê duyệt lần thử lại.
