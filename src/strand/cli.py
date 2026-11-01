"""Operator CLI: inspect and administer the Strand memory database without raw SQL.

Talks to SQLite directly (WAL mode), so the daemon does not need to be running.
"""

import argparse
import asyncio
import json
import os
import stat
import sys
import uuid
from collections import deque
from collections.abc import Sequence
from typing import Any

import sqlite_vec

import httpx

from .compactor import SessionCompactor
from .config import ENV_VARS, load_config, save_config_value
from .db import DatabaseManager, embedding_text
from .embed import (
    DEFAULT_MODEL,
    UNAVAILABLE_MESSAGE,
    Embedder,
    EmbeddingManager,
    EmbeddingUnavailable,
    fastembed_available,
    fastembed_cache_dir,
    model_cache_dir,
)
from .extractors import DEFAULT_EXTRACTOR_URL
from .logging import default_log_path

PING_TIMEOUT = 0.5


async def _resolve(db: DatabaseManager, ref: str) -> dict[str, Any] | None:
    """Find an entity by exact name (case-insensitive), falling back to numeric id."""
    async with db.conn.execute("SELECT * FROM entities WHERE name = ? COLLATE NOCASE", (ref,)) as cur:
        row = await cur.fetchone()
    if row is None and ref.isdigit():
        async with db.conn.execute("SELECT * FROM entities WHERE id = ?", (int(ref),)) as cur:
            row = await cur.fetchone()
    return dict(row) if row is not None else None


async def _scalar(db: DatabaseManager, sql: str, params: Sequence[Any] = ()) -> int:
    async with db.conn.execute(sql, params) as cur:
        row = await cur.fetchone()
    return int(row[0])


async def cmd_stats(db: DatabaseManager, args: argparse.Namespace) -> int:
    print(f"entities: {await _scalar(db, 'SELECT COUNT(*) FROM entities')}")
    for origin in ("user_explicit", "model_inferred"):
        count = await _scalar(db, "SELECT COUNT(*) FROM entities WHERE origin = ?", (origin,))
        print(f"  {origin}: {count}")
    print(f"edges: {await _scalar(db, 'SELECT COUNT(*) FROM edges')}")
    print(f"episodes: {await _scalar(db, 'SELECT COUNT(*) FROM episodes')}")
    return 0


