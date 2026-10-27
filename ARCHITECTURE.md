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
   - `entity_id` INTEGER PRIMARY KEY (vec0 virtual table, float[384])

## 3. Core Modules
- `src/cortext/db.py`: DatabaseManager handling connection, WAL mode, schema DDL, sqlite-vec extension.
- `src/cortext/resolver.py`: Fast entity scan (exact + vector) and 1-to-2 hop recursive graph walk.
- `src/cortext/session.py`: In-memory session delta cache (`seen_entity_ids = set()`).
- `src/cortext/proxy.py`: FastAPI application serving `/v1/chat/completions`.
