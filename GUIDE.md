# Lab Guide — Region Failover

## Table of Contents

1. [Learning Objectives](#learning-objectives)
2. [Timeline](#timeline)
3. [Step 0 — Setup](#step-0--setup-10-min)
4. [Step 1 — Baseline](#step-1--baseline-15-min)
5. [Step 2 — Red Team](#step-2--red-team-25-min)
6. [Step 3 — Containment](#step-3--containment-50-min)
7. [Step 4 — Prove &amp; Collect Evidence](#step-4--prove--collect-evidence-20-min)
8. [Common Pitfalls](#common-pitfalls)
9. [Troubleshooting](#troubleshooting)
10. [Stretch Goals](#stretch-goals)
11. [Reflection Questions](#reflection-questions)

## Learning Objectives

By the end of this lab, you will have:

- Watched a request pipeline fail with **no** disaster recovery in place, and measured exactly how long it stays down.
- Implemented a health checker, a failover procedure, and a runbook — the three pieces that turn "down forever" into "down for N seconds."
- Re-run the same attack and proven, from raw log timestamps, that RTO ≤ 300s and RPO is bounded and quantified.
- Produced evidence (`reports/rto-evidence.md`, `reports/postmortem.md`, `reports/runbook.md`) where every number traces back to a real file and line.

Everything runs locally: two FastAPI processes stand in for two regions, SQLite stands in for a vector DB, and the filesystem stands in for S3. The behaviors that matter — health-check polling, replication lag, GPU pool warm-up, DNS TTL caching — are all real; only the infrastructure underneath is simplified.

## Timeline

| Step           | Duration | Focus                                        | Slide reference                                                            |
| -------------- | -------: | -------------------------------------------- | -------------------------------------------------------------------------- |
| 0. Setup       |   10 min | Install deps, seed state, bring the stack up | —                                                                         |
| 1. Baseline    |   15 min | Trace the request path; inspect Region B     | §1 RTO/RPO Fundamentals                                                   |
| 2. Red Team    |   25 min | Kill Region A while serving live traffic     | §1 Case Study · §4 Anti-Patterns                                        |
| 3. Containment |   50 min | Implement health check, failover, runbook    | §2 Multi-Region Patterns · §3 State Recovery · §4 Failover Automation |
| 4. Prove       |   20 min | Re-attack, measure RTO/RPO, write evidence   | §4 Runbook & Postmortem · §6 Game Day                                   |

---

## Step 0 — Setup (10 min)

```bash
cd lab23-region-failover
pip install -r requirements.txt
make seed
bash scripts/up_bare.sh
curl localhost:8080/v1/infer
```

`make seed` creates Region A with 200 documents and model weights on disk; Region B starts **empty** — that gap is intentional and is what you'll fix in Step 3.

**Checkpoint** — all three services must respond:

```bash
curl localhost:8001/healthz   # Region A
curl localhost:8002/healthz   # Region B
curl localhost:8080/edge/state
```

> If a service doesn't come up, check `run/region-a.log`, `run/region-b.log`, or `run/edge.log` first. Don't reach for Docker to work around a bare-mode issue — 90% of the time it's a stale process still holding port 8001/8002/8080.

---

## Step 1 — Baseline (15 min)

Read `serving/app.py` and `edge/proxy.py`, then trace one request through three layers:

1. **`edge/proxy.py`** — the DNS/LB stand-in. Picks an upstream from `edge/active_region`, but caches it for `EDGE_TTL_SECONDS`.
2. **`serving/app.py`** — the inference API. `/readyz` checks pool state, model weights, *and* vector count — not just "is the process alive."
3. **`state/`** — the vector DB (SQLite) and model weights on disk.

Inspect Region B directly:

```bash
curl localhost:8002/v1/state
```

**Before writing any code, answer these three questions:**

1. If Region A died right now, which component would notice — and how?
2. Does Region B currently hold any data or model weights?
3. If you flipped `edge/active_region` to `b` this instant, what would users get?

**Expected answers:** nothing detects it yet · Region B has `count:0`, `weights:false` · users would get `region_not_ready`. This is the core lesson of AI infrastructure DR: a process being alive is not the same as a region being able to serve inference.

---

## Step 2 — Red Team (25 min)

Generate traffic, wait 8 seconds, then take Region A down mid-flight:

```bash
python3 loadgen/traffic.py --duration 40 --rps 2 --out reports/drill-1-nodr.jsonl &
sleep 8
python3 chaos/kill_region.py --region a --mode netblock --mock
```

`netblock --mock` sends `SIGSTOP` in bare mode: the TCP connection still opens, but nothing answers — the same symptom as a dropped packet, so the client hangs until timeout. `--mode stop` sends `SIGKILL` instead, which fails fast with a connection error.

Once the load generator finishes, measure the (non-existent) recovery:

```bash
python3 tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300
```

**Reference run** (yours will differ — that's expected): 32 requests total, first failure at `+0.2s` with a `2017.7ms` timeout (`reports/drill-1-nodr.jsonl:17`), 16/32 requests failed, verdict `"rto_verdict":"NO_RECOVERY"`.

Restore Region A before moving on — **`--backend bare` is required** in bare mode:

```bash
python3 chaos/kill_region.py restore --region a --backend bare
```

> `restore` has no `--mock` shortcut. Omit `--backend bare` and it defaults to Docker, which will fail if you don't have a daemon running. If you used `--mode stop` earlier, the process was `SIGKILL`'d — restore will report `need_manual_start`; just re-run `bash scripts/up_bare.sh`.

---

## Step 3 — Containment (50 min)

Three files under `dr/` are skeletons with `NotImplementedError`. Each one has a detailed docstring — read it in full before writing code. **Do not modify any file outside `dr/`** to make a test pass.

### 3a. `dr/health_checker.py` — 12 min

Poll `/readyz` on both regions every `interval` seconds. Only flip to `UNHEALTHY` after `threshold` **consecutive** failures, and only emit a JSONL line when the state actually changes. A `state_change` line must include at least: `ts`, `region`, `to`, `reason`, `interval_s`, `threshold`.

> `interval × threshold` is your **detection floor** — it's part of your RTO whether you like it or not. With `interval=5s, threshold=3`, that floor is 15 seconds. In one reference run, that alone was ~53% of the total 28.5s RTO.

```bash
pytest tests/test_failover.py::test_health_checker_can_threshold_lien_tiep
```

### 3b. `dr/failover.py` — 15 min

Implement exactly these five steps, in this order:

| # | Step                   | What it does                                                                                  |
| - | ---------------------- | --------------------------------------------------------------------------------------------- |
| 1 | `1_verify_target`    | Check current state of the target region                                                      |
| 2 | `2_restore_snapshot` | Restore via`state/snapshot.py`; log `rpo_seconds`, `docs_lost`, `embed_model_version` |
| 3 | `3_scale_pool`       | Flip target's pool state to`full`                                                           |
| 4 | `4_wait_ready`       | Poll`/readyz` until it returns 200                                                          |
| 5 | `5_dns_cutover`      | Only now, write the target region into`edge/active_region`                                  |

> If step 4 times out, **abort** — do not cut over. Flipping DNS before the target is actually ready means users get 503s from *both* regions, which lengthens RTO instead of shortening it.

```bash
pytest tests/test_failover.py::test_failover_khong_cutover_khi_target_chua_ready
```

### 3c. `dr/runbook.py` — 13 min

Automate the seven checklist steps from §4 "Runbook: Region Chính Down": confirm the outage, announce the incident and start the clock, call `failover.failover(...)` **exactly once**, verify the state replica, record the cutover result, check golden signals with 10 real requests, then log the post-incident summary.

Default behavior must ask for a `y/N` confirmation — this is intentionally semi-automated, not full-auto (§4 warns that full-auto failover without a circuit breaker causes flapping between regions). `--auto` is for the graded drill / CI only.

- Runbook's own log: `reports/runbook-run.jsonl`
- The five failover sub-steps: `reports/failover-events.jsonl`

### 3d. `reports/runbook.md` — 10 min

Fill in the one-page template so someone who did **not** write this code could execute it at 3 AM. Each step needs: a copy-pasteable command, a clear "how do I know this worked" signal, an owner role, and — critically — the rollback condition and who has authority to trigger it back to Region A.

---

## Step 4 — Prove & Collect Evidence (20 min)

`dr/failover.py` restores from a snapshot — but a fresh checkout has never taken one. You must run ingest + replication **before** the attack, or `failover.py` will die at `2_restore_snapshot`.

```bash
python3 state/ingest.py --region a --rate 0.5 --duration 150 &
python3 state/replicate.py --every 30 --duration 150 --backend fs &
sleep 5   # let the first replication cycle complete before anything else starts

python3 loadgen/traffic.py --duration 100 --rps 2 --out reports/drill-2-withdr.jsonl &
python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 \
    --out reports/health-events.jsonl &
sleep 12
python3 chaos/kill_region.py --region a --mode netblock --mock
python3 dr/runbook.py --primary a --target b --backend fs --auto
python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
```

### Reading the Result

The table below is from **one** reference run — an illustration of what the output looks like, not a target to copy:

| Milestone (from`t_outage`)             |       +seconds | Evidence                            |
| ---------------------------------------- | -------------: | ----------------------------------- |
| User sees first error                    |            0.2 | `reports/drill-2-withdr.jsonl:25` |
| Health check flags Region A`UNHEALTHY` |           14.9 | `reports/health-events.jsonl:3`   |
| Snapshot restore complete                |           17.2 | `reports/failover-events.jsonl:2` |
| Region B ready                           |           23.3 | `reports/failover-events.jsonl:4` |
| DNS cutover                              |           23.4 | `reports/failover-events.jsonl:5` |
| First successful request from B          | **28.5** | `reports/drill-2-withdr.jsonl:39` |

RTO = 28.5s vs. a 300s target → **PASS**. RPO in that run was 14.04s / 7 documents lost; a separate run measured 4.01s / 2 documents. **RPO is expected to vary** — it depends on exactly where the last `state/replicate.py` cycle landed relative to the restore. RTO is much more stable across runs (typically 28.3–28.6s). Do not copy either sample number into your own report.

### Finishing the Reports

Fill in `reports/rto-evidence.md` and `reports/postmortem.md` with numbers from **your own** run. Every Evidence cell must be a real `path:line`. Your RTO breakdown table must cover all four components and sum to your measured RTO:

1. Health-check detection floor
2. Snapshot restore
3. GPU pool warm-up
4. DNS/LB TTL cache

Run the evidence gates individually while you work:

```bash
pytest tests/test_rto_evidence.py::test_evidence_table_tro_vao_file_that
pytest tests/test_rto_evidence.py::test_evidence_table_da_dien_that
pytest tests/test_rto_evidence.py::test_so_trong_bang_khop_voi_so_trong_log
pytest tests/test_rto_evidence.py::test_postmortem_co_gap_analysis
```

Then run everything:

```bash
python3 -m pytest tests/ -v
```

---

## Common Pitfalls

| Pitfall                                           | Fix                                                                                                                                                                                  |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| No Docker available                               | Use`bash scripts/up_bare.sh` — this is the primary path, not a fallback.                                                                                                          |
| MinIO setup eats your time budget                 | Use`--backend fs` (snapshots land under `state/_replica/`). MinIO is a stretch goal only.                                                                                        |
| Killing both regions at once                      | The chaos script refuses this by default.`--i-really-want-both` forces it but marks the drill `INVALID`.                                                                         |
| Measuring RTO "by feel"                           | Only timestamps from`loadgen/traffic.py` count. The kill event must fall inside the load generator's own time window.                                                              |
| Forgetting the detection floor                    | `interval_s × threshold` must appear as its own line in your RTO breakdown — it's not optional.                                                                                  |
| Manually editing`edge/active_region`            | `measure_rto.py` flags it if `t_cutover < t_detect`, and `test_drill2_hop_le` rejects any warnings. Re-run the automated drill instead.                                        |
| Assuming warm-up starts at process boot           | `serving/app.py` only starts the warm-up timer when `pool_state` changes *while running*. Don't patch `serving/` to dodge this.                                              |
| Confusing`netblock` with a dead process         | In bare mode,`netblock` is `SIGSTOP` (process paused, not killed). Restore with `restore --region a --backend bare`. `stop` is the one that actually kills it (`SIGKILL`). |
| Calling`failover.py` before any snapshot exists | Start`state/replicate.py` and wait for its first `put` — otherwise `state/snapshot.py get` fails at `2_restore_snapshot`.                                                   |
| Using Docker mode for the graded drill            | `--backend docker` works, but timing depends on your machine and Docker daemon — not reproducible. Grading always uses bare `--mock`.                                           |

## Troubleshooting

- **Port 8001/8002/8080 already in use** — run `bash scripts/down_bare.sh`, find whatever's still holding the port, then restart. Check `run/*.log` for details.
- **Region A isn't ready right after setup** — re-run `make seed`; confirm `state/region-a/pool_state`, the weights file, and the SQLite DB were actually created.
- **`restore` says no snapshot was ever `put`** — check `reports/replication.jsonl` and `state/_replica/dr-artifacts/MANIFEST.json`.
- **Baseline drill has zero failed requests** — confirm traffic is going through port 8080 (the edge), and that the kill event's timestamp falls between the load generator's first and last request.
- **Logs from old drills are cluttering everything** — `make clean` wipes generated state and logs; re-run `make seed` and `bash scripts/up_bare.sh` afterward.

## Stretch Goals

1. **Real MinIO** — run `docker compose up minio`, repeat the snapshot/failover flow with `--backend minio`, and compare `put`/`get` latency against the `fs` backend.
2. **Postgres PITR** — add a Postgres metadata store, use `pg_basebackup` + WAL archiving, restore to a specific point in time, and measure RTO for that layer separately (§3 PITR).
3. **Active-active** — keep both regions at `pool_state=full`, split traffic 50/50 at the edge, and design conflict resolution for concurrent ingest (§2 Active-Passive vs. Active-Active).
4. **Terraform (write-only)** — draft an `aws_s3_bucket_replication_configuration` resource that maps to what `put`/`get` do in `state/snapshot.py`, and to what `MANIFEST.json` versioning represents in a real bucket.
5. **Randomized chaos** — randomize the kill timing and mode (`stop` vs. `netblock`) across five runs; report mean RTO and standard deviation (§6 Chaos Engineering).
6. **DR maturity self-assessment** — rate your system on the Level 0–4 scale from the slides, and name the specific change needed to reach the next level.

## Reflection Questions

1. Your RTO is the sum of detection floor, snapshot restore, GPU warm-up, and DNS TTL cache. Which one could you shrink without increasing the risk of flapping — and what would that cost you?
2. If `dr/health_checker.py` ran in the same process as the serving API it monitors, who would raise the alarm when that process dies? Check: does your health checker import anything from `serving/`?
3. When someone asks "is our 5-minute RTO actually true?", which log or evidence file do you open to answer with a real number instead of a guess?
