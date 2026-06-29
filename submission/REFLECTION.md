# Day 23 Lab Reflection

**Student:** Dương Quang Khải
**Submission date:** 2026-06-29
**Lab repo URL:** https://github.com/Kaistory/Day23-Track2-Observability-Lab-DuongQuangKhai-2A202600708

> Run on Windows 10 + Docker Desktop (WSL2 backend), 12 logical cores / 15.3 GB host
> RAM. Host tooling uses a Python **3.13** venv (`.venv/`) because the pinned
> numpy/scipy/pandas wheels don't yet build on 3.14.

---

## 1. Hardware + setup output

Output of `python 00-setup/verify-docker.py` (`00-setup/setup-report.json`):

```json
{
  "docker": { "ok": true, "version": "29.0.1" },
  "compose_v2": { "ok": true, "version": "2.40.3-desktop.1" },
  "ram_gb_available": 1.86,
  "ram_ok": false,
  "required_ports": [8000, 9090, 9093, 3000, 3100, 16686, 4317, 4318, 8888],
  "bound_ports": [],
  "all_ports_free": true
}
```

`ram_gb_available` reads **1.86 GB** because Docker Desktop's WSL2 VM was capped at
`memory=2GB` in `~/.wslconfig`, not because the host is short on RAM (host has 15.3 GB).
The 7-container stack still runs comfortably inside that cap at steady state (~1 GB) plus
the 4 GB WSL swap as a cushion under load — every service reached `healthy`/ready and all
9 ports were free before `up`.

---

## 2. Track 02 — Dashboards & Alerts

**Load profile** (`make load`, baseline 10 users / 60 s):

| metric | value |
|---|---|
| requests | 1030 |
| failures | 0 (0.00%) |
| throughput | ~17.4 req/s |
| latency p50 / p99 / max | 170 ms / 260 ms / 2305 ms |

**Dashboards provisioned** (Grafana API `?query=Day 23`, all render against the pinned
`prometheus` datasource uid):

- AI Service Overview (Day 23) — `day23-ai-overview`
- SLO Burn Rate (Day 23) — `day23-slo`
- Cost & Tokens (Day 23) — `day23-cost-tokens`
- Cross-Day Stack (Day 23 integrative) — `day23-cross-day`

Overview screenshot → `submission/screenshots/dashboard-overview.png`,
burn-rate → `submission/screenshots/slo-burn-rate.png`,
cost/$-per-hr → `submission/screenshots/cost-and-tokens.png`.

### Alert fire + resolve

Measured end-to-end with `scripts/trigger-alert.sh` (ServiceDown dwell tuned to `for: 30s`
so it fires reliably inside the 90 s demo window):

| When | What | Evidence |
|---|---|---|
| T0 | killed `day23-app` (`docker stop`) | `alertmanager-firing.png` |
| **T0 + 85 s** | `ServiceDown` (severity=critical) became **active** in Alertmanager (`startsAt 2026-06-29T02:37:22Z`) | `slack-firing.png` |
| T1 | restored app (`docker start`) | — |
| **T1 + 55 s** | alert **resolved** (0 active alerts) | `slack-resolved.png` |

> Slack delivery: Alertmanager **does not expand env vars inside its config file**, so the
> original `api_url: '{{ env "SLACK_WEBHOOK_URL" }}'` made the container crash-loop with
> `unsupported scheme "" for URL`. I kept the env-driven design but made it work: the
> container entrypoint writes `$SLACK_WEBHOOK_URL` to `/tmp/slack_webhook_url` at startup
> and the config references it via `slack_api_url_file`. With the placeholder webhook the
> alert still fires/resolves *in Alertmanager* (proven above); drop a real webhook into
> `.env` to also see it land in `#observability` / `#oncall`.

### One thing that surprised me about Prometheus / Grafana

