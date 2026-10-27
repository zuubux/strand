# Cortext Roadmap

Check items off as they land. Every phase must keep `pytest -W error` green, fully offline, at $0 API cost.

## Phase 1: Modularization & Architecture Hygiene
- [x] **M1 `common.py`**: single home for `EMBEDDING_DIM`, `MIN_EDGE_CONFIDENCE`, `MODEL_CONFIDENCE`, `MAX_SUMMARY_LEN`; shared `extract_json`, `load_metadata`, `aliases_of`, `clean_text`/`truncate`. `db`, `distill`, `resolver`, `extractors`, `compactor`, `embed` import from it.
- [x] **M2 `schema.py`**: `SCHEMA_SQL`, `VEC_SCHEMA_SQL` and `run_migrations(conn, vec_enabled)` carved out of `db.py`.
- [x] **M3 Public DB APIs**: `db.transaction()` replaces `db._transaction()` (used by `cli.py`, `distill.py`); `_aliases_of` removed in favor of `common.aliases_of`. `upsert_entity_deduped` is retained because the test suite exercises it directly.

## Phase 2: Security & Ingestion Hardening
- [x] **SEC-1 (C1) Prompt-injection & ingestion immunity**: assistant responses can never set `origin`/`confidence` above `model_inferred`/0.60 and can never issue `forgotten_entities` or `deprecated_edges`; conversational forgetting needs the target named in the user's own forget command. `parse_extraction` clamps model-emitted provenance.
- [x] **SEC-2 (C2) Streaming compression & line splitting**: upstream streaming requests send `Accept-Encoding: identity`, relay via `aiter_bytes()`, and split SSE on `\n` only so U+2028-style separators never truncate events.
- [x] **SEC-3 (C3) Fail-open memory resilience**: a failing `resolver.resolve` is logged, the session ledger is reset and the request is forwarded without context; `get_entity_subgraph` tolerates corrupt metadata JSON via `load_metadata`.
- [x] **SEC-4 (C4) API ingress protection & route lockdown**: `POST /v1/chat/completions` requires `application/json` (HTTP 415 otherwise); `/docs`, `/redoc`, `/openapi.json` are disabled.
- [x] **SEC-5 (C5) Sanitizer ReDoS protection & expanded patterns**: bounded URL-credentials regex, input length cap, and new detectors for Stripe live, Slack, AWS STS, GitHub `ghs_`/`gho_`/`ghu_`, HuggingFace tokens and unclosed PEM blocks.
- [x] **SEC-6 (C8) Strict configuration loading**: a corrupt `config.json` stops the daemon with a clear error (`load_config(strict=True)`); `Config.pick` uses `is not None` so explicit `0`, `0.0` and `""` are honored.

## Phase 3: Protocol Robustness & Daemon Lifecycle
- [x] **ROB-1 (C6) Socket Lifecycle & Filesystem Isolation**: `prepare_socket` only unlinks a dead unix socket (`S_ISSOCK` + liveness probe) and never a regular file or a live listener; the socket is pre-bound under `umask 0o177` and handed to uvicorn (no 0o666 race); parent `chmod 0700` tolerates `PermissionError`; `save_config_value` creates and chmods its parent dir to `0700`.
- [x] **ROB-2 (C7) Resilient Stream Lifecycle & Disconnect Teardown**: `StreamingResponse` carries `BackgroundTask(upstream.aclose)`; `relay` resets the ledger and shields `upstream.aclose()` on `GeneratorExit`/`CancelledError`; in-stream `error` events and streams that end without `finish_reason`/`[DONE]` are never distilled and reset the ledger; a mid-stream `HTTPError` yields an explicit SSE error event instead of a silent EOF.
- [x] **ROB-3 (D1, D2, D8) Upstream Provider Neutrality (Choices, Reasoning Blocks, Developer Role)**: `_stream_text` ignores choices with `index != 0`; `<think>...</think>` is stripped (`common.strip_think`) before distillation and LLM extraction; `_inject_delta` also targets the `developer` role.
- [x] **ROB-4 (D13, D14) Graph Traversal Privacy & Compactor Isolation**: `get_entity_subgraph` excludes private entities inside the recursive CTE, before `LIMIT`, so hidden nodes cannot consume hop/edge quota; the compactor's `_entities` omits private entities from the SLM prompt.
- [x] **ROB-5 (D24, D27, D28) Concurrency Throttling & Numerical Stability Guards**: background distillation is bounded by `asyncio.Semaphore(4)`; `SessionLedger` caps active sessions (`max_sessions=1000`, LRU eviction) and `session_id` is limited to 128 chars (HTTP 400 beyond); `effective_priority` returns the undecayed `confidence * weight` for a non-finite or non-positive half-life.

## Phase 4: Interface & Interaction Layer
- [ ] **UI-1** Terminal REPL
- [ ] **UI-2** Web Inspector
- [ ] **UI-3** OmniBar Overlay