async def cmd_list(db: DatabaseManager, args: argparse.Namespace) -> int:
    sql = (
        "SELECT e.id, e.name, e.archetype, e.confidence, e.origin, "
        "(SELECT COUNT(*) FROM edges WHERE source_id = e.id OR target_id = e.id) AS edge_count "
        "FROM entities AS e"
    )
    params: list[Any] = []
    if args.archetype:
        sql += " WHERE e.archetype = ?"
        params.append(args.archetype)
    sql += " ORDER BY e.id LIMIT ?"
    params.append(args.limit)
    async with db.conn.execute(sql, params) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        print("no entities found")
        return 0
    table = [("ID", "NAME", "ARCHETYPE", "CONF", "ORIGIN", "EDGES")]
    table += [
        (str(r["id"]), r["name"], r["archetype"], f"{r['confidence']:.2f}", r["origin"], str(r["edge_count"]))
        for r in rows
    ]
    widths = [max(len(line[i]) for line in table) for i in range(len(table[0]))]
    for line in table:
        print("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip())
    return 0


async def cmd_inspect(db: DatabaseManager, args: argparse.Namespace) -> int:
    entity = await _resolve(db, args.entity)
    if entity is None:
        print(f"error: entity not found: {args.entity}", file=sys.stderr)
        return 1
    metadata = json.loads(entity["metadata"] or "{}")
    if not isinstance(metadata, dict):
        metadata = {"value": metadata}
    aliases = metadata.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [aliases]
    print(f"[{entity['id']}] {entity['name']}")
    print(f"  archetype:  {entity['archetype']}")
    print(f"  origin:     {entity['origin']}")
    print(f"  confidence: {entity['confidence']:.2f}")
    print(f"  aliases:    {', '.join(map(str, aliases)) if aliases else '-'}")
    print(f"  created:    {entity['created_at']}")
    print(f"  updated:    {entity['updated_at']}")
    print(f"  metadata:   {json.dumps(metadata, sort_keys=True)}")
    print(f"  summary:    {entity['summary'] or '-'}")

    edge_sql = (
        "SELECT ed.relation, ed.weight, ed.origin, ed.confidence, o.name AS other "
        "FROM edges AS ed JOIN entities AS o ON o.id = ed.{other} "
        "WHERE ed.{own} = ? ORDER BY ed.confidence DESC, ed.weight DESC, o.name"
    )
    for title, own, other, arrow in (
        ("outgoing", "source_id", "target_id", "-[{rel}]->"),
        ("incoming", "target_id", "source_id", "<-[{rel}]-"),
    ):
        async with db.conn.execute(edge_sql.format(own=own, other=other), (entity["id"],)) as cur:
            rows = await cur.fetchall()
        print(f"  {title} edges ({len(rows)}):")
        for r in rows:
            print(
                f"    {arrow.format(rel=r['relation'])} {r['other']}  "
                f"weight={r['weight']:.2f} origin={r['origin']} confidence={r['confidence']:.2f}"
            )
    return 0


async def _delete_entities(db: DatabaseManager, ids: Sequence[int]) -> tuple[int, int]:
    """Delete entities with their edges and embeddings; return (entities, edges) removed."""
    edges = entities = 0
    async with db.transaction() as conn:
        for entity_id in ids:
            cur = await conn.execute(
                "DELETE FROM edges WHERE source_id = ? OR target_id = ?", (entity_id, entity_id)
            )
            edges += cur.rowcount
            if db.vec_enabled:
                await conn.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (entity_id,))
            cur = await conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))
            entities += cur.rowcount
    return entities, edges


async def cmd_drop(db: DatabaseManager, args: argparse.Namespace) -> int:
    entity = await _resolve(db, args.entity)
    if entity is None:
        print(f"error: entity not found: {args.entity}", file=sys.stderr)
        return 1
    if not args.yes:
        degree = await _scalar(
            db,
            "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
            (entity["id"], entity["id"]),
        )
        prompt = f"Drop '{entity['name']}' (id {entity['id']}) and {degree} edge(s)? [y/N] "
        try:
            answer = input(prompt)
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1
    _, edges = await _delete_entities(db, [entity["id"]])
    print(f"dropped entity '{entity['name']}' (id {entity['id']}) and {edges} edge(s)")
    return 0


async def cmd_prune_stale(db: DatabaseManager, args: argparse.Namespace) -> int:
    """Purge low-confidence model_inferred edges, then model_inferred entities left orphaned."""
    threshold = args.confidence_below
    async with db.transaction() as conn:
        cur = await conn.execute(
            "DELETE FROM edges WHERE origin = 'model_inferred' AND confidence < ?", (threshold,)
        )
        edges = cur.rowcount
    async with db.conn.execute(
        "SELECT id FROM entities WHERE origin = 'model_inferred' AND confidence < ? "
        "AND NOT EXISTS (SELECT 1 FROM edges WHERE source_id = entities.id OR target_id = entities.id)",
        (threshold,),
    ) as cur:
        orphan_ids = [row[0] for row in await cur.fetchall()]
    entities, _ = await _delete_entities(db, orphan_ids)
    print(f"pruned {entities} entit(y/ies) and {edges} edge(s) below confidence {threshold}")
    return 0


async def cmd_prune_decayed(db: DatabaseManager, args: argparse.Namespace) -> int:
    """Remove deprecated and cold (unaccessed for `--days`, rarely used) edges atomically."""
    edges, entities = await db.prune_decayed_edges(args.days, dry_run=args.dry_run)
    verb = "would prune" if args.dry_run else "pruned"
    print(f"{verb} {edges} decayed edge(s) and {entities} orphaned entit(y/ies) (older than {args.days:g} days)")
    return 0