That a dashboard JSON which "looks correct" can silently render **nothing**: every panel
referenced datasource `uid: prometheus`, but provisioning auto-generated a random uid
(`PBFA97CFB590B2093`) because `datasources.yml` never pinned one. Pinning `uid: prometheus`
(and `uid: loki`) was the difference between "No data" and live panels — a reminder that in
dashboards-as-code the datasource **uid is part of the contract**, not an implementation detail.

---

## 3. Track 03 — Tracing & Logs

### One trace from Jaeger

Retained full-tree trace `d8a30a034276d150bebd4007b65a0325` (service `inference-api`):

```
predict                       (root)
├── embed-text                text.length=…
├── vector-search             k=5
└── generate-tokens           gen_ai.usage.input_tokens / output_tokens / finish_reason=stop
```

Screenshot → `submission/screenshots/jaeger-trace.png`. The `gen_ai.*` attributes follow
the **OTel GenAI semantic conventions** (`gen_ai.request.model`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.response.finish_reason`).

> **Bug I had to fix to get this:** the handler created the parent with
> `tracer.start_span("predict")` but never made it the *current* span, so each
> `start_as_current_span(...)` child had no parent context and was exported as its own
> **single-span** trace (Jaeger showed dozens of orphan `embed-text` / `vector-search`
> traces). Switching the parent to `start_as_current_span("predict")` nests the three
> stages under one trace — see §6.

### Log line correlated to trace

Structured JSON log from `day23-app` stdout; `trace_id` matches the value returned in the
`/predict` response body (same request):

```json
{"model": "llama3-mock", "input_tokens": 4, "output_tokens": 56, "quality": 0.846,
 "duration_seconds": 0.1827, "trace_id": "640b82a5e8f135f3e54e45b6ef3ffc45",
 "event": "prediction served", "level": "info", "timestamp": "2026-06-29T02:27:04.243807Z"}
```

The Loki datasource's `derivedFields` regex (`"trace_id":"([a-fA-F0-9]+)"`) turns that field
into a click-through to the Jaeger trace — logs↔traces correlation without copy-paste.

### Tail-sampling math

The collector keeps a trace if **any** policy matches: `keep-errors` (status=ERROR),
`keep-slow` (latency > 2000 ms), or `probabilistic` (1% of everything else). For a stream of
`N` traces/s with error fraction `e` and slow fraction `s`, the retained rate is:

```
R = N · [ e + s + 0.01·(1 − e − s) ]
```

- **Baseline run** (`ERROR_RATE=0`, p99=260 ms, only a handful of ~2.3 s cold-start outliers):
  e≈0, s≈0.003 → `R ≈ N·(0.003 + 0.01·0.997) ≈ 0.013·N`. At N≈17.4 traces/s that's
  **~0.23 traces/s kept (~1.3 %)** → ~13 traces over the 60 s run, which matches the
  ~7–10 full-tree traces retained in Jaeger.
- **Forced-error burst** (`fail:true`, e≈0.2): `R ≈ 0.2 + 0.01·0.8 ≈ 0.21` → **~21 % kept**,
  and ~100 % of the *error* traces survive. That is the whole point: you discard 99 % of
  healthy noise but keep essentially every diagnostically valuable trace. The 5 forced-error
  requests I sent were all retained (keep-errors), while the bulk of healthy traffic was dropped.

---

## 4. Track 04 — Drift Detection

`04-drift-detection/reports/drift-summary.json`:

```json
{
  "prompt_length":    { "psi": 3.461,  "kl": 1.7982,  "ks_stat": 0.702, "ks_pvalue": 0.0,      "drift": "yes" },
  "embedding_norm":   { "psi": 0.0187, "kl": 0.0324,  "ks_stat": 0.052, "ks_pvalue": 0.133853, "drift": "no"  },
  "response_length":  { "psi": 0.0162, "kl": 0.0178,  "ks_stat": 0.056, "ks_pvalue": 0.086899, "drift": "no"  },
  "response_quality": { "psi": 8.8486, "kl": 13.5011, "ks_stat": 0.941, "ks_pvalue": 0.0,      "drift": "yes" }
}
```

`prompt_length` (mean shifted 50→85) and `response_quality` (beta(8,2)→beta(2,6), i.e. the
eval-as-metric flipping from mostly-good to mostly-bad) both blow past the PSI=0.2 threshold;
the two unchanged features stay near zero with non-significant KS p-values.

### Which test fits which feature?

| feature | test I'd use in prod | why |
|---|---|---|
| `prompt_length` | **PSI** (KS as significance backstop) | continuous, unimodal; PSI is cheap to compute on a schedule and has an industry-standard 0.1/0.2 threshold. PSI=3.46 is unambiguous. |
| `embedding_norm` | **KS** 2-sample | near-constant, low-variance continuous — coarse PSI bins miss subtle location/scale shifts; KS compares full CDFs and is most sensitive here. |
| `response_length` | **PSI** | same family as `prompt_length`; routine binned monitoring with a fixed threshold. |
| `response_quality` | **KL divergence** (PSI also flags it) | a bounded [0,1] eval score whose *shape* inverts; KL captures the full distributional change, not just a mean shift. This is the 4th-pillar signal that maps to `inference_quality_score` + the `InferenceQualityDrop` alert. |

**MMD** isn't needed for any of these 1-D scalars — it's a kernel two-sample test for
**high-dimensional** drift (e.g. the raw embedding *vectors*, of which `embedding_norm` is
only the scalar norm). I'd reserve MMD for embedding-space drift where per-dimension PSI/KS
is intractable.

---

## 5. Track 05 — Cross-Day Integration

The `Cross-Day Stack (Day 23 integrative)` dashboard renders all 6 panels; with no prior-day
services running locally they fail-soft to "No Data" (the panels resolve the `prometheus`
datasource correctly — they simply have no series yet).

### Which prior-day metric was hardest to expose? Why?

**Day 20 (llama.cpp model serving)** is the hardest. Host- and infra-level signals (Day 16
via `node_exporter`) are basically free, but model-serving internals — tokens/sec, KV-cache
occupancy, and especially **GPU utilization** — aren't a counter the process just emits.
llama.cpp's server exposes only a thin (build-dependent) `/metrics`, GPU util needs a
separate DCGM / nvidia-smi exporter, and on this Windows/WSL2 lab with no NVIDIA GPU the
signal is *absent entirely*, so it degrades to the `monitor-day20-llama-cpp.py` stub. The
general rule: the closer a metric sits to model/hardware internals, the more plumbing it
takes to surface as a clean Prometheus series. (Day 19 `recall@k` is hard for a different
reason — it's a *quality* metric that needs a labeled eval set, not something Qdrant emits.)

---

## 6. The single change that mattered most

**Making the `predict` span the *active* parent span** (`tracer.start_span("predict")` →
`with tracer.start_as_current_span("predict") as span:`).

Before the change, every signal *looked* healthy — metrics scraped, dashboards drew, traces
appeared in Jaeger. But the traces were a lie: because the parent span was created but never
pushed onto the OTel context, each of `embed-text`, `vector-search`, and `generate-tokens`
was emitted as its **own single-span trace** with a different `trace_id`. Jaeger showed
hundreds of orphan one-span traces. You could not answer the only question a trace exists to
answer — *for one slow request, which stage cost the time?* — because there was no single
request to look at. The telemetry was being emitted; it just wasn't **useful**.

Activating the parent span so the three stages nest under one `trace_id` turned that pile of
orphans into a real request waterfall: one trace, `predict → embed → search → generate`, with
the `gen_ai.*` token attributes on the generate span. This is exactly deck §7's point that a
trace is only worth anything if its **causal structure** is intact — context propagation is
the feature, not the span. The same fix unlocked the tail-sampling story in §3/checkpoint 14:
with the parent active, a forced failure marks the `predict` span `ERROR`, which is precisely
the signal the collector's `keep-errors` policy keys on to retain the traces worth keeping and
drop the 99 % that aren't. One-line change in altitude — from "spans are being produced" to
"I can debug a request and sample intelligently" — which is the whole difference between
*works* and *useful*.
