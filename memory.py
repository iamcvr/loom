"""The memory pipeline.

Four layers, one model call.

  long   durable facts, retrieved by embedding similarity
  temp   recent salience, heat-sorted and decaying
  rels   how each character sees the player
  goals  extracted, then confirmed by the player before going active

Everything mechanical — decay, similarity, heat ordering, keyword matching — is
plain arithmetic here. Exactly one model call happens per turn, in digest(), and
it returns a single structured delta. SillyTavern's failure was four extensions
each firing their own call with no shared ordering or budget; that is the thing
this module exists to not do.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

import brain
import config
import lorebook
import store

# ---------------------------------------------------------------- heat


def heat(mem: dict, turn: int) -> float:
    """salience * decay(age) * (1 + w*log1p(reinforcements))"""
    age = max(0, turn - int(mem.get("seen_turn") or 0))
    half = max(1e-6, config.MEM_DECAY_HALFLIFE_TURNS)
    decay = 0.5 ** (age / half)
    boost = 1.0 + config.MEM_REINFORCE_WEIGHT * math.log1p(int(mem.get("reinforced") or 0))
    return float(mem.get("salience") or 0.0) * decay * boost


def hot_temp(session_id: int, turn: int, limit: Optional[int] = None) -> list[dict]:
    rows = store.memories(session_id, "temp")
    scored = [dict(r, heat=heat(r, turn)) for r in rows]
    scored.sort(key=lambda m: m["heat"], reverse=True)
    return scored[: limit] if limit else scored


def decay_and_prune(session_id: int, turn: int) -> dict[str, int]:
    """Drop cold temp memories; promote hot ones to long-term."""
    rows = store.memories(session_id, "temp")
    dropped: list[int] = []
    promoted: list[int] = []

    scored = sorted(
        (dict(r, heat=heat(r, turn)) for r in rows), key=lambda m: m["heat"], reverse=True
    )
    for i, m in enumerate(scored):
        earned = int(m.get("reinforced") or 0) >= config.MEM_PROMOTE_MIN_REINFORCED
        if earned and m["heat"] >= config.MEM_PROMOTE_THRESHOLD:
            store.promote_memory(m["id"])
            promoted.append(m["id"])
        elif m["heat"] < config.MEM_TEMP_FLOOR or i >= config.MEM_TEMP_MAX_ROWS:
            dropped.append(m["id"])

    store.drop_memories(dropped)

    # Long-term storage needs a ceiling too. Promotion has no natural brake, and an
    # unbounded store is self-defeating: retrieval pulls a fixed K per turn, so every
    # extra row is one more competitor for the same eight slots. Durable world facts
    # belong in the lorebook now, where a keyword fires them deterministically.
    culled = store.prune_long(session_id, config.MEM_LONG_MAX_ROWS)

    return {"dropped": len(dropped) + len(culled), "promoted": len(promoted),
            "long_culled": len(culled)}


# ---------------------------------------------------------------- retrieval


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def retrieve_long(session_id: int, query: str, k: Optional[int] = None) -> list[dict]:
    """Long-term memories most similar to the query. Falls back to recency when
    embeddings are unavailable, so a dead Ollama degrades instead of breaking."""
    k = k or config.MEM_RETRIEVE_K
    rows = store.memories(session_id, "long")
    if not rows:
        return []

    qv = brain.embed([query])[0] if query.strip() else []
    if not qv:
        return rows[:k]

    scored: list[dict] = []
    for r in rows:
        v = store.unpack_embedding(r["embedding"])
        sim = _cosine(qv, v)
        if sim >= config.MEM_RETRIEVE_MIN_SIMILARITY:
            scored.append(dict(r, similarity=sim))
    scored.sort(key=lambda m: m["similarity"], reverse=True)
    return scored[:k]


def retrieval_query(session_id: int) -> str:
    turns = config.MEM_QUERY_TURNS * 2
    msgs = store.messages(session_id, limit=turns)
    return "\n".join(m["content"] for m in msgs)[-2000:]


# ---------------------------------------------------------------- keywords


def match_keywords(story: dict, session_id: int) -> list[dict]:
    """Notes triggered by the recent transcript, always-on ones first.

    Ordering matters: the arbiter takes a prefix, so whatever is first survives a
    squeeze. Always-on notes lead, then notes whose keyword appeared most recently.

    The story's authored notes and the session's lorebook entries are ranked in one
    pool. They are the same kind of thing — a reference note fired by a keyword —
    and separating them would mean a note written up front outranking one the story
    actually earned, on nothing more than where it is stored.
    """
    msgs = store.messages(session_id, limit=config.KEYWORD_SCAN_TURNS * 2)
    haystack = " ".join(m["content"] for m in msgs).lower()

    always: list[dict] = []
    hits: list[tuple[int, dict]] = []
    for note in list(story.get("keywords", [])) + lorebook.notes(session_id):
        if note.get("always"):
            always.append(note)
            continue
        best = -1
        for kw in note["keywords"]:
            pos = haystack.rfind(kw)
            if pos > best:
                best = pos
        if best >= 0:
            hits.append((best, note))

    hits.sort(key=lambda t: t[0], reverse=True)  # most recently mentioned first
    ordered = always + [n for _, n in hits]
    return ordered[: config.KEYWORD_MAX_NOTES]


# ---------------------------------------------------------------- the nudge


_NUDGE_SOFT = """## Still outstanding

