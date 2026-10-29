# Strand Roadmap

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

## Phase 3b: Final Kernel Hardening
- [x] **KRN-1 (C1) No private-context laundering**: an exchange served with `X-Strand-Allow-Private: true` is never distilled (JSON and streaming); `DistillationEngine` also never rewrites an existing private entity.
- [x] **KRN-2 (C2) Ingress & TCP hardening**: `POST /v1/chat/completions` bodies are capped at 10 MiB (`MAX_REQUEST_BYTES`, HTTP 413, enforced on `Content-Length` and while streaming chunked bodies); pathologically nested JSON is HTTP 400; `daemon.main` logs a security warning when TCP binds beyond loopback with no `api_key`.
- [x] **KRN-3 (C3) Log leak prevention**: the "Incoming query" log line carries the length plus an 80-char preview of the *sanitized* query.
- [x] **KRN-4 (C4) Orphan vector sweep**: `run_migrations` deletes `entity_embeddings` rows with no matching entity whenever vec is enabled.
- [x] **KRN-5 (C6) Unified privacy predicate**: one `common.is_private(archetype, metadata)` (case-insensitive archetype, truthy `private` flag, fail-closed on corrupt metadata) used by `resolver`, `compactor` and `distill`; `db.PRIVATE_ENTITY_SQL` is its SQL twin inside the subgraph CTE, with a parity test.
- [x] **KRN-6 (C5, D1) Stream parsing resilience & reasoning tags**: `strip_think` removes `<think>`, `<thinking>`, `<thought>` and `<reasoning>` blocks; `_stream_text` extracts text from list-form `delta.content` parts instead of dropping the chunk.

## Phase 4: Interface & Interaction Layer (GUI)
- [x] **UI-1 Desktop Workbench (`strand-gui`)**: Native PySide6 / QML chat window communicating over UDS (`strand.sock`) or TCP. Includes diagnostics drawer (`Ctrl+D`), session rotation (`Ctrl+L`), abort (`Esc`), command history (`Up`/`Down`), markdown/code styling, and window geometry persistence.
- [x] **UI-2 Local Slash Commands**: `/new`, `/private <on|off>`, `/model <name>`, and `/help` intercepted client-side without polluting upstream context.
- [ ] **UI-3 System Tray & Global Hotkey**: Ambient background daemon presence with global hotkey launcher.

## Phase 5: The Personality Matrix & Interaction Tuning
- [x] **PER-1 Matrix Archetypes (`trait`, `directive`, `preference`)**: End-to-end substrate for behavioral memory. Permitted in schema, extracted by Model B (with automatic edge deprecation for conflicting rules), tracked by `SessionLedger` (injected once per session), and categorized hierarchically in `resolver.py`.
- [x] **PER-2 Bedrock Persona Anchor**: `persona_prompt` configured via `config.json` (`STRAND_PERSONA_PROMPT`), injected as synthetic entity `-1` on Turn 1 under `[Persona Stance]`.
- [x] **PER-3 Structured Proxy Metric Logs**: Log line accurately reports `entities=X edges=Y traits=Z directives=A preferences=B` or `(empty)`.

## Phase 6: Cognitive Partnership & Proactive Reasoning
- [x] **COG-1 Proactive Contradiction & Reality Guard**:
  - In `resolver.py`: Detect direct attribute conflicts between incoming prompt claims and grounded active edges (e.g. user specifies "5-speed clutch" for a car recorded with a 6-speed transmission).
  - Inject a specialized `[Reality Check]` warning into the session context delta to prompt the model to clarify discrepancies rather than blindly agree.
- [x] **COG-2 Conversational Grounding & Onboarding**:
  - Ambient first-run detection or `/onboard` / `/bootstrap` command.
  - Guided, organic conversational interview (subtle steering rather than rigid questionnaires) to seed the `User` (`Operator`) anchor node, hardware orbit (WORKSTATION, Cluster-Node-01), active projects, key collaborators, and core interaction stances.
- [x] **COG-3 Episodic Stitching & Project Continuity**:
  - Cross-reference recent uncompacted episodes (last 7 days) matching queried entities to inject recent working context into new sessions.
- [ ] **COG-4 Shared Lore & Episodic Vernacular**:
  - Defer until > 200 operational episodes accumulate.
  - Background compaction (`compactor.py`) distills shared debugging milestones and inside shorthand into entity lore metadata.