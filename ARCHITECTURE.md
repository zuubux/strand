# Cortext Architecture Specification

## 1. Role & Identity
Cortext is a sovereign, local, headless cognitive working-memory daemon for Linux.
It intercepts `/v1/chat/completions` requests from local frontends (Aether OmniBar, CLI pipes, editors), resolves relevant entity subgraphs from an embedded SQLite graph, injects a deduplicated context delta into the prompt, and proxies the call to a chosen frontier LLM.

## 2. Storage Schema (`~/.config/cortext/memory.db`)
Using `aiosqlite` and `sqlite-vec`:

1. `entities`:
   - `id` INTEGER PRIMARY KEY AUTOINCREMENT
   - `name` TEXT UNIQUE NOT NULL
   - `archetype` TEXT NOT NULL (person, project, system, vehicle, concept)
   - `summary` TEXT DEFAULT ''
   - `metadata` JSON DEFAULT '{}'
   - `origin` TEXT NOT NULL DEFAULT 'user_explicit' ('user_explicit', 'model_inferred', 'system')
   - `confidence` REAL NOT NULL DEFAULT 1.0 (0.0-1.0; higher confidence wins on upsert, user_explicit is never displaced at equal confidence; resolver tags < 0.8 as `[inferred]`)
   - `created_at`, `updated_at` DATETIME

2. `edges`:
   - `id` INTEGER PRIMARY KEY AUTOINCREMENT
   - `source_id` INTEGER NOT NULL (FK -> entities.id)
   - `target_id` INTEGER NOT NULL (FK -> entities.id)
   - `relation` TEXT NOT NULL
   - `weight` REAL NOT NULL DEFAULT 1.0
   - `metadata` JSON DEFAULT '{}'
   - `origin` TEXT NOT NULL DEFAULT 'user_explicit' ('user_explicit', 'model_inferred', 'system')
   - `confidence` REAL NOT NULL DEFAULT 1.0 (0.0-1.0; higher confidence wins on upsert, user_explicit is never displaced at equal confidence; resolver tags < 0.8 as `[inferred]`)
   - UNIQUE(source_id, target_id, relation)

3. `episodes`:
   - `id` INTEGER PRIMARY KEY AUTOINCREMENT
   - `timestamp` REAL NOT NULL
   - `session_id` TEXT NOT NULL
   - `narrative` TEXT NOT NULL
   - `extracted_entities` JSON DEFAULT '[]'

4. `entity_embeddings`:
   - `entity_id` INTEGER PRIMARY KEY (vec0 virtual table, float[384]); written by `upsert_entity(embedding=...)`, queried by `find_similar_entities` (KNN, L2 distance) for the resolver's semantic tier, deleted with the entity.

