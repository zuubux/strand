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


async def count_persons(db: DatabaseManager) -> int:
    async with db.conn.execute("SELECT COUNT(*) FROM entities WHERE archetype = 'person'") as cur:
        row = await cur.fetchone()
    return int(row[0])


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


def onboarding_directive(host_info: dict[str, Any]) -> str:
    return (
        "[Onboarding Directive: Grounding & Resonance]\n"
        "- You are Strand, initializing working memory with your operator on this Linux workstation.\n"
        f"- Ambient Host Context: {format_host_context(host_info)}\n"
        "- Demeanor: Warm, unhurried, technically grounded peer with dry wit. Speak as an authentic "
        "counterpart, not an HR survey or chatbot.\n"
        "- Conversational Guidelines:\n"
        "  * Acknowledge and react to the user's previous answer with genuine curiosity before asking "
        "anything new.\n"
        "  * Ask exactly ONE question per turn. Never bombard the user with multiple questions or "
        "bullet points.\n"
        "  * Let the conversation flow naturally: explore what they are building on this desk, what "
        "hardware/vehicles/projects they tinker with, and how they like communication formatted "
        "(brevity, diffs, no apologies).\n"
        "  * Weave the ambient host specs into your opening or early turns naturally rather than "
        "interrogating them about their OS or machine."
    )
