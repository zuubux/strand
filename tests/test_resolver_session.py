import asyncio

import pytest

from strand.db import DatabaseManager
from strand.resolver import ContextResolver
from strand.session import SessionLedger


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_memory.db"


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def _seed(db: DatabaseManager) -> dict[str, int]:
    """Component 1: Marcus -> Atlas Migration -> Postgres, Marcus -> Dana. Component 2: Priya -> Billing Audit."""
    ids = {
        "marcus": await db.upsert_entity("Marcus", "person", "Staff engineer", {"aliases": ["Marc"]}),
        "atlas": await db.upsert_entity(
            "Atlas Migration", "project", "Move billing to Postgres", {"aliases": ["the migration"]}
        ),
        "dana": await db.upsert_entity("Dana", "person", "Engineering director"),
        "postgres": await db.upsert_entity("Postgres", "system", "Primary datastore"),
        "priya": await db.upsert_entity("Priya", "person", "Finance lead"),
        "audit": await db.upsert_entity("Billing Audit", "project", "Quarterly audit"),
    }
    await db.upsert_edge(ids["marcus"], ids["atlas"], "leads", weight=1.0)
    await db.upsert_edge(ids["marcus"], ids["dana"], "reports_to", weight=0.8)
    await db.upsert_edge(ids["atlas"], ids["postgres"], "runs_on", weight=0.9)
    await db.upsert_edge(ids["priya"], ids["audit"], "owns", weight=1.0)
    return ids


# --- SessionLedger -----------------------------------------------------------------


def test_ledger_returns_only_novel_ids_and_marks_them_seen():
    ledger = SessionLedger()
    first = ledger.filter_new("s1", [1, 2, 2, 3], [10, 11])
    assert first.entity_ids == (1, 2, 3)  # in-batch duplicates collapse, order preserved
    assert first.edge_ids == (10, 11)
    assert not first.is_empty

    second = ledger.filter_new("s1", [2, 3, 4], [11, 12])
    assert second.entity_ids == (4,)
    assert second.edge_ids == (12,)

    third = ledger.filter_new("s1", [1, 2, 3, 4], [10, 11, 12])
    assert third.is_empty
    assert third.entity_ids == () and third.edge_ids == ()


def test_ledger_entity_and_edge_id_spaces_are_independent():
    ledger = SessionLedger()
    ledger.filter_new("s1", [1], [])
    delta = ledger.filter_new("s1", [], [1])
    assert delta.edge_ids == (1,)  # edge 1 is not "seen" just because entity 1 is


def test_ledger_sessions_are_isolated_and_reset_forgets():
    ledger = SessionLedger()
    ledger.filter_new("a", [1], [1])
    assert ledger.filter_new("b", [1], [1]).entity_ids == (1,)
    assert ledger.filter_new("a", [1], [1]).is_empty

    assert ledger.reset("a") is True
    assert "a" not in ledger and "b" in ledger
    assert ledger.reset("a") is False  # already gone
    assert ledger.filter_new("a", [1], [1]).entity_ids == (1,)


def test_ledger_ttl_evicts_stale_sessions_and_activity_refreshes():
    clock = FakeClock()
    ledger = SessionLedger(ttl_seconds=60, clock=clock)
    ledger.filter_new("stale", [1], [])
    ledger.filter_new("active", [1], [])

    clock.now += 40
    ledger.filter_new("active", [2], [])  # refreshes "active" only
    clock.now += 30  # stale idle for 70s, active idle for 30s
    assert ledger.evict_expired() == 1
    assert "stale" not in ledger and "active" in ledger
    assert len(ledger) == 1

    # An expired session starts over with a clean slate.
    clock.now += 100
    assert ledger.filter_new("active", [1, 2], []).entity_ids == (1, 2)


def test_ledger_filter_new_evicts_expired_sessions_opportunistically():
    clock = FakeClock()
    ledger = SessionLedger(ttl_seconds=10, clock=clock)
    ledger.filter_new("old", [1], [])
    clock.now += 10  # exactly at the TTL boundary counts as expired
    ledger.filter_new("new", [1], [])
    assert "old" not in ledger
    assert len(ledger) == 1


@pytest.mark.parametrize("ttl", [0, -5])
def test_ledger_rejects_non_positive_ttl(ttl):
    with pytest.raises(ValueError):
        SessionLedger(ttl_seconds=ttl)


# --- ContextResolver: multi-turn delta behaviour -----------------------------------