async def cmd_embed_backfill(
    db: DatabaseManager, args: argparse.Namespace, embedder: Embedder | None = None
) -> int:
    """Embed every entity that has no row in `entity_embeddings`."""
    if not db.vec_enabled:
        print("error: sqlite-vec is unavailable; vector indexing is disabled", file=sys.stderr)
        return 1
    async with db.conn.execute(
        "SELECT id, name, summary FROM entities "
        "WHERE id NOT IN (SELECT entity_id FROM entity_embeddings) ORDER BY id"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        print("embedded 0 entit(y/ies); nothing to backfill")
        return 0
    embedder = embedder or getattr(args, "embedder", None) or EmbeddingManager()
    done = 0
    for row in rows:
        try:
            vector = await asyncio.to_thread(embedder.embed, embedding_text(row["name"], row["summary"]))
        except EmbeddingUnavailable:
            print(f"error: {UNAVAILABLE_MESSAGE} Embedded {done} entit(y/ies) before stopping.", file=sys.stderr)
            return 1
        async with db.transaction() as conn:
            await conn.execute("DELETE FROM entity_embeddings WHERE entity_id = ?", (row["id"],))
            await conn.execute(
                "INSERT INTO entity_embeddings (entity_id, embedding) VALUES (?, ?)",
                (row["id"], sqlite_vec.serialize_float32(vector)),
            )
        done += 1
    print(f"embedded {done} entit(y/ies)")
    return 0


async def _ping(url: str) -> bool:
    """True if anything answers HTTP at `url` within PING_TIMEOUT (any status counts)."""
    try:
        async with httpx.AsyncClient(timeout=PING_TIMEOUT) as client:
            await client.get(url)
        return True
    except Exception:
        return False


def _check(ok: bool | None) -> str:
    return "[ok]  " if ok else ("[warn]" if ok is None else "[fail]")


async def cmd_doctor(db: DatabaseManager, args: argparse.Namespace) -> int:
    """Print a health checklist. Degraded components are warnings, never errors."""
    config = load_config()
    async with db.conn.execute("PRAGMA journal_mode") as cur:
        mode = str((await cur.fetchone())[0]).lower()
    print("Strand doctor")
    print(f"{_check(mode == 'wal')} database: {db.db_path} (journal_mode={mode})")
    vec = db.vec_enabled
    print(f"{_check(vec)} sqlite-vec: {'loaded' if vec else 'not loaded; vector indexing disabled'}")

    if fastembed_available():
        cached = model_cache_dir()
        print(f"{_check(True)} fastembed: installed")
        if cached:
            print(f"{_check(True)} model cache: {DEFAULT_MODEL} cached at {cached}")
        else:
            print(f"{_check(None)} model cache: {DEFAULT_MODEL} not cached in {fastembed_cache_dir()} (downloads on first use)")
    else:
        print(f"{_check(None)} fastembed: not installed; {UNAVAILABLE_MESSAGE}")
        print(f"{_check(None)} model cache: n/a")

    base = (config.extractor_url or DEFAULT_EXTRACTOR_URL).rstrip("/")
    reachable = await _ping(base + "/models")
    label = f"extractor SLM: {base}"
    if config.extractor_model:
        label += f" (model {config.extractor_model})"
    if reachable:
        print(f"{_check(True)} {label} reachable")
    elif config.extractor_model:
        print(f"{_check(False)} {label} unreachable; heuristic extraction only")
    else:
        print(f"{_check(None)} {label} unreachable (none configured; heuristic extraction in use)")

    kind, target = config.get_bind_target()
    transport = f"uds {target}" if kind == "uds" else f"tcp {target}:{config.port}"
    print(f"{_check(True)} transport: {transport}")
    return 0


SECRET_KEYS = ("upstream_api_key", "api_key")


def mask_secret(value: str) -> str:
    """Mask an API key as `sk-...abcd`; short secrets are fully hidden."""
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}...{value[-4:]}"


