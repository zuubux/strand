"""Conversational grounding: operator anchor seeding and the onboarding directive."""

from typing import Any

from .db import DatabaseManager
from .host import format_host_context

ONBOARDING_MODE = "onboarding"
ONBOARDING_HEADER = "X-Strand-Mode"
ONBOARD_TRIGGER = "(The operator just opened a fresh session. Open the conversation.)"
OPERATOR_ALIAS = "User"
OPERATES_HOST = "operates_host"


def operator_name(host_info: dict[str, Any]) -> str:
    name = str(host_info.get("user") or "").strip() or "operator"
    return name[:1].upper() + name[1:]


async def seed_operator_anchor(db: DatabaseManager, host_info: dict[str, Any]) -> tuple[int, int]:
    """Ensure `(User) --[operates_host]--> (Host)` exists; return `(user_id, host_id)`.

    An existing operator (resolved by name or the `User` alias) is never overwritten.
    """
    hostname = str(host_info.get("hostname") or "").strip() or "localhost"
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


MODE_DAY_ZERO = "day_zero"
MODE_TRIAGE = "triage"
OPENING_LINE = "Hi, I'm Strand. What should I call you?"


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
    await db.drop_entity(generic_id)  # removes any edges that could not be moved
    await db.merge_entity_alias(operator_id, OPERATOR_ALIAS)
    return True


_GUIDELINES = (
    "- Conversational Guidelines:\n"
    "  * Acknowledge and react to the user's previous answer with genuine curiosity before asking "
    "anything new.\n"
    "  * Ask exactly ONE question per turn. Never bombard the user with multiple questions or "
    "bullet points.\n"
)


def onboarding_directive(host_info: dict[str, Any], mode: str = MODE_DAY_ZERO, first_turn: bool = True) -> str:
    hostname = str(host_info.get("hostname") or "").strip() or "localhost"
    head = (
        "[Onboarding Directive: Grounding & Resonance]\n"
        "- You are Strand, initializing working memory with your operator on this Linux workstation.\n"
        f"- Ambient Host Context: {format_host_context(host_info)}\n"
        "- The host specs and the Linux account name are ambient context only. NEVER address the "
        "operator by their Linux account ($USER) name; only use a name the operator gives you.\n"
        "- Demeanor: Warm, unhurried, technically grounded peer with dry wit. Speak as an authentic "
        "counterpart, not an HR survey or chatbot.\n"
    )
    if mode == MODE_TRIAGE:
        if first_turn:
            body = (
                "- Intent Triage: an operator is already known, so do NOT restart onboarding. Ask what they "
                "wish to recalibrate, offering exactly these options:\n"
                "  1. Update their name/identity.\n"
                "  2. Recalibrate communication directives, tone, and formatting.\n"
                "  3. Re-run the full onboarding dialogue.\n"
                "  Remind them that a full database wipe/reset is handled in Settings, not in chat.\n"
            )
        else:
            body = (
                "- Intent Triage (in progress): act on the operator's choice. If they confirm a full restart, "
                "run the onboarding dialogue from the top. If they give a new name or new communication "
                "directives, acknowledge and apply them smoothly; nothing already remembered is lost. "
                "A full database wipe/reset is handled in Settings, not in chat.\n"
            )
        return head + body + _GUIDELINES.rstrip("\n")
    if first_turn:
        body = (
            f'- Turn 1 Directive: open simply with exactly: "{OPENING_LINE}" Say nothing else of substance '
            "yet, and do not use the Linux account name as a fallback.\n"
        )
    else:
        body = (
            "- Subsequent Turns Directive: once the operator gives their name, greet them by it, "
            f"acknowledge that you are running locally on {hostname}, state your mission in two sentences "
            "(sovereign working memory and privacy on this local machine), then gently ask what they "
            "typically build, fix, or troubleshoot at this desk.\n"
            "  * Over the next 2-3 turns, let the conversation naturally explore their primary projects/stack "
            "and preferred communication rules (e.g. terse diffs, direct answers, zero corporate apologies) "
            "without feeling like an HR questionnaire.\n"
        )
    return head + body + _GUIDELINES.rstrip("\n")
