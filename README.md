# Day 23 — Disaster Recovery & High Availability for AI Infrastructure

**Lab format:** 2 hours · **Environment:** fully local, no cloud account required · **Core idea:** kill your own region first, then learn to survive it.

> RTO/RPO are numbers you *measure* from logs — not numbers you read off a slide.

---

## Overview

This lab simulates a two-region AI serving deployment entirely on your own machine:

| Component | Real-world equivalent | Local stand-in |
|---|---|---|
| Region A / Region B | AWS us-east-1 / us-west-2 | Two FastAPI processes on different ports |
| Vector DB | Pinecone / Weaviate / Qdrant | SQLite file per region |
| Model weight replication | S3 Cross-Region Replication | Filesystem snapshot (`state/_replica/`) |
| DNS / Global Load Balancer | Route53 health-check failover | A local proxy reading a `edge/active_region` pointer file |
| Region outage | Network partition / AZ failure | `chaos/kill_region.py` (`SIGSTOP` or `SIGKILL`) |

You will bring the stack up, watch it serve traffic normally, **kill Region A while it is live**, and measure — with real timestamps, not intuition — how long it takes users to notice and how long it takes to recover. Then you build the health check, failover, and runbook automation that turns "no recovery" into a measured, passing RTO.

No AWS account, no cloud spend, no Docker required for the graded path.

## Quick Start

```bash
pip install -r requirements.txt
make seed              # seed Region A (200 docs + weights), Region B empty
bash scripts/up_bare.sh
curl localhost:8080/v1/infer
```

Expected response: `"edge_region":"a"` and an answer starting with `"[a] ..."`.

Stop everything:

```bash
bash scripts/down_bare.sh
```

> **Docker mode** (optional, `docker compose up -d`) works for local exploration, but is **not** used for the graded drill — timing is not reproducible across machines. Every drill in [GUIDE.md](GUIDE.md) runs in bare mode with `--mock`.

## Repository Structure

```
serving/    Mock inference API per region (FastAPI). Readiness depends on pool state,
            model weights on disk, and vector count — not just "process is alive".
edge/       DNS/load-balancer stand-in. Routes by reading edge/active_region,
            cached for EDGE_TTL_SECONDS to simulate real DNS cache behavior.
state/      Seed, ingest, snapshot/restore, and replication for the vector DB
            and model weights.
chaos/      kill_region.py — induces `stop` (SIGKILL) or `netblock` (SIGSTOP)
            failure, with a --mock flag for reproducible grading.
loadgen/    traffic.py — continuous request generator. This is the RTO clock:
            every request is one timestamped line in a JSONL log.
dr/         ★ YOUR ASSIGNMENT — three skeletons to implement:
            health_checker.py, failover.py, runbook.py
tools/      measure_rto.py — computes RTO/RPO from logs and validates the drill.
tests/      Unit tests for your dr/ code, plus the evidence-gate tests used
            for grading.
reports/    Templates you fill in: runbook.md, rto-evidence.md, postmortem.md.
scripts/    up_bare.sh / down_bare.sh — start/stop the stack without Docker.
```

## Where to Go Next

| Document | Purpose |
|---|---|
| **[GUIDE.md](GUIDE.md)** | Full 2-hour walkthrough — setup, baseline, attack, containment, proof |
| **[RUBRIC.md](RUBRIC.md)** | Grading criteria, hard-fail conditions, and exact verification commands |

## Safety Notes

- Every target in this lab is `127.0.0.1` / `localhost`. Do not repoint `URL`, `UPSTREAM`, or the load generator at any external host.
- `--mock` mode only sends process signals (`SIGSTOP`/`SIGKILL`) and touches local files — nothing here reaches a real cloud region.
- The chaos script refuses to kill a region if the other one is already down, to prevent an accidental double outage. Overriding this (`--i-really-want-both`) marks the drill invalid for grading.