## 3. Module Directory (`src/cortext/`)
- `db.py`: `DatabaseManager` (aiosqlite). Opens the DB, loads `sqlite-vec`, applies `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`, `busy_timeout=5000`, creates schema DDL (incl. `entity_embeddings` vec0 float[384]) and runs column migrations. Enforces POSIX modes at runtime: parent dir `0700`, DB/`-wal`/`-shm` files `0600` (chmod errors from non-owners are ignored). Provides `upsert_entity` (an optional 384-float embedding replaces the row in `entity_embeddings`, only when the assertion wins on provenance), `upsert_edge` (confidence/origin precedence), `find_similar_entities(query_embedding, limit=5, distance_threshold=1.0)` (sqlite-vec KNN, L2 distance: `WHERE embedding MATCH ? AND k = ? ORDER BY distance`, keeps `distance <= threshold`, returns entity ids nearest first), `get_entity_subgraph`, `record_episode`. Dropping an entity (`cli.py`) also deletes its `entity_embeddings` row.
- `embed.py`: `Embedder` protocol (`embed(text) -> list[float]`) and `EmbeddingManager`, a `fastembed.TextEmbedding` wrapper (default `BAAI/bge-small-en-v1.5`, 384 dims, matching `entity_embeddings`). `fastembed` is imported and the model built lazily on first `embed`, so imports, the CLI and tests never pay load cost. A `model_factory` or any custom `Embedder` can be injected for offline tests; wrong-dimension output raises `ValueError`.
- **Embedder wiring:** `daemon.build_app` builds one lazy `EmbeddingManager` and shares it with `ContextResolver(..., embedder=...)` (semantic tier) and `DatabaseManager(..., embedder=...)`, so daemon startup stays fast. When a `DatabaseManager` has an embedder and `upsert_entity` gets `embedding=None`, it embeds `f"{name}: {summary}"` (just `name` when the summary is empty; see `db.embedding_text`) off the event loop; an embedder failure is logged and never blocks the write. An explicit `embedding` always wins. `cortext embed-backfill` (`cli.py`) embeds every entity with no `entity_embeddings` row using the same text format and reports the count; re-running it is a no-op.
- `extractors.py`: Model B. `LLMExtractor(base_url, model_name, api_key="ollama")` is an async `Extractor` (`(query, response) -> extraction`) for `DistillationEngine`. It POSTs to a local OpenAI-compatible `{base_url}/chat/completions` (Ollama, vLLM) with a concise system prompt demanding strict JSON: `entities` (name, archetype, summary) and `edges` (source, target, relation). `parse_extraction` tolerates code fences and surrounding prose, drops malformed items, and any transport error, non-2xx status or unparsable JSON yields empty lists, so the exchange is still recorded as an episode. Output flows through the normal ingestion defenses (sanitization, grounding, provenance: `model_inferred` at 0.6, edge requirement). `daemon.build_app` uses it only when `extractor_model` is set (URL defaults to `http://127.0.0.1:11434/v1`); otherwise `heuristic_extract` stays the default. Distillation is a background task, so SLM latency never delays the proxied reply.
- `config.py`: Frozen `Config` dataclass. Resolution per field: env var > `~/.config/cortext/config.json` > default. Keys: `upstream_url`, `upstream_api_key`, `api_key`, `host`, `port`, `db_path`, `log_level`, `socket_path`, `extractor_model`, `extractor_url` (both default empty), `distance_threshold` (float, default `1.0`, max L2 distance for semantic matches) (env: `CORTEXT_<KEY>`). Defaults: host `127.0.0.1`, port `8088`, upstream `https://api.openai.com/v1`, `socket_path` empty. Transport detection (`effective_socket_path`, `is_socket_mode`, `get_bind_target`): explicit `socket_path` selects UDS; else UDS at `$XDG_RUNTIME_DIR/cortext/cortext.sock` when `XDG_RUNTIME_DIR` is set and host/port were not set via env or config.json (`tcp_explicit`, excluded from equality); else TCP. A malformed file is ignored on load. `save_config_value` writes atomically (temp file + `os.replace`, mode `0600`). Secret masking is performed by `cli.py` (`mask_secret`), not here.
- `daemon.py`: Entrypoint (`python -m cortext.daemon`). `build_app` wires `DatabaseManager`, `SessionLedger`, `ContextResolver`, `create_app`. `main` loads config, starts logging, then serves in socket mode or TCP mode (Section 5). Socket lifecycle: `prepare_socket` (parent dir `0700`, stale socket unlinked), uvicorn serves on `uds`, socket chmod `0600` once bound, socket unlinked on shutdown. TCP mode uses `uvicorn.run(host, port)`. uvicorn traps SIGINT/SIGTERM and runs the FastAPI lifespan: startup connects SQLite; shutdown drains distillation tasks, closes the HTTP client, closes SQLite.
- `proxy.py`: FastAPI streaming proxy: `GET /health` (open readiness probe) and `POST /v1/chat/completions`. Resolves a session delta from the last user message and prepends it to the first system message (or inserts one). Streams SSE upstream responses through unchanged. Optional constant-time inbound Bearer auth on `/v1/*` (Section 4). Reads `X-Cortext-Allow-Private` for egress scoping. Session id: body `session_id` > `X-Session-Id` header > `default`. Schedules background distillation after a successful exchange. On upstream failure it rolls back the session ledger (Section 6); there is no automatic upstream retry, the client retries and receives the full context again.
- `resolver.py`: `ContextResolver`. `match_entities` does an exact whole-word scan of entity names and `metadata.aliases` (casefolded), then a constituent-token fallback, then (only if an `embedder` is injected and fewer than `max_entities` matched) a vector-KNN tier via `db.find_similar_entities`. Matching invariant: Exact Whole-Word -> Constituent-Token -> Vector KNN; semantic hits only fill remaining slots, never displace lexical ones, are filtered by the same `allow_private` rule, and are skipped entirely (no embedding call) when the lexical tiers already fill `max_entities`. `gather` walks the graph 1 to 2 hops (`MAX_HOPS_LIMIT = 2`). `resolve` returns a markdown block of facts new to the session. Facts with confidence < 0.8 are tagged `[inferred, confidence: x.xx]`. Private entities are scoped out unless allowed (Section 4). Defaults: `max_entities=5`, `edge_limit=10`. Semantic matching uses sqlite-vec default **L2 (Euclidean)** distance, not cosine: unit vectors at cosine distance ~0.35 sit at L2 ~0.83, orthogonal noise at L2 >= 1.414. `distance_threshold` (default `1.0`, wired from `Config.distance_threshold` by `daemon.build_app`) keeps hits with `distance <= threshold`.
- `distill.py`: `DistillationEngine.process_exchange`: sanitize, substantive-turn gate, heuristic extraction (or injected extractor), grounding, persistence, episode record. Failures are logged, never raised. Enforces the edge requirement and constituent consolidation (Section 6). `strip_markdown_structure` removes heading lines and bold labels so formatting words never become entities.
- `sanitize.py`: `sanitize_text` replaces matches with `[REDACTED_SECRET]` for: PEM private key blocks, URIs with embedded credentials (DB URIs), JWTs, `Bearer` tokens, GitHub (`ghp_`, `github_pat_`), OpenAI-style `sk-`, Google `AIza`, AWS `AKIA`. `is_secret_token` flags candidate entity names that are redaction-affected or are high-entropy: length >= 24, no whitespace, contains letters and digits, Shannon entropy >= 3.5 bits/char.
- `stopwords.py`: Stop-word and filler lists, `is_valid_entity_name` (rejects stop-words/fillers; short names allowed via whitelist), `name_tokens`. Also used by the resolver to skip single-word stop-word concepts.
- `session.py`: `SessionLedger`: in-memory, per-session `seen_entity_ids` and `seen_edge_ids` (separate id spaces), idle TTL 3600 s, `filter_new` returns a `SessionDelta`, `reset`, `evict_expired`. Single event loop only; not persisted.
- `logging.py`: `cortext` logger tree. `RotatingFileHandler` (5 MiB, 2 backups) at `~/.local/share/cortext/cortext.log` (override `CORTEXT_LOG_PATH`). Directory forced to `0700`, log file to `0600` (re-applied after each rollover). Stderr stream only in debug on a TTY.
- `cli.py`: `cortext` operator tool (`[project.scripts]`), global `--db`. Commands: `stats`, `list [--archetype --limit]`, `inspect <name|id>`, `drop <name|id> [-y]`, `prune-stale [--confidence-below X]` (default 0.5, removes model_inferred facts), `config show|get|set` (`show` masks secrets), `logs [-n N]`.