{goal}

Nobody has mentioned this for {quiet} turns. Somewhere in this scene, let it
surface — once, lightly, and from a character rather than from the narration. A
question, a glance at the hour, an offhand "weren't you going to". One line.

Then drop it and let the scene go wherever the player takes it. Do not move the
story toward it, do not have events conspire to make it happen, and do not require
the player to answer for it. If this moment genuinely has no room for it — mid-
fight, mid-confession — ignore this note entirely; it will come around again."""

_NUDGE_FIRM = """## Still outstanding

{goal}

This has been hanging for {quiet} turns and the characters would have noticed. The
reminder can be plainer now: someone may be mildly put out that it has not happened
yet, or the world may make it slightly inconvenient to keep putting off — a shop
closing, a person waiting, light going.

Still no forcing. The player chooses when to act on it, and refusing is allowed. Do
not resolve it on their behalf and do not stage a coincidence that resolves it."""


_ACTION_NUDGE = """## Time for {kind}

It has been {quiet} turns since anything physical happened, and this story is
supposed to be dangerous. Bring {kind} into play — in this scene, or in the one the
player's next move opens.

It does not need a reason. This is a city with Quirks in it: a bag-snatch that goes
badly, a training bout, a collapsing scaffold, someone starting something in a bar,
an assessment, a callout the cohort is allowed to attend. It has NOTHING to do with
anything already running — do not tie it to the current plot, the last villain, or
anybody's backstory. It is just Tuesday.

Keep it close and short: contact within a paragraph or two, over inside a few turns,
somebody hurt or something broken. Then let the story go back to whatever it was
doing."""

# The firm form is delivered at DEPTH, not in the system prompt — see
# assemble.build. Measured: with the identical text sitting in the system block
# the model produced five consecutive peaceful turns past the cadence. This is
# the same finding the director's note produced, for the same reason: a few
# thousand characters of vivid recent scene outrank a paragraph of system text.
#
# The soft form above shipped with an escape clause — "if the player is
# mid-conversation, let them finish first". Measured against real turns, that
# clause fired every single time: this story's baseline register IS conversation,
# so "mid-conversation" is always true and the nudge was always deferred. Three
# peaceful turns past the cadence produced three peaceful turns.
#
# So the escape is gone from the soft form, and there is now a hard form that
# closes the door explicitly. An instruction the model can always decline is not
# a cadence, it is a suggestion.
_ACTION_NUDGE_FIRM = """## {kind}, this turn — not the next one

{quiet} turns without anything physical. That is too long for this story and it is
now overdue.

Something violent starts in THIS reply. Not foreshadowed, not approaching, not
somebody mentioning that it is dangerous around here — it happens, on the page, and
the player is in it before the reply ends.

Interrupt whatever is going on to do it. If they are mid-meal, mid-argument,
mid-flirtation, that conversation gets cut off by it, and that is fine — being
interrupted is what living in this city is like. Do not ask permission, do not
build up to it, and do not resolve it in the same reply.

It is unrelated to everything currently running. Do not tie it to the plot, to the
last villain, or to anyone's backstory. It is just Tuesday."""


