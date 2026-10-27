"""Stop-word filter and guardrails for entity names (keeps discourse noise out of the graph)."""

import re

DISCOURSE_MARKERS = frozenset(
    """
    however therefore moreover furthermore nevertheless nonetheless meanwhile otherwise
    additionally also instead thus hence consequently besides anyway anyhow still yet
    indeed actually basically essentially overall finally firstly secondly thirdly first
    second third next then later earlier now today tomorrow yesterday again further
    similarly likewise accordingly alternatively specifically generally typically usually
    ultimately honestly obviously clearly perhaps maybe sometimes often always never
    """.split()
)

CONVERSATIONAL_STARTERS = frozenset(
    """
    please certainly sure absolutely definitely of course okay ok yes no yeah yep nope
    right alright great good fine perfect sorry apologies unfortunately fortunately
    here there this that these those what when where which who whom whose why how
    let lets tell give show make write help can could would should will shall may might
    must do does did is are was were be been being have has had if but and or so because
    although though while since unless until whether for from with without about into
    the a an it its i me my we us our you your he she they them their his her
    note remember consider imagine suppose assume
    got whenever wherever whatever whichever whoever classic understood noted certainly
    sounds looks seems keep mind as not just prep
    """.split()
)

GREETINGS = frozenset(
    """
    hi hello hey hiya greetings goodbye bye thanks thank cheers welcome regards
    morning afternoon evening goodnight dear
    """.split()
)

DAYS = frozenset(
    """
    monday tuesday wednesday thursday friday saturday sunday
    mon tue tues wed thu thur thurs fri sat sun
    """.split()
)

MONTHS = frozenset(
    """
    january february march april may june july august september october november december
    jan feb mar apr jun jul aug sep sept oct nov dec
    """.split()
)

STOP_WORDS = frozenset(
    DISCOURSE_MARKERS | CONVERSATIONAL_STARTERS | GREETINGS | DAYS | MONTHS
)

# Short (<3 char) single-word names that are legitimate domain entities.
SHORT_NAME_WHITELIST = frozenset(
    {"ai", "ml", "db", "os", "ui", "ux", "qa", "ci", "cd", "go", "c", "r", "js", "ts", "vm", "gpu", "cpu"}
)

_WORD_RE = re.compile(r"\w[\w\-.#+]*")


def is_stop_word(word: str) -> bool:
    """True if `word` (case-insensitive, surrounding punctuation ignored) is a stop-word."""
    return word.strip(" \t\n.,;:!?\"'()[]{}").casefold() in STOP_WORDS


def is_valid_entity_name(name: str, archetype: str = "concept") -> bool:
    """Whether `name` may be stored/matched as an entity of `archetype`.

    - Empty or punctuation-only names are rejected.
    - Single stop-words are rejected for the "concept" archetype (case-insensitive).
    - Single-word names shorter than 3 characters are rejected unless whitelisted.
    - Multi-word names (e.g. "BMW E34", "Database Migration") are always accepted.
    """
    if not isinstance(name, str):
        return False
    words = name.split()
    if not words or not _WORD_RE.search(name):
        return False
    if len(words) > 1:
        return True
    word = words[0]
    folded = word.casefold()
    if len(word) < 3 and folded not in SHORT_NAME_WHITELIST:
        return False
    if archetype == "concept" and is_stop_word(word):
        return False
    return True

# Function words that carry no identity when a multi-word name is split into tokens.
FUNCTION_WORDS = frozenset(
    {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at", "by", "with", "from", "my", "is"}
)


def name_tokens(name: str, min_length: int = 2) -> list[str]:
    """Significant tokens of a multi-word `name` (original case, de-duplicated, in order).

    Drops tokens shorter than `min_length`, stop-words and function words. Single-word
    names yield no tokens: they are already matched by name.
    """
    words = name.split()
    if len(words) < 2:
        return []
    tokens: list[str] = []
    for word in words:
        token = word.strip(" \t\n.,;:!?\"'()[]{}")
        if (
            len(token) >= min_length
            and not is_stop_word(token)
            and token.casefold() not in FUNCTION_WORDS
            and token.casefold() not in {t.casefold() for t in tokens}
        ):
            tokens.append(token)
    return tokens

# Assistant headers, instructional markers and generic nouns that never name a graph entity
# on their own ("Important Tip", "Capacity", "Weight", "Note", "Feel").
FILLER_WORDS = frozenset(
    """
    important tip tips note notes feel look looks capacity capacities weight weights
    example examples summary overview reference quick general common popular option options
    step steps warning caution reminder conclusion recommendation recommendations
    answer details detail info information feature features benefit benefits tl dr
    pro pros con cons key takeaway takeaways point points
    specification specifications spec specs recommended tips instructions instruction
    guide checklist procedure procedures requirement requirements section torque
    """.split()
)


def is_filler_name(name: str) -> bool:
    """True when every word of `name` is a stop-word or assistant filler/header word."""
    words = [w.strip(" \t\n.,;:!?\"'()[]{}").casefold() for w in name.split()]
    words = [w for w in words if w]
    return not words or all(w in FILLER_WORDS or w in STOP_WORDS for w in words)