## 4. Security & Privacy Architecture
- **POSIX storage isolation:** data and log directories `0700`; DB, WAL, SHM, log files and `config.json` `0600`. Applied at runtime by `db.py`, `logging.py`, `config.py`, not left to umask.
- **Inbound auth:** if `CORTEXT_API_KEY` (or `api_key` in config.json) is non-empty, every `/v1` and `/v1/*` request must send `Authorization: Bearer <key>`. It is compared with `hmac.compare_digest` and failure returns 401 `authentication_error`. `/health` is always open. If the key is empty, auth is disabled. The inbound key is distinct from `CORTEXT_UPSTREAM_API_KEY`. The upstream key, when set, is the only credential sent upstream. The client's `Authorization` header is forwarded only when neither key is configured.
- **Ingestion defense:** query and response pass through `sanitize_text` before substantive-turn gating, extraction and `record_episode`, so redacted text is what is stored. Entity names and edge endpoints that satisfy `is_secret_token` are dropped. The entropy heuristic applies to candidate names/endpoints only; free text is covered by the regex patterns.
- **Egress guardrails:** an entity is private if `archetype == 'private'` or `metadata['private'] is True`. Private entities, and edges touching them, are excluded from the delta and from graph traversal unless the request sends `X-Cortext-Allow-Private: true` (case-insensitive). The default is no private data to the LLM.

## 5. Transport
- **Desktop mode (UDS):** zero-network local IPC at `$XDG_RUNTIME_DIR/cortext/cortext.sock` (mode `0600`, parent dir `0700`), or any path given by `CORTEXT_SOCKET_PATH`.
- **Server/container mode (TCP):** `CORTEXT_HOST:CORTEXT_PORT`, chosen whenever host or port is set explicitly or `XDG_RUNTIME_DIR` is absent. The Docker image sets `CORTEXT_HOST=0.0.0.0`, so it uses TCP.
- **TCP details:** Served by uvicorn on `CORTEXT_HOST:CORTEXT_PORT` (default `127.0.0.1:8088`). Use this for Docker/remote deployments, with `CORTEXT_API_KEY` set when binding beyond loopback.
- **UDS details:** socket lifecycle is in the `daemon.py` entry (Section 3). systemd uses `RuntimeDirectory=cortext` and `%t/cortext` in `ReadWritePaths`.

## 6. Operational Invariants
- **Edge requirement:** a `model_inferred` entity persists only if it is an endpoint of at least one valid edge in the same extraction (valid = non-empty source/target/relation, not a self-loop, valid names, no secrets). Otherwise it is dropped. `user_explicit` entities are exempt.
- **Provenance:** extractions without attribution are `model_inferred` at confidence 0.6. On upsert, higher confidence wins and `user_explicit` is never displaced at equal confidence.
- **Upstream failure rollback:** since facts are marked seen when resolved, any upstream failure (connect/send error, status >= 400, body-read error, stream interrupted mid-relay) calls `ledger.reset(session_id)` so the next request replays the full context. Failed or interrupted exchanges are never distilled.
- **Alias matching:** exact = casefolded whole-word match of the entity name or any `metadata.aliases` entry (word boundary enforced only on word-character edges, so `C++` matches). Constituent = whole-word match of a single token of a name/alias (>= 3 chars, not a stop/function word). Ranking: confidence desc, exact before constituent, first mention, id.
- **Constituent consolidation (write side):** a single-word candidate with no entity of its own that equals an alias or token of exactly one multi-word entity (`BMW` -> `BMW 540i`) refreshes that entity's `updated_at` instead of creating a node. Ambiguous matches are not consolidated.
- **Turn gating:** trivial exchanges (short greeting/acknowledgment queries without a fact assertion, empty or boilerplate replies) are not distilled.