def cmd_config(args: argparse.Namespace) -> int:
    if args.config_command == "set":
        try:
            save_config_value(args.key, args.value)
        except KeyError as exc:
            print(f"error: {exc.args[0]}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        shown = mask_secret(args.value) if args.key in SECRET_KEYS else args.value
        print(f"set {args.key} = {shown}")
        return 0
    config = load_config()
    values = {key: getattr(config, key) for key in ENV_VARS}
    if args.config_command == "get":
        if args.key not in values:
            print(f"error: unknown config key '{args.key}'; valid keys: {', '.join(values)}", file=sys.stderr)
            return 1
        print(values[args.key])
        return 0
    for secret in SECRET_KEYS:
        values[secret] = mask_secret(values[secret])
    for key, value in values.items():
        print(f"{key}: {value}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from . import daemon

    daemon.main(["--diag"] if args.diag else [])
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    path = default_log_path()
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            lines = deque(fh, maxlen=args.tail)
    except FileNotFoundError:
        print(f"no log file at {path}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line.rstrip("\n"))
    return 0


async def cmd_compact(db: DatabaseManager, args: argparse.Namespace) -> int:
    config = load_config()
    compactor = SessionCompactor(
        db,
        config.extractor_url or DEFAULT_EXTRACTOR_URL,
        config.extractor_model,
        threshold_hours=config.compaction_threshold_hours,
    )
    hours = config.compaction_threshold_hours if args.hours is None else args.hours
    if args.session:
        sessions = [args.session]
    else:
        sessions = await db.get_uncompacted_sessions(hours * 3600.0)
    if not sessions:
        print("no sessions to compact")
        return 0
    compacted = 0
    for session_id in sessions:
        result = await compactor.compact_session(session_id)
        if result["skipped"]:
            print(f"Skipped session '{session_id}' (extractor unavailable or failed)")
        elif not result["episodes"]:
            print(f"Session '{session_id}' has no uncompacted episodes")
        else:
            compacted += 1
            print(
                f"Compacted session '{session_id}' ({result['episodes']} episodes, "
                f"{result['summaries_updated']} summaries updated)"
            )
    print(f"{compacted} of {len(sessions)} session(s) compacted")
    return 0


CHAT_PATH = "/v1/chat/completions"
CHAT_FALLBACK_MODEL = "gpt-4o-mini"
PRIVATE_HEADER = "X-Strand-Allow-Private"
CHAT_HELP = (
    "/help             show this help\n"
    "/new              start a fresh session\n"
    "/private on|off   send (or stop sending) X-Strand-Allow-Private\n"
    "/stats            show memory entity/edge/episode counts\n"
    "exit | quit       leave the chat (Ctrl+C aborts a stream; twice exits)"
)


def _chat_client(config) -> httpx.Client:
    """HTTP client for the daemon: its UNIX socket when present, else loopback TCP."""
    timeout = httpx.Timeout(None, connect=5.0)
    path = config.effective_socket_path()
    if path:
        try:
            if stat.S_ISSOCK(os.stat(path).st_mode):
                return httpx.Client(transport=httpx.HTTPTransport(uds=path), base_url="http://strand", timeout=timeout)
        except OSError:
            pass
    return httpx.Client(base_url=f"http://{config.host}:{config.port}", timeout=timeout)


def _sse_token(line: str) -> tuple[str, str]:
    """Classify one SSE line as ("token", text), ("done", ""), ("error", msg) or ("skip", "")."""
    line = line.strip()
    if not line.startswith("data:"):
        return "skip", ""
    payload = line[5:].strip()
    if payload == "[DONE]":
        return "done", ""
    try:
        data = json.loads(payload)
    except ValueError:
        return "skip", ""
    if not isinstance(data, dict):
        return "skip", ""
    err = data.get("error")
    if err:
        return "error", str(err.get("message") if isinstance(err, dict) else err) or "upstream error"
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta") or {}
        content = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(content, str) and content:
            return "token", content
    return "skip", ""


def _chat_stream(client: httpx.Client, body: dict[str, Any], headers: dict[str, str]) -> tuple[str, str | None]:
    """Stream one turn to stdout; returns (reply_text, error). KeyboardInterrupt propagates."""
    parts: list[str] = []
    with client.stream("POST", CHAT_PATH, json=body, headers=headers) as resp:
        if resp.status_code >= 400:
            detail = resp.read().decode("utf-8", "replace").strip()
            return "", f"HTTP {resp.status_code}: {detail[:300]}" if detail else f"HTTP {resp.status_code}"
        for line in resp.iter_lines():
            kind, text = _sse_token(line)
            if kind == "token":
                parts.append(text)
                sys.stdout.write(text)
                sys.stdout.flush()
            elif kind == "error":
                return "".join(parts), text
            elif kind == "done":
                break
    return "".join(parts), None


def run_chat(args: argparse.Namespace, client: httpx.Client, config, read=input) -> int:
    """Interactive REPL. `read` is the line reader (injectable for tests)."""
    model = (args.model or config.model_default or CHAT_FALLBACK_MODEL).strip()
    private = bool(args.private)
    session_id = f"cli-{uuid.uuid4().hex[:12]}"
    history: list[dict[str, str]] = []
    interrupts = 0
    print(f"strand chat: model {model}, session {session_id}. /help for commands.")
    while True:
        try:
            line = read("you> ").strip()
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print()
            interrupts += 1
            if interrupts >= 2:
                return 0
            print("(press Ctrl+C again or type exit to quit)")
            continue
        interrupts = 0
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            return 0
        if line.startswith("/") and " " not in line[1:2] and line[1:2].isalpha():
            name, _, arg = line[1:].partition(" ")
            name, arg = name.lower(), arg.strip().lower()
            if name == "help":
                print(CHAT_HELP)
            elif name == "new":
                history.clear()
                session_id = f"cli-{uuid.uuid4().hex[:12]}"
                print(f"Started a new session ({session_id}).")
            elif name == "private":
                if arg in ("on", "off"):
                    private = arg == "on"
                    print(f"Private context {'enabled' if private else 'disabled'}.")
                else:
                    print(f"Private mode is {'on' if private else 'off'}. Usage: /private on|off")
            elif name == "stats":
                try:
                    asyncio.run(_chat_stats(args))
                except Exception as exc:  # noqa: BLE001 - the REPL must survive a bad DB
                    print(f"error: cannot read stats: {exc}", file=sys.stderr)
            else:
                print(f"Unknown command /{name}.\n{CHAT_HELP}")
            continue
        history.append({"role": "user", "content": line})
        headers = {"Content-Type": "application/json", "X-Session-Id": session_id}
        if private:
            headers[PRIVATE_HEADER] = "true"
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        body = {"model": model, "messages": list(history), "stream": True, "session_id": session_id}
        try:
            reply, error = _chat_stream(client, body, headers)
        except KeyboardInterrupt:
            history.pop()
            interrupts = 1
            print("\n[aborted]")
            continue
        except httpx.ConnectError as exc:
            history.pop()
            print(f"error: cannot reach Strand daemon: {exc}", file=sys.stderr)
            continue
        except httpx.HTTPError as exc:
            history.pop()
            print(f"error: request failed: {exc}", file=sys.stderr)
            continue
        print()
        if error is not None:
            history.pop()
            print(f"error: {error}", file=sys.stderr)
        elif reply:
            history.append({"role": "assistant", "content": reply})


async def _chat_stats(args: argparse.Namespace) -> None:
    async with DatabaseManager(args.db or load_config().db_path) as db:
        await cmd_stats(db, args)


def cmd_chat(args: argparse.Namespace) -> int:
    config = load_config()
    try:
        import readline  # noqa: F401 - enables line editing and history for input()
    except ImportError:
        pass
    with _chat_client(config) as client:
        return run_chat(args, client, config)


def _confidence(value: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0.0 and 1.0")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if not (number > 0 and number != float("inf")):
        raise argparse.ArgumentTypeError("must be a finite number > 0")
    return number


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strand", description="Inspect and administer Strand memory.")
    parser.add_argument("--db", help="database path (default: $STRAND_DB_PATH or ~/.config/strand/memory.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stats", help="show entity/edge/episode counts").set_defaults(func=cmd_stats)

    p = sub.add_parser("list", help="list entities")
    p.add_argument("--archetype", help="only entities of this archetype")
    p.add_argument("--limit", type=_positive, default=50, help="max rows (default 50)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("inspect", help="show an entity and its 1-hop edges")
    p.add_argument("entity", metavar="name_or_id")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("drop", help="delete an entity and its edges")
    p.add_argument("entity", metavar="name_or_id")
    p.add_argument("--yes", "-y", action="store_true", help="skip confirmation")
    p.set_defaults(func=cmd_drop)

    p = sub.add_parser("prune-stale", help="purge low-confidence model-inferred facts")
    p.add_argument("--confidence-below", type=_confidence, default=0.5, metavar="X")
    p.set_defaults(func=cmd_prune_stale)

    p = sub.add_parser("prune-decayed", help="remove deprecated and cold, rarely used edges")
    p.add_argument("--days", type=_positive_float, default=90.0, metavar="DAYS", help="idle age (default 90)")
    p.add_argument("--dry-run", action="store_true", help="report without deleting")
    p.set_defaults(func=cmd_prune_decayed)

    sub.add_parser("embed-backfill", help="embed entities missing from entity_embeddings").set_defaults(
        func=cmd_embed_backfill
    )

    p = sub.add_parser("compact", help="synthesize inactive sessions' episodes into entity summaries")
    p.add_argument("--hours", type=float, metavar="FLOAT", help="inactivity threshold override (hours)")
    p.add_argument("--session", metavar="TEXT", help="compact this session regardless of age")
    p.set_defaults(func=cmd_compact)

    sub.add_parser("doctor", help="check DB, sqlite-vec, fastembed, extractor and transport").set_defaults(
        func=cmd_doctor
    )

    config = sub.add_parser("config", help="show or change persistent settings")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="print the active configuration (API key masked)")
    p = config_sub.add_parser("get", help="print the raw value of one setting")
    p.add_argument("key")
    p = config_sub.add_parser("set", help="persist a setting to config.json")
    p.add_argument("key")
    p.add_argument("value")
    config.set_defaults(func=cmd_config, local=True)

    p = sub.add_parser("daemon", help="run the daemon in the foreground (quiet unless --diag)")
    p.add_argument("--diag", "--debug", dest="diag", action="store_true", help="verbose colored logs on the terminal")
    p.set_defaults(func=cmd_daemon, local=True)

    p = sub.add_parser("chat", help="interactive terminal chat with the running daemon")
    p.add_argument("--model", help="model name (default: config model_default)")
    p.add_argument("--private", action="store_true", help="start with private context enabled")
    p.set_defaults(func=cmd_chat, local=True)

    p = sub.add_parser("logs", help="print the end of the Strand log file")
    p.add_argument("--tail", "-n", type=_positive, default=50, metavar="N", help="lines to show (default 50)")
    p.set_defaults(func=cmd_logs, local=True)
    return parser


async def _run(args: argparse.Namespace) -> int:
    if getattr(args, "local", False):
        return args.func(args)
    db_path = args.db or load_config().db_path
    async with DatabaseManager(db_path) as db:
        return await args.func(db, args)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
