# RTO/RPO Evidence — Lab 23

Mọi số liệu dưới đây được lấy từ lần drill ngày 2026-08-25. RTO được đo theo trải
nghiệm người dùng qua load generator, không lấy từ thời điểm script runbook kết thúc.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| `t_outage` | `2026-08-25T04:57:16Z` | Event `action:kill`, Region A, `netblock` | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.3s` | Dòng `ok:false` đầu tiên sau `t_outage` | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | Không có | `request_thanh_cong_dau_tien:null` | `reports/measure-drill-1.json:16` |
| RTO | `NO_RECOVERY` | Không có request thành công nào sau lỗi | `reports/measure-drill-1.json:25` |
| Tổng request lỗi | `16` | Kết quả đo từ loadgen | `reports/measure-drill-1.json:28` |

## 2. Drill 2 — có DR

| Mốc | +giây từ `t_outage` | Cách đo | Evidence |
|---|---:|---|---|
| `t_outage` | `0.0s` | Event `action:kill` lúc `2026-08-25T05:17:54Z` | `chaos/chaos-events.jsonl:4` |
| User thấy lỗi đầu tiên | `0.0s` | Dòng `ok:false` đầu tiên sau outage | `reports/drill-2-withdr.jsonl:25` |
| Snapshot restore xong | `12.4s` | Timestamp của `2_restore_snapshot` trừ `t_outage` | `reports/failover-events.jsonl:14` |
| Health check phát hiện | `14.1s` | Region A chuyển sang `UNHEALTHY` sau 3 lỗi liên tiếp | `reports/health-events.jsonl:2` |
| Region phụ ready | `19.0s` | `4_wait_ready` thành công, Region B có 215 vectors và weights | `reports/failover-events.jsonl:16` |
| DNS cutover | `19.0s` | `5_dns_cutover`, `active_region:b` | `reports/failover-events.jsonl:17` |
| **RTO đo được** | **`22.2s`** | Request `ok:true` đầu tiên sau lỗi, được Region B phục vụ | `reports/drill-2-withdr.jsonl:36` |

| Chỉ số | Đo được | Mục tiêu | Verdict | Evidence |
|---|---:|---:|---|---|
| RTO — Inference API | `22.2s` | `300s` | **PASS**, thấp hơn mục tiêu `277.8s` | `reports/measure-drill-2.json:20` |
| RPO — Vector DB | `2.02s / 1 doc` | `300s` | **PASS**, thấp hơn mục tiêu `297.98s` | `reports/failover-events.jsonl:14` |
| Drill hợp lệ | `valid:true`, `warnings:[]` | Bắt buộc | **PASS** | `reports/measure-drill-2.json:2` |
| Region phục hồi | `b` | Khác Region A đã bị kill | **PASS** | `reports/measure-drill-2.json:6` |

## 3. Phân rã RTO theo critical path

Health checker và runbook xác nhận outage chạy song song trong drill này. Snapshot đã
restore xong ở `+12.4s`, trước khi health checker phát event ở `+14.1s`. Vì vậy cộng
thẳng mọi wall-clock duration sẽ đếm trùng phần chạy song song. Cột “đóng góp” dưới
đây chỉ tính thời gian nằm trên critical path của request người dùng.

| Thành phần | Thời gian thô | Đóng góp vào RTO | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---:|---:|---|---|
| Health-check detection | `14.08s`; cấu hình floor `5s × 3 = 15s` | `14.08s` | `interval_s:5`, `threshold:3`; phase của poll làm số đo thực tế lệch dưới 1 giây so với floor | Giảm interval có kiểm soát, giữ threshold để chống flapping. Evidence: `reports/health-events.jsonl:2` |
| Snapshot restore | `0.09s` | `0.00s` | Từ `1_verify_target` tới `2_restore_snapshot`; hoàn thành trong lúc detection còn chạy | Replicate thường xuyên hơn để giảm RPO; pre-stage snapshot ở target. Evidence: `reports/failover-events.jsonl:13`, `reports/failover-events.jsonl:14` |
| GPU pool warm-up | `6.40s` | `4.89s` | `waited_s:6.401`; khoảng `1.51s` đầu overlap với detection | Giữ một pool warm/full tối thiểu hoặc pre-warm khi alert bắt đầu. Evidence: `reports/failover-events.jsonl:16` |
| DNS/LB TTL cache | `3.20s` | `3.20s` | Request phục hồi lúc `+22.2s` trừ cutover lúc `+19.0s` | Giảm TTL hoặc dùng LB health routing trực tiếp. Evidence: `reports/failover-events.jsonl:17`, `reports/drill-2-withdr.jsonl:36` |
| **Tổng critical path** |  | **`22.17s ≈ 22.2s`** | `14.08 + 0.00 + 4.89 + 3.20` | Khớp kết quả đo ở `reports/measure-drill-2.json:20` |

## 4. Kết luận

Drill tạo ra 11 request lỗi trước khi phục hồi. Region B phục vụ request thành công
đầu tiên sau `22.2s`; snapshot làm mất 1 document tương ứng `2.02s`. Cả RTO và RPO
đều đạt mục tiêu 300 giây, và công cụ đo không phát hiện warning hay điều kiện invalid.
