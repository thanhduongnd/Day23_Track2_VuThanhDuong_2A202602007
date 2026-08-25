# Postmortem — DR Drill Lab 23

Postmortem này là blameless: sự cố được phân tích theo thiết kế hệ thống và quy trình,
không quy lỗi cho người chạy chaos drill.

## 1. Timeline

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| `2026-08-25T05:17:54Z` | Outage bắt đầu: Region A bị `netblock` | `chaos/chaos-events.jsonl:4` |
| `2026-08-25T05:17:54Z` | User đầu tiên nhận HTTP 503/`ReadTimeout` | `reports/drill-2-withdr.jsonl:25` |
| `2026-08-25T05:18:06Z` | Runbook xác nhận 3 lỗi liên tiếp và mở incident; notification delay `12.285s` | `reports/runbook-run.jsonl:2` |
| `2026-08-25T05:18:08Z` | Health checker chuyển Region A sang `UNHEALTHY` | `reports/health-events.jsonl:2` |
| `2026-08-25T05:18:13Z` | Region B ready và DNS/LB cutover sang B | `reports/failover-events.jsonl:16`, `reports/failover-events.jsonl:17` |
| `2026-08-25T05:18:14Z` | Golden signals: 10/10 request thành công, error rate `0.0`, p95 `112.05ms` | `reports/runbook-run.jsonl:6` |
| `2026-08-25T05:18:16Z` | Resolved: request đầu tiên qua edge thành công từ Region B | `reports/drill-2-withdr.jsonl:36` |

## 2. RTO/RPO đo được và gap

- RTO mục tiêu: `300s`; đo được: `22.2s`; gap: **thấp hơn mục tiêu `277.8s`**.
- RPO mục tiêu: `300s`; đo được: `2.02s` và `1` document bị mất; gap:
  **thấp hơn mục tiêu `297.98s`**.
- Bước tốn nhiều thời gian nhất là health-check detection: thực đo `14.1s`, còn
  detection floor theo cấu hình là `15s`. Đây là khoảng 63.5% RTO theo số đo thực tế.
- Snapshot restore và phần đầu của GPU warm-up chạy song song với detection. Vì vậy
  critical-path breakdown là `14.08 + 0.00 + 4.89 + 3.20 = 22.17s`, làm tròn thành
  RTO `22.2s`; chi tiết ở `reports/rto-evidence.md`.

Evidence kết quả tổng hợp: `reports/measure-drill-2.json:9` và
`reports/measure-drill-2.json:20`.

## 3. Root cause — 5 Whys

1. **Vì sao user nhận lỗi?** Edge vẫn định tuyến tới Region A trong khi process A bị
   treo, nên request chờ hết upstream timeout và trả 503.
2. **Vì sao edge chưa chuyển ngay sang B?** Hệ thống yêu cầu xác nhận nhiều probe để
   chống flapping và chỉ cho phép DNS cutover sau khi target thực sự ready.
3. **Vì sao B chưa ready khi outage bắt đầu?** B là passive region: pool ở trạng thái
   `warm`, không có model weights và vector DB ban đầu rỗng.
4. **Vì sao recovery cần restore và warm-up?** Thiết kế active-passive giảm chi phí
   compute nhưng đưa thời gian restore state và GPU warm-up vào RTO.
5. **Vì sao quy trình vẫn có rủi ro trong outage thật?** Bản lab dùng replica filesystem
   trên cùng máy. Nếu đây là mất region vật lý, bước `2_restore_snapshot` sẽ thất bại
   nếu object store/control plane không độc lập với primary. Hệ thống thật cần snapshot
   off-region, kiểm tra restore định kỳ và health checker chạy ngoài serving process.

Root cause hệ thống: Region B chưa được pre-stage để phục vụ ngay, còn detection và
recovery phụ thuộc vào cadence chống flapping cùng một bản snapshot phải restore khi
sự cố đã xảy ra. Chaos script chỉ kích hoạt và làm lộ đặc tính thiết kế này.

## 4. Action items

| # | Action item | Owner | Deadline | Tác động dự kiến |
|---:|---|---|---|---|
| 1 | Đổi health check từ `5s × 3` sang `3s × 3`, theo dõi false positive và thêm circuit breaker trước khi áp dụng production | SRE owner | 2026-09-01 | Giảm detection floor khoảng `6s` (`15s → 9s`) |
| 2 | Duy trì model weights và một pool tối thiểu đã pre-warm tại Region B; chạy restore/readiness canary hằng ngày | ML Platform owner | 2026-09-08 | Loại phần lớn `6.40s` GPU warm-up và phát hiện snapshot lỗi trước incident |
| 3 | Giảm chu kỳ replication từ `30s` xuống `10s` và lưu snapshot ở object store off-region | DR/Storage owner | 2026-09-08 | Giảm RPO ceiling tối đa khoảng `20s`; tránh mất replica cùng primary |

## 5. Ba câu hỏi bắt buộc

1. `interval × threshold = 5s × 3 = 15s`. Detection floor này bằng khoảng **67.6%**
   RTO `22.2s`; số detection thực đo là `14.1s` do thời điểm outage nằm giữa hai poll.
2. Nếu interval giảm xuống `1s` và vẫn giữ threshold 3, floor giảm từ `15s` xuống
   `3s`; RTO lý thuyết giảm khoảng `12s`, còn khoảng `10.2s` nếu các thành phần khác
   không đổi. Đổi lại là tần suất probe tăng 5 lần, nhiều alert do lỗi thoáng qua hơn,
   nguy cơ flapping/cutover sai và tải lớn hơn lên serving/control plane.
3. Với outage kéo dài 6 giờ và primary mất dữ liệu vĩnh viễn, `docs_lost:1` nghĩa là
   một document đã được hệ thống primary chấp nhận nhưng chưa vào snapshot, nên không
   tồn tại ở Region B sau recovery. Với khách hàng, đó có thể là một ticket, cập nhật
   knowledge-base hoặc dữ liệu inference bị mất vĩnh viễn; con số sẽ tăng nếu hệ thống
   tiếp tục nhận ghi mà replication không hoạt động.