def action_quiet(session_id: int, turn: int) -> int:
    """Turns since anything physical happened."""
    last = store.get_meta(f"action:{session_id}", None)
    if last is None:
        # Nothing recorded yet: measure from the start of the session rather than
        # from turn zero, so a fresh story does not open owing a fight.
        return turn
    return turn - int(last)


def mark_action(session_id: int, turn: int) -> None:
    store.set_meta(f"action:{session_id}", turn)


def action_level(story: dict, session_id: int, turn: int) -> str:
    """"" | "soft" | "firm" — how overdue a fight is."""
    every = int((story.get("pacing") or {}).get("action_every") or 0)
    if not every:
        return ""
    quiet = action_quiet(session_id, turn)
    if quiet < every:
        return ""
    return "firm" if quiet >= every + max(2, every // 2) else "soft"


def action_nudge(story: dict, session_id: int, turn: int) -> str:
    """Ask for a fight when the story has gone quiet too long, or "" if not yet.

    This is a counter in code, not an instruction in prose, and it has to be. The
    model is handed the current chapter's transcript with no turn numbers on it —
    seven messages, in the measured case — so it cannot tell whether the last
    fight was two turns ago or thirty. Telling it "combat should be frequent"
    delegates a measurement it is structurally unable to make, and what actually
    happens is that combat appears when the player asks for it and never
    otherwise.

    Frequency is therefore the harness's job. What the story file supplies is the
    interval; what the model supplies is a fight worth reading.
    """
    every = int((story.get("pacing") or {}).get("action_every") or 0)
    if not every:
        return ""
    kind = (story.get("pacing") or {}).get("action_kind") or "a fight"
    quiet = action_quiet(session_id, turn)
    level = action_level(story, session_id, turn)
    if not level:
        return ""
    template = _ACTION_NUDGE_FIRM if level == "firm" else _ACTION_NUDGE
    return template.format(kind=kind, quiet=quiet)


def goal_quiet(goal: dict, turn: int) -> int:
    """Turns since the story last engaged with this objective.

    max(), not `touched or created`: touched_turn is 0 both for a goal nothing has
    engaged with yet and for every row that predates the column, and a falsy-zero
    fallback conflates the two. add_goal stamps touched_turn on insert, so a live
    goal never reads 0, and a migrated row correctly measures from its creation.
    """
    return turn - max(int(goal.get("touched_turn") or 0), int(goal.get("created_turn") or 0))


def goal_nudge(session_id: int, turn: int) -> str:
    """A reminder about the open objective, or "" if it is not time for one.

    Two failure modes sit either side of this. Say nothing and the objective is
    quietly forgotten: it lives in one line of a 48,000-character prompt and the
    transcript is louder. Say it every turn and the model treats it as the scene's
    purpose, which is how a story about buying a coat becomes a story about the
    logistics of buying a coat.

    So: silence while the story is engaged with it, then one soft prompt after a
    few quiet turns, then a firmer one much later. The counter resets whenever the
    turn actually touched the goal, which the digest reports.
    """
    if not (config.GOAL_NUDGE_ENABLED and config.GOALS_MAX_ACTIVE):
        return ""
    goal = store.active_goal(session_id)
    if not goal:
        return ""
    quiet = goal_quiet(goal, turn)
    if quiet < config.GOAL_NUDGE_AFTER:
        return ""
    template = _NUDGE_FIRM if quiet >= config.GOAL_NUDGE_ESCALATE else _NUDGE_SOFT
    return template.format(goal=goal["text"].strip(), quiet=quiet)


# ---------------------------------------------------------------- the delta

DELTA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "new_memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "kind": {"type": "string", "enum": ["long", "temp"]},
                    "salience": {"type": "integer"},
                },
                "required": ["text", "kind", "salience"],
                "additionalProperties": False,
            },
        },
        "reinforced": {"type": "array", "items": {"type": "integer"}},
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "summary": {"type": "string"}},
                "required": ["name", "summary"],
                "additionalProperties": False,
            },
        },
        "goals_add": {"type": "array", "items": {"type": "string"}},
        "goals_complete": {"type": "array", "items": {"type": "integer"}},
        "goal_touched": {"type": "boolean"},
        "action_beat": {"type": "boolean"},
        "stats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "delta": {"type": "number"}},
                "required": ["key", "delta"],
                "additionalProperties": False,
            },
        },
        "new_characters": {"type": "array", "items": {"type": "string"}},
        "scene_changed": {"type": "boolean"},
        "scene_prompt": {"type": "string"},
    },
    "required": [
        "new_memories",
        "reinforced",
        "relationships",
        "goals_add",
        "goals_complete",
        "goal_touched",
        "action_beat",
        "stats",
        "new_characters",
        "scene_changed",
        "scene_prompt",
    ],
    "additionalProperties": False,
}


