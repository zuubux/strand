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


async def is_onboarding_completed(db: DatabaseManager) -> bool:
    """Permanent graduation: an explicit operator exists AND the telemetry choice is resolved."""
    if await count_operators(db) <= 0:
        return False
    return await telemetry_preference(db) is not None


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


TASK_YIELD_RULE = (
    "TASK-YIELD CONSTRAINT: When the operator provides an imperative command, tool instruction, "
    "code/file edit, or debugging task, execute or report the result plainly and STOP. Do NOT append "
    "onboarding questions, follow-up banter, or conversational filler."
)
QUESTION_BUDGET_RULE = (
    "Default question budget = 0. Do not ask conversational follow-up questions to keep dialogue "
    "moving. Ask a question only if strictly blocked by ambiguity or missing parameters."
)

_GUIDELINES = (
    "- Conversational Guidelines:\n"
    "  * Acknowledge and react to the user's previous answer with genuine curiosity before asking "
    "anything new.\n"
    "  * Ask exactly ONE question per turn. Never bombard the user with multiple questions or "
    "bullet points.\n"
    f"  * {TASK_YIELD_RULE}\n"
    f"  * {QUESTION_BUDGET_RULE}\n"
)


TELEMETRY_ENABLED = "Host Telemetry Enabled"
TELEMETRY_ON_DEMAND = "Host Telemetry On-Demand"
TELEMETRY_RELATION = "prefers"
TELEMETRY_GATE_QUESTION = (
    "I keep things private by default—I don't review your local computer's details unless you want "
    "me to. Should I check out your machine specs, or keep that strictly on-demand?"
)
TELEMETRY_DECLINED_ACK = "Understood, keeping machine specs strictly local and on-demand"
TELEMETRY_GATE_TURN = 4  # first operator turn on which the telemetry check-in may be raised


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


def onboarding_directive(
    host_info: dict[str, Any],
    mode: str = MODE_DAY_ZERO,
    first_turn: bool = True,
    turn: int | None = None,
    telemetry_decision: bool | None = None,
) -> str:
    """Build the directive. `turn` (1-based operator turn count) overrides `first_turn` when given."""
    if turn is not None:
        first_turn = turn <= 1
    hostname = str(host_info.get("hostname") or "").strip() or "workstation"
    head = (
        "[Onboarding Directive: Grounding & Resonance]\n"
        "- You are Strand, initializing working memory with your operator on this Linux workstation.\n"
        f"- Ambient Host Context: {format_host_context(host_info)}\n"
        "- The host specs and the Linux account name are ambient context only. Do not reflexively "
        "default to the local account username ($USER) if the operator is willing to provide a preferred "
        "name. However, if the operator is coy, deflects, or expresses indifference ('whatever', 'I don't "
        "care', 'guess'), you may conversationally suggest or offer a cleaned-up name derived from their "
        "username (e.g., offering 'Alice' for account 'alice' or 'asmith') and ask if that works, ensuring "
        "the conversation moves forward without stalling.\n"
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
            "yet. Ask for their preferred name rather than assuming the Linux account name; the account "
            "name is only a conversational fallback if they deflect.\n"
        )
    else:
        step = turn if turn is not None else 2
        body = (
            "- Turn 2/3 Directive: once the operator gives their name, greet them by it, quietly note "
            f"\"Looks like I'm running locally on {hostname}\". In 1-2 plain sentences, explain what you do: "
            "you keep memory and context locally on this computer so the operator doesn't have to keep "
            "re-explaining things to models, and you strip out sensitive info before anything goes out. "
            "Then gently ask: "
            "\"What do you usually find yourself working on, creating, or exploring at this computer?\"\n"
            "  * Over the next turns, let the conversation naturally explore their primary projects/stack, "
            "tools and preferred communication rules (e.g. terse diffs, direct answers, zero corporate "
            "apologies) without feeling like an HR questionnaire.\n"
        )
        if step >= TELEMETRY_GATE_TURN:
            if telemetry_decision is None:
                body += (
                    "- Turn 4/5 Telemetry Gate: while exploring their workbench, stacks and tools, casually "
                    "check in on hardware awareness, in your own warm words, with exactly this question "
                    f'(still only ONE question this turn): "{TELEMETRY_GATE_QUESTION}"\n'
                    "  * If they agree (e.g. 'sure', 'yes', 'go ahead', 'Tune into the iron'), briefly "
                    "acknowledge the machine specs from the Ambient Host Context (hostname, RAM, cores) in one "
                    "plain sentence, say machine specs are now active, then yield back to the operator: no "
                    "corporate fluff and no follow-up question.\n"
                    f'  * If they decline (e.g. "Keep it on-demand", "Leave it local"), reply smoothly: '
                    f'"{TELEMETRY_DECLINED_ACK}".\n'
                    "  * Do not raise the hardware question again once they have answered.\n"
                )
            elif telemetry_decision:
                body += (
                    "- Telemetry Gate (resolved): the operator opted in; machine specs are now active. "
                    "Do not ask about hardware again.\n"
                )
            else:
                body += (
                    f"- Telemetry Gate (resolved): the operator declined. {TELEMETRY_DECLINED_ACK}. "
                    "Do not ask about hardware again.\n"
                )
    return head + body + _GUIDELINES.rstrip("\n")