def test_multi_turn_deltas_full_then_empty_then_only_new_entity(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await _seed(db)
            resolver = ContextResolver(db)
            turn1 = await resolver.resolve("Prep my 1-on-1 with Marcus about the migration", "s1")
            turn2 = await resolver.resolve("What did marcus say about the migration?", "s1")
            turn3 = await resolver.resolve("Also loop in Priya", "s1")
            turn4 = await resolver.resolve("Priya again", "s1")
            return turn1, turn2, turn3, turn4

    turn1, turn2, turn3, turn4 = run(scenario())

    # Turn 1: full delta for the Marcus/Atlas neighbourhood (2 hops reaches Postgres).
    assert turn1.startswith("## Strand Memory")
    for line in (
        "- Marcus (person): Staff engineer",
        "- Atlas Migration (project): Move billing to Postgres",
        "- Dana (person): Engineering director",
        "- Postgres (system): Primary datastore",
        "- Marcus -leads-> Atlas Migration",
        "- Marcus -reports_to-> Dana",
        "- Atlas Migration -runs_on-> Postgres",
    ):
        assert line in turn1
    assert "Priya" not in turn1

    # Turn 2: everything touched is already in working memory.
    assert turn2 == ""

    # Turn 3: only the new component (Priya + Billing Audit + their edge).
    assert "- Priya (person): Finance lead" in turn3
    assert "- Billing Audit (project): Quarterly audit" in turn3
    assert "- Priya -owns-> Billing Audit" in turn3
    for already_seen in ("Marcus", "Atlas", "Dana", "Postgres", "leads", "reports_to", "runs_on"):
        assert already_seen not in turn3

    # Turn 4: Priya is now known too.
    assert turn4 == ""


# --- ContextResolver: prompt entity extraction -------------------------------------


def test_match_entities_is_case_insensitive_and_supports_aliases(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await _seed(db)
            resolver = ContextResolver(db)
            return (
                await resolver.match_entities("ping MARCUS"),
                await resolver.match_entities("ask marc about it"),
                await resolver.match_entities("status of THE MIGRATION?"),
                await resolver.match_entities("Priya then Marcus"),
                await resolver.match_entities("nothing relevant"),
                await resolver.match_entities("   "),
            )

    by_name, by_alias, by_alias_2, ordered, none, blank = run(scenario())
    assert [e.name for e in by_name] == ["Marcus"]
    assert (by_name[0].archetype, by_name[0].summary) == ("person", "Staff engineer")
    assert [e.name for e in by_alias] == ["Marcus"]
    assert [e.name for e in by_alias_2] == ["Atlas Migration"]
    assert [e.name for e in ordered] == ["Priya", "Marcus"]  # first mention wins, not insertion order
    assert none == [] and blank == []


def test_match_entities_requires_whole_words_but_tolerates_punctuation(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await db.upsert_entity("Al", "person", "")
            await db.upsert_entity("C++", "concept", "")
            resolver = ContextResolver(db)
            queries = ["Alice and Albert", "hey al!", "Review c++, please", "abc++ is not it", "(Al)"]
            return [[e.name for e in await resolver.match_entities(q)] for q in queries]

    assert run(scenario()) == [[], ["Al"], ["C++"], [], ["Al"]]


def test_match_entities_tolerates_malformed_alias_metadata(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await db.upsert_entity("Bad Count", "concept", "", {"aliases": 5})
            await db.upsert_entity("Mixed", "concept", "", {"aliases": [7, "zed"]})
            await db.upsert_entity("Single", "concept", "", {"aliases": "solo"})
            broken = await db.upsert_entity("Broken", "concept", "")
            await db.conn.execute("UPDATE entities SET metadata = ? WHERE id = ?", ("{aliases oops", broken))
            await db.conn.commit()
            resolver = ContextResolver(db)
            queries = ["bad count", "zed", "solo", "broken", "7", "5"]
            return [[e.name for e in await resolver.match_entities(q, allow_private=True)] for q in queries]

    assert run(scenario()) == [["Bad Count"], ["Mixed"], ["Single"], ["Broken"], [], []]


# --- ContextResolver: graph walk, formatting, limits -------------------------------


def _turns(db_path, queries, **resolver_kwargs):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await _seed(db)
            resolver = ContextResolver(db, **resolver_kwargs)
            return [await resolver.resolve(q, "s1") for q in queries]

    return run(scenario())


def test_graph_walk_respects_hops_and_dedups_across_roots(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await _seed(db)
            one_hop = await ContextResolver(db, max_hops=1).gather("Marcus")
            two_hop = await ContextResolver(db, max_hops=2).gather("Marcus")
            both = await ContextResolver(db).gather("Marcus and Atlas Migration")
            return one_hop, two_hop, both

    one_hop, two_hop, both = run(scenario())
    assert {e.name for e in one_hop[0]} == {"Marcus", "Atlas Migration", "Dana"}
    assert {e.relation for e in one_hop[1]} == {"leads", "reports_to"}
    assert {e.name for e in two_hop[0]} == {"Marcus", "Atlas Migration", "Dana", "Postgres"}
    assert {e.relation for e in two_hop[1]} == {"leads", "reports_to", "runs_on"}
    entities, edges = both
    assert len({e.id for e in entities}) == len(entities) == 4
    assert len({e.id for e in edges}) == len(edges) == 3


def test_resolve_renders_compact_markdown(db_path):
    (turn,) = _turns(db_path, ["Priya"])
    assert turn == "\n".join(
        [
            "## Strand Memory",
            "Entities:",
            "- Priya (person): Finance lead",
            "- Billing Audit (project): Quarterly audit",
            "Relations:",
            "- Priya -owns-> Billing Audit",
        ]
    )


def test_resolve_emits_only_new_edge_between_already_seen_entities(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            ids = await _seed(db)
            resolver = ContextResolver(db)
            first = await resolver.resolve("Marcus", "s1")
            await db.upsert_edge(ids["dana"], ids["postgres"], "approved", weight=0.5)
            return first, await resolver.resolve("Marcus", "s1"), await resolver.resolve("Marcus", "s1")

    first, delta, again = run(scenario())
    assert first
    assert delta == "## Strand Memory\nRelations:\n- Dana -approved-> Postgres"
    assert again == ""


# --- ContextResolver: empty paths, sessions, validation ----------------------------


def test_resolve_returns_empty_string_without_touching_ledger_when_nothing_matches(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            resolver = ContextResolver(db)
            on_empty_db = await resolver.resolve("Prep my 1-on-1 with Marcus", "s1")
            await _seed(db)
            no_match = await resolver.resolve("What is the weather today?", "s1")
            blank = await resolver.resolve("", "s1")
            return on_empty_db, no_match, blank, len(resolver.ledger)

    on_empty_db, no_match, blank, sessions = run(scenario())
    assert (on_empty_db, no_match, blank) == ("", "", "")
    assert sessions == 0  # no overhead: no session state was even created


def test_resolve_entity_without_edges_renders_entities_only(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await db.upsert_entity("Loner", "concept", "Line one\n  line   two\t!")
            await db.upsert_entity("Blank", "concept", "")
            resolver = ContextResolver(db)
            return await resolver.resolve("Loner and Blank", "s1"), await resolver.resolve("Loner", "s1")

    first, second = run(scenario())
    assert first == "\n".join(
        ["## Strand Memory", "Entities:", "- Loner (concept): Line one line two !", "- Blank (concept)"]
    )
    assert "Relations:" not in first
    assert second == ""


def test_resolve_sessions_are_isolated_and_reset_replays_context(db_path):
    async def scenario():
        async with DatabaseManager(db_path) as db:
            await _seed(db)
            resolver = ContextResolver(db)
            a1 = await resolver.resolve("Marcus", "a")
            a2 = await resolver.resolve("Marcus", "a")
            b1 = await resolver.resolve("Marcus", "b")
            resolver.ledger.reset("a")
            a3 = await resolver.resolve("Marcus", "a")
            return a1, a2, b1, a3

    a1, a2, b1, a3 = run(scenario())
    assert a1 and a2 == ""
    assert b1 == a1  # another conversation has seen nothing yet
    assert a3 == a1  # reset replays the full context


def test_resolve_replays_context_after_ttl_expiry(db_path):
    async def scenario():
        clock = FakeClock()
        async with DatabaseManager(db_path) as db:
            await _seed(db)
            resolver = ContextResolver(db, SessionLedger(ttl_seconds=60, clock=clock))
            first = await resolver.resolve("Marcus", "s1")
            clock.now += 59
            still_known = await resolver.resolve("Marcus", "s1")
            clock.now += 60
            expired = await resolver.resolve("Marcus", "s1")
            return first, still_known, expired

    first, still_known, expired = run(scenario())
    assert first and still_known == ""
    assert expired == first


def test_max_entities_caps_roots_in_order_of_mention(db_path):
    (turn,) = _turns(db_path, ["Priya, Marcus, Dana"], max_entities=1, max_hops=1)
    assert "- Priya (person): Finance lead" in turn
    assert "Marcus" not in turn and "Dana" not in turn


def test_edge_limit_bounds_the_subgraph(db_path):
    (turn,) = _turns(db_path, ["Marcus"], edge_limit=1, max_hops=1)
    # Strongest edge only (leads, weight 1.0); the weaker reports_to edge is cut.
    assert "- Marcus -leads-> Atlas Migration" in turn
    assert "reports_to" not in turn and "Dana" not in turn


@pytest.mark.parametrize(
    "kwargs",
    [{"max_hops": 0}, {"max_hops": 3}, {"edge_limit": 0}, {"max_entities": 0}],
)
def test_resolver_rejects_invalid_limits(db_path, kwargs):
    with pytest.raises(ValueError):
        ContextResolver(DatabaseManager(db_path), **kwargs)