def _digest_prompt(story: dict, session_id: int, turn: int, user_text: str, reply: str) -> str:
    # Show the CURRENT value, not just the definition. The model is asked for
    # deltas; without the present value it has no anchor and cannot tell that a
    # stat is already pinned at its ceiling or sitting untouched at its floor.
    # In a 58-turn session this produced exactly that: scrutiny saturated at 100
    # while it kept adding, and standing never left 0.
    vals = store.stats(session_id)
    stat_lines = [
        f"- {s['key']} ({s['name']}): now {vals.get(s['key'], s['default']):g}"
        f" of {s['min']}..{s['max']}"
        f"{'  [AT MAXIMUM — only a decrease is possible]' if vals.get(s['key'], s['default']) >= s['max'] else ''}"
        f"{'  [AT MINIMUM — only an increase is possible]' if vals.get(s['key'], s['default']) <= s['min'] else ''}"
        f"\n    {s['description'].strip()}"
        for s in story.get("stats", [])
    ] or ["(this story defines no stats — return an empty stats array)"]

    rels = store.relationships(session_id)
    rel_lines = [f"- {r['name']}: {r['summary']}" for r in rels] or ["(none yet)"]

    active = store.active_goal(session_id)
    backlog = [g for g in store.goals(session_id, ["pending"])]
    goal_lines = ([f"- [{active['id']}] ACTIVE: {active['text']}"] if active else
                  ["(nothing active)"])
    goal_lines += [f"- [{g['id']}] waiting: {g['text']}" for g in backlog]

    recent = hot_temp(session_id, turn, limit=15)
    mem_lines = [f"- [{m['id']}] {m['text']}" for m in recent] or ["(none yet)"]

    # Include how each character is likely to be *described* before the player
    # learns their name. Narration deliberately says "the half-elf" rather than
    # "Lirael", and without this mapping the extractor attributes the wrong
    # descriptions to the wrong people.
    cast_lines = [
        f"- {c['name']}" + (f" — {c['short']}" if c.get("short") else "")
        for c in story.get("cast", [])
    ] or ["(none defined)"]

    # A narrative-mode story has no player in it, so every player-centric phrase
    # below is asking the extractor about somebody who does not exist. Left
    # unchanged it attributes the leads' feelings about each other to a "you"
    # that is nowhere in the prose, and the relationship layer fills with
    # nonsense.
    narrative = story.get("mode") == "narrative"
    leads = " and ".join(c["name"] for c in story.get("cast", [])[:2]) or "the leads"

    if narrative:
        rel_task = (
            f"relationships: only characters whose standing with someone else actually "
            f"shifted — above all {leads}, with each other. 1-3 sentences, present tense, "
            f"how they now regard the other. Use the canonical name from the roster above, "
            f"matching by description if the narration did not name them. Never attribute "
            f"one character's description to another; if you cannot tell who a description "
            f"refers to, leave them out entirely.")
        goal_task = (
            "goals_add: AT MOST ONE, and usually none. Short imperative phrase.\n\n"
            "  A goal is something the leads have explicitly committed to that will take "
            "more than one scene to finish: a road agreed on, a promise made, a debt owed, "
            "somebody they said they would go back for.\n\n"
            "  It is NOT a question, NOT something to find out, NOT a single action inside "
            "the current scene, and NOT something the narration merely raised. If it could "
            "be finished before the end of this passage, it is not a goal. If it begins "
            "with Determine, Discover, Clarify, Understand, Identify or Investigate, it is "
            "a question — leave it out.")
        goal_touched_task = (
            "goal_touched: true if this passage engaged with the ACTIVE objective above — "
            "moved toward it, mentioned it, was delayed by it, or deliberately avoided it. "
            "False if the scene simply had nothing to do with it. This times a reminder, so "
            "guess conservatively: a passing reference counts, the objective merely still "
            "being true does not. False when nothing is active.")
        exchange = f"# The passage just now\n\n{reply}"
        if user_text.strip():
            exchange = (f"# The reader's direction for this passage\n{user_text}\n\n"
                        f"# The passage just now\n\n{reply}")
        rel_header = "# How the characters currently stand with each other"
        cast_header = ("# Known characters, with how the narration may refer to them before\n"
                       "# they are named. Match descriptions to the right person using these.")
    else:
        rel_task = (
            "relationships: only characters whose standing with the player actually shifted. "
            "1-3 sentences, present tense, their view of the player. Use the canonical name "
            "from the roster above, matching by description if the narration did not name "
            "them. Never attribute one character's description to another; if you cannot "
            "tell who a description refers to, leave them out entirely.")
        goal_task = (
            "goals_add: AT MOST ONE, and usually none. Short imperative phrase.\n\n"
            "  A goal is an objective the player has explicitly committed to that will take "
            "more than\n  one scene to finish: a job accepted, a journey agreed, a promise "
            "made, a debt owed.\n\n"
            "  It is NOT a question, NOT something to find out, NOT a single action inside "
            "the current\n  scene, and NOT something the narration merely raised. If it "
            "could be finished before\n  the end of this turn, it is not a goal. If it "
            "begins with Determine, Discover, Clarify,\n  Understand, Identify or "
            "Investigate, it is a question — leave it out.")
        goal_touched_task = (
            "goal_touched: true if this exchange engaged with the ACTIVE objective above —\n"
            "  worked toward it, mentioned it, was delayed by it, or deliberately avoided "
            "it. False if the\n  scene simply had nothing to do with it. This times a "
            "reminder, so guess conservatively:\n  a passing reference counts, the "
            "objective merely still being true does not. False when\n  nothing is active.")
        exchange = f"# The exchange just now\nPLAYER: {user_text}\n\nNARRATOR: {reply}"
        rel_header = "# Current relationships"
        cast_header = ("# Known characters, with how the narration may refer to them before the "
                       "player\n# learns their names. Match descriptions to the right person "
                       "using these.")

    return f"""You maintain the state of an ongoing interactive story. You are not writing the story — you are reading the latest exchange and reporting what changed.

# Stats this story tracks
{chr(10).join(stat_lines)}

{cast_header}
{chr(10).join(cast_lines)}

{rel_header}
{chr(10).join(rel_lines)}

# The objective, and anything waiting behind it
{chr(10).join(goal_lines)}

# Recent memories (id in brackets)
{chr(10).join(mem_lines)}

{exchange}

# Your task
Report only what genuinely changed. Empty arrays are correct and expected — most turns change little.

- new_memories: facts worth remembering. salience 0-100. Do not record things already listed above.
  DEFAULT TO kind "temp". Reserve "long" for the small number of facts that will still matter fifty turns from now: a name, a promise, a debt, an identity revealed, a permanent change in the world. Everything else — what was said this scene, who reacted how, the state of a room, a mood, a detail of an ongoing conversation — is "temp", and temp memory is not lost: what keeps mattering gets referenced again, and reinforcement promotes it to long-term on its own. If in doubt, "temp". A long-term store that accumulates every passing detail retrieves worse than one that holds only what endures.
- reinforced: ids of listed memories this exchange referenced or made more relevant.
- {rel_task}
- {goal_touched_task}
- action_beat: true if this exchange contained physical danger — a fight, a chase, a
  rescue, a training bout, an accident, anyone hurt or nearly hurt. False for an
  argument, a threat that stays verbal, or a story merely being told about violence.
  This paces the story's action, so judge it on what happened, not on how tense it felt.
- {goal_task}
- goals_complete: ids of open goals now finished or abandoned.
- stats: deltas only, and only for the keys listed above, following each stat's own rules. Omit a stat if it did not move.
- new_characters: PROPER NAMES ONLY, of people who were named in this exchange for the
  first time and who are likely to appear again. Usually empty.
  Never a description in place of a name ("the boy's mother", "the clerk in the knitted
  cap", "the horn-man") — if you do not have a name, omit them. Never a place, a country,
  a world, an organisation or a title. Never a person merely mentioned in conversation or
  recalled from history; they must be present in the scene. If someone already known
  turns out to have a fuller name, omit them rather than listing the new form.
- scene_changed: true only if the location or situation changed materially — a new place, a jump in time, a shift from travel to combat. Not true for continued conversation in the same spot.
- scene_prompt: if scene_changed, a short comma-separated visual description of the new setting for an illustrator (no characters, just place, time of day, weather, mood). Otherwise "".
"""


