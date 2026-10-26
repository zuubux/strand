"""Operator CLI: inspect and administer the Cortext memory database without raw SQL.

Talks to SQLite directly (WAL mode), so the daemon does not need to be running.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

from .config import Config
from .db import DatabaseManager


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
    async with db._transaction() as conn:
        for entity_id in ids:
            cur = await conn.execute(
                "DELETE FROM edges WHERE source_id = ? OR target_id = ?", (entity_id, entity_id)
            )
            edges += cur.rowcount
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
    async with db._transaction() as conn:
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


def _confidence(value: str) -> float:
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0.0 and 1.0")
    return number


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cortext", description="Inspect and administer Cortext memory.")
    parser.add_argument("--db", help="database path (default: $CORTEXT_DB_PATH or ~/.config/cortext/memory.db)")
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
    return parser


async def _run(args: argparse.Namespace) -> int:
    db_path = args.db or Config.from_env().db_path
    async with DatabaseManager(db_path) as db:
        return await args.func(db, args)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
