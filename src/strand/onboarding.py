"""Operator anchor seeding and operator/telemetry query helpers (first-run setup is the wizard)."""

from typing import Any

from .db import DatabaseManager

OPERATOR_ALIAS = "User"
OPERATES_HOST = "operates_host"


def operator_name(host_info: dict[str, Any]) -> str:
    name = str(host_info.get("user") or "").strip() or "Operator"
    return name[:1].upper() + name[1:]


async def seed_operator_anchor(db: DatabaseManager, host_info: dict[str, Any]) -> tuple[int, int]:
    """Ensure `(User) --[operates_host]--> (Host)` exists; return `(user_id, host_id)`.

    An existing operator (resolved by name or the `User` alias) is never overwritten.
    """
    hostname = str(host_info.get("hostname") or "").strip() or "workstation"
    existing = await db.find_entity(operator_name(host_info)) or await db.find_entity(OPERATOR_ALIAS)
    if existing is not None:
        user_id = existing[0]
        async with db.conn.execute(
            "SELECT target_id FROM edges WHERE source_id = ? AND relation = ? ORDER BY id LIMIT 1",
            (user_id, OPERATES_HOST),
        ) as cur:
            row = await cur.fetchone()
        if row is not None:
            return user_id, int(row[0])
    else:
        user_id = await db.upsert_entity(
            operator_name(host_info),
            "person",
            "Primary operator of this workstation",
            metadata={"aliases": [OPERATOR_ALIAS]},
            origin="user_explicit",
            confidence=1.0,
        )
    found = await db.find_entity(hostname)
    if found is not None:
        host_id = found[0]
    else:
        host_id = await db.upsert_entity(
            hostname,
            "system",
            f"Primary workstation running {host_info.get('os') or 'Linux'}",
            origin="user_explicit",
            confidence=1.0,
        )
    await db.upsert_edge(
        user_id, host_id, OPERATES_HOST, weight=1.0, origin="user_explicit", confidence=1.0
    )
    return user_id, host_id


async def count_operators(db: DatabaseManager) -> int:
    """Number of operator anchors: `person` entities the operator explicitly asserted."""
    async with db.conn.execute(
        "SELECT COUNT(*) FROM entities WHERE archetype = 'person' AND origin = 'user_explicit'"
    ) as cur:
        row = await cur.fetchone()
    return int(row[0])


async def reanchor_generic_user(db: DatabaseManager) -> bool:
    """Fold a generic `User` node into the named operator, keeping every edge.

    The named operator is the most recently updated explicit `person` other than `User`.
    Edges are re-pointed (duplicates already on the operator are dropped), `User` becomes an
    alias and the generic node is removed. Returns False when there is nothing to do.
    """
    async with db.conn.execute("SELECT id FROM entities WHERE name = ?", (OPERATOR_ALIAS,)) as cur:
        generic = await cur.fetchone()
    if generic is None:
        return False
    async with db.conn.execute(
        "SELECT id, name FROM entities WHERE archetype = 'person' AND origin = 'user_explicit' "
        "AND name != ? ORDER BY updated_at DESC, id DESC LIMIT 1",
        (OPERATOR_ALIAS,),
    ) as cur:
        operator = await cur.fetchone()
    if operator is None:
        return False
    generic_id, (operator_id, operator_label) = int(generic[0]), (int(operator[0]), operator[1])
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE OR IGNORE edges SET source_id = ? WHERE source_id = ? AND target_id != ?",
            (operator_id, generic_id, operator_id),
        )
        await conn.execute(
            "UPDATE OR IGNORE edges SET target_id = ? WHERE target_id = ? AND source_id != ?",
            (operator_id, generic_id, operator_id),
        )
    await db.drop_entity(generic_id, scrub_episodes=False)  # merge, not a forget; removes any edges that could not be moved
    await db.merge_entity_alias(operator_id, OPERATOR_ALIAS)
    return True


TELEMETRY_ENABLED = "Host Telemetry Enabled"
TELEMETRY_ON_DEMAND = "Host Telemetry On-Demand"
TELEMETRY_RELATION = "prefers"


async def telemetry_preference(db: DatabaseManager) -> bool | None:
    """Operator's latest active hardware-telemetry stance recorded by Model B (SEC-PRIV-002).

    Reads `(<Operator>) --[prefers]--> (Host Telemetry Enabled | Host Telemetry On-Demand)`:
    True for Enabled, False for On-Demand, None when neither edge is active. No schema change.

    Provenance guard (SEC-PRIV-008): an *Enabled* edge counts only when the edge is
    `user_explicit` with confidence >= 1.0 and its source is an authenticated operator anchor
    (a `person` entity that is itself `user_explicit` at confidence >= 1.0). A `model_inferred`
    edge can never open hardware telemetry; On-Demand (the closed, fail-safe stance) is
    honoured at any provenance.
    """
    async with db.conn.execute(
        "SELECT t.name, e.origin, e.confidence, s.origin, s.confidence "
        "FROM edges e JOIN entities t ON t.id = e.target_id "
        "JOIN entities s ON s.id = e.source_id "
        "WHERE e.relation = ? AND e.weight > 0 AND s.archetype = 'person' "
        "AND lower(t.name) IN (?, ?) ORDER BY e.updated_at DESC, e.id DESC",
        (TELEMETRY_RELATION, TELEMETRY_ENABLED.lower(), TELEMETRY_ON_DEMAND.lower()),
    ) as cur:
        rows = await cur.fetchall()
    for name, edge_origin, edge_conf, src_origin, src_conf in rows:
        if str(name).lower() != TELEMETRY_ENABLED.lower():
            return False
        if (
            edge_origin == "user_explicit"
            and float(edge_conf) >= 1.0
            and src_origin == "user_explicit"
            and float(src_conf) >= 1.0
        ):
            return True
    return None