_QUESTION_VERBS = ("determine ", "discover ", "clarify ", "understand ", "identify ",
                   "investigate ", "find out ", "figure out ", "learn ", "ask ")


def _clean_goals(raw: Any) -> list[str]:
    """Objectives only — not questions, and not many.

    An objective is something the player committed to doing. A question the
    narration raised is not one, and treating it as one is what turns a goal list
    into a list of things the story now feels obliged to explain.
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for g in raw:
        if not isinstance(g, str):
            continue
        t = " ".join(g.split())
        if not t or t.lower().startswith(_QUESTION_VERBS):
            continue
        out.append(t)
    return out[:config.GOALS_MAX_ACTIVE + 1]


_BAD_NAME_START = ("the ", "a ", "an ", "his ", "her ", "their ", "some ", "another ")


def _clean_character_names(names: list, story: dict, session_id: int) -> list[str]:
    """Keep only things that are plausibly a person's name.

    Left unchecked this filled a two-hander's cast panel with eighteen entries,
    including "the boy's mother", "The clerk in the knitted cap", the name of the
    world itself, and one man listed twice under a short and a long form.
    """
    known = {c["name"].lower() for c in story.get("cast", [])}
    for c in story.get("cast", []):
        known.update(a.lower() for a in c.get("aliases", []))
    known.update(r["name"].lower() for r in store.relationships(session_id))
    known.update(m["subject"].lower() for m in store.media(session_id)
                 if m["kind"] == "portrait")

    # Ban the story's own words, not just its full title. "Veyra: Two" as a title
    # did not stop the extractor filing the world itself, "Veyra", as a character.
    banned = {story.get("name", "").lower(), story.get("id", "").lower()}
    for token in re.split(r"[^a-z0-9]+", (story.get("name", "") + " " + story.get("id", "")).lower()):
        if len(token) >= 3:
            banned.add(token)
    banned.discard("")

    out: list[str] = []
    for raw in names:
        if not isinstance(raw, str):
            continue
        n = " ".join(raw.split())
        low = n.lower()
        if not n or len(n) > 40 or len(n.split()) > 4:
            continue
        if low in banned or low.split()[0] in banned:
            continue          # the world, the story, the setting
        if low.startswith(_BAD_NAME_START) or "'s " in low:
            continue          # a description, not a name
        if not n[0].isupper():
            continue          # proper nouns are capitalised
        if low in known:
            continue          # already have them
        # "Varro" vs "Ansell Varro" — same person, listed twice. Treat a name whose
        # words are a subset of a known one (or vice versa) as already present.
        parts = set(low.split())
        if any(parts <= set(k.split()) or set(k.split()) <= parts for k in known):
            continue
        out.append(n)
        known.add(low)
    return out


def digest(
    story: dict, session_id: int, turn: int, user_text: str, reply: str
) -> dict[str, Any]:
    """The one utility call per turn. Applies the delta and returns a summary."""
    if not config.UTILITY_ENABLED:
        return {"skipped": True}

    prompt = _digest_prompt(story, session_id, turn, user_text, reply)
    delta = brain.utility(prompt, DELTA_SCHEMA)
    return apply_delta(story, session_id, turn, delta)


def apply_delta(
    story: dict, session_id: int, turn: int, delta: dict[str, Any]
) -> dict[str, Any]:
    """Write a delta to the store. Separated from digest() so it can be tested
    and replayed without a model call."""
    applied: dict[str, Any] = {
        "memories_added": 0, "reinforced": 0, "relationships": 0,
        "goals_added": 0, "goals_completed": 0, "stats": {},
        "new_characters": [], "scene_changed": False, "scene_prompt": "",
    }

    # memories (embed long-term ones so they are retrievable)
    new = delta.get("new_memories") or []
    long_texts = [m["text"] for m in new if m.get("kind") == "long"]
    vectors = brain.embed(long_texts) if long_texts else []
    vi = 0
    for m in new:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        kind = "long" if m.get("kind") == "long" else "temp"
        sal = max(0.0, min(100.0, float(m.get("salience", 50))))
        vec = None
        if kind == "long":
            vec = vectors[vi] if vi < len(vectors) and vectors[vi] else None
            vi += 1
        store.add_memory(session_id, text, kind, sal, turn, vec)
        applied["memories_added"] += 1

    ref = [int(i) for i in (delta.get("reinforced") or []) if isinstance(i, (int, float))]
    if ref:
        store.reinforce(ref, turn)
        applied["reinforced"] = len(ref)

    for r in delta.get("relationships") or []:
        name, summary = (r.get("name") or "").strip(), (r.get("summary") or "").strip()
        if name and summary:
            store.upsert_relationship(session_id, name, summary, turn)
            applied["relationships"] += 1

    # The filter used to run AFTER the insert loop below, mutating a list nothing
    # read again — so neither the question-phrasing rejects nor the cap had any
    # effect, which is how sixteen open objectives happened.
    delta["goals_add"] = _clean_goals(delta.get("goals_add"))

    for gid in delta.get("goals_complete") or []:
        try:
            store.set_goal_status(int(gid), "complete", turn)
            applied["goals_completed"] += 1
        except (TypeError, ValueError):
            continue

    if delta.get("action_beat"):
        mark_action(session_id, turn)
        applied["action_beat"] = True

    if delta.get("goal_touched"):
        current = store.active_goal(session_id)
        if current:
            store.touch_goal(current["id"], turn)

    # One objective at a time. Anything extracted while a slot is taken waits in
    # the backlog rather than being thrown away — it is usually a real commitment,
    # just not the one the story is currently about.
    n_active = len(store.goals(session_id, ["active"]))
    existing = {g["text"].strip().lower() for g in store.goals(session_id)}
    for text in delta["goals_add"]:
        t = text.strip()
        if not t or t.lower() in existing:
            continue
        if config.GOALS_NEED_CONFIRMATION or n_active >= config.GOALS_MAX_ACTIVE:
            status = "pending"
        else:
            status = "active"
            n_active += 1
        store.add_goal(session_id, t, status, turn)
        existing.add(t.lower())
        applied["goals_added"] += 1

    # A finished objective leaves an empty slot, and an empty slot means the story
    # is pointed at nothing. Promote the newest thing waiting — newest, because a
    # commitment made sixty turns ago has usually been overtaken.
    if not config.GOALS_NEED_CONFIRMATION:
        waiting = store.goals(session_id, ["pending"])
        while waiting and len(store.goals(session_id, ["active"])) < config.GOALS_MAX_ACTIVE:
            store.set_goal_status(waiting.pop()["id"], "active", turn)
            applied["goals_promoted"] = applied.get("goals_promoted", 0) + 1

    defs = {s["key"]: s for s in story.get("stats", [])}
    for st in delta.get("stats") or []:
        key = (st.get("key") or "").strip()
        if key not in defs:
            continue
        d = defs[key]
        val = store.adjust_stat(session_id, key, float(st.get("delta", 0)), d["min"], d["max"])
        applied["stats"][key] = val

    applied["new_characters"] = _clean_character_names(
        delta.get("new_characters") or [], story, session_id)
    applied["scene_changed"] = bool(delta.get("scene_changed"))
    applied["scene_prompt"] = (delta.get("scene_prompt") or "").strip()

    applied["pruned"] = decay_and_prune(session_id, turn)
    return applied


def action_demand(story: dict, session_id: int, turn: int) -> str:
    """The depth-injected version, for when a fight is properly overdue."""
    if action_level(story, session_id, turn) != "firm":
        return ""
    kind = (story.get("pacing") or {}).get("action_kind") or "a fight"
    quiet = action_quiet(session_id, turn)
    return (
        f"{kind.capitalize()} starts in this reply — {quiet} turns without anything "
        "physical is too long for this story. Not foreshadowed, not approaching: it "
        "happens, on the page, and the player is in it before the reply ends. Cut off "
        "whatever is currently going on to do it. It is unrelated to the current plot, "
        "the last villain and anyone's backstory. Do not resolve it in this reply."
    )
