"""The lorebook — durable knowledge you can actually see and edit.

Why this replaces a visible memory panel
----------------------------------------
Long-term memory was a growing list with a similarity search on the front. A
132-turn session put 497 rows in it and retrieved eight per turn, so 489 of them
existed only to make the other eight harder to find. Worse, the list was the
thing the author was shown, which invited managing it — and there is nothing
useful to do with 497 rows.

A lorebook inverts both problems. Entries are few, written once, and fire
*deterministically*: mention "Diamond Dogs" and the Diamond Dogs entry is in the
prompt, whatever else is competing for retrieval that turn. That is the same
mechanism a story's authored notes already use, so this is not a new subsystem —
it is the authored-notes list, extended to accept entries the story earned.

Memory itself does not go away. It keeps running underneath, unshown, doing what
it is good at: the passing detail that matters for six turns and then does not.

Suggestions
-----------
Drafted at a chapter break, from that chapter's own material, capped and
deduplicated. They are inert until accepted — a suggested entry contributes
nothing to the prompt. A candidate has to have recurred across several turns to
be proposed at all, which is the "it came up a couple of times" signal, measured
rather than guessed.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

import brain
import config
import store

SUGGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "body", "keywords"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["entries"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- prompt notes


def notes(session_id: int) -> list[dict]:
    """Accepted entries, in the shape story.keywords uses.

    Returning the same dict shape as an authored note is what lets both come down
    the one keyword_notes layer, ranked against each other by recency of mention
    rather than by which file they happen to live in.
    """
    if not config.LOREBOOK_ENABLED:
        return []
    return [
        {
            "title": e["title"],
            "body": e["body"],
            "keywords": [k.lower() for k in e["keywords"]] or [e["title"].lower()],
            "always": e["always"],
            "lore_id": e["id"],
        }
        for e in store.lore(session_id, ["active"])
        if e["body"].strip()
    ]


# ---------------------------------------------------------------- candidates

_STOP = {
    "the", "and", "but", "for", "with", "that", "this", "they", "them", "their",
    "she", "her", "his", "him", "you", "your", "was", "were", "had", "has", "not",
    "one", "two", "out", "off", "who", "what", "when", "where", "why", "how",
    "then", "than", "into", "over", "back", "down", "just", "like", "said", "says",
    "chapter", "player", "narrator",
}


def recurring_terms(session_id: int, since_msg_id: int | None, min_turns: int) -> list[str]:
    """Capitalised terms that appeared in at least `min_turns` distinct turns.

    A cheap, local pre-filter so the model is asked about things the story keeps
    returning to rather than everything that was ever named. Proper nouns only —
    it is the recurring *entity* (a person, a place, an organisation, an artefact)
    that earns a permanent entry, not a recurring adjective.
    """
    msgs = store.messages(session_id, since_id=since_msg_id or None)
    per_turn: dict[str, set[int]] = {}
    for m in msgs:
        turn = int(m.get("turn") or 0)
        for raw in re.findall(r"\b[A-Z][a-zA-Z'’-]{2,}(?:\s+[A-Z][a-zA-Z'’-]{2,})?", m["content"]):
            term = " ".join(raw.split())
            if term.lower() in _STOP or term.split()[0].lower() in _STOP:
                continue
            per_turn.setdefault(term, set()).add(turn)
    ranked = sorted(
        ((t, len(v)) for t, v in per_turn.items() if len(v) >= min_turns),
        key=lambda kv: kv[1],
        reverse=True,
    )
    return [t for t, _ in ranked[:40]]


def _known(session_id: int, story: dict) -> set[str]:
    """Everything already covered, so nothing gets proposed twice.

    Dismissed suggestions count as known. Being re-offered an entry you already
    said no to, every twenty turns, for the life of the story, would make the
    whole feature something to turn off.
    """
    out: set[str] = set()
    for note in story.get("keywords", []):
        out.add(note["title"].lower())
        out.update(k.lower() for k in note.get("keywords", []))
    for e in store.lore(session_id, ["active", "suggested", "dismissed"]):
        out.add(e["title"].lower())
        out.update(k.lower() for k in e["keywords"])
    return out


# ---------------------------------------------------------------- suggestion


def suggest(session_id: int, story: dict, chapter: dict) -> list[dict]:
    """Propose lorebook entries from a just-closed chapter. Writes them as
    'suggested', which contributes nothing to the prompt until accepted."""
    if not (config.LOREBOOK_ENABLED and config.UTILITY_ENABLED):
        return []

    known = _known(session_id, story)
    terms = [
        t for t in recurring_terms(
            session_id, chapter.get("start_msg_id"), config.LOREBOOK_MIN_MENTIONS)
        if t.lower() not in known
    ]
    if not terms:
        return []

    msgs = store.messages(session_id, since_id=chapter.get("start_msg_id") or None)
    body = "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in msgs)[-24_000:]

    have = sorted(known)[:60] or ["(nothing yet)"]

    prompt = f"""You maintain the lorebook for an interactive story called "{story['name']}".

A lorebook entry is a short, permanent reference note. It is injected into the
narrator's context whenever one of its keywords appears in play, and it stays
accurate for the rest of the story. It is a gazetteer entry, not a plot summary.

# Already covered — do not propose any of these again
{chr(10).join('- ' + h for h in have)}

# Terms that recurred across several turns of this chapter
{chr(10).join('- ' + t for t in terms)}

# The chapter
{body}

# Your task
Propose AT MOST {config.LOREBOOK_SUGGEST_MAX} entries, and fewer is better. Zero is a
perfectly good answer for a chapter that established nothing new.

Only propose something that is BOTH:
  - durable — it will still be true and still matter fifty turns from now, and
  - liable to be forgotten — the narrator would get it wrong or invent a new
    version of it if it were not written down.

Good entries: a place and what it is like, an organisation and what it wants, a
recurring person the story's cast list does not already define, a custom, a piece
of local history, a rule of how something works in this world.

Not entries: what the characters are doing, where they are going, what happened
this chapter, an emotional state, a one-off encounter, anything the chapter recap
already carries, or anything in the covered list above.

For each entry give:
- title: the thing's name, 1-4 words, exactly as the story spells it.
- body: 1-3 sentences of established fact, present tense. Only what the story has
  actually shown or stated — invent nothing.
- keywords: 1-4 lowercase trigger phrases that would appear in prose when this
  becomes relevant. Include the name itself. Keep them specific: a keyword like
  "man" or "city" fires on every turn and makes the entry worthless.
"""
    try:
        out = brain.utility(prompt, SUGGEST_SCHEMA)
    except Exception as e:  # a failed suggestion must never fail the turn
        print(f"[lorebook] suggestion failed (session {session_id}): {e}")
        return []

    made: list[dict] = []
    for e in (out.get("entries") or [])[: config.LOREBOOK_SUGGEST_MAX]:
        title = " ".join(str(e.get("title") or "").split())
        body_text = (e.get("body") or "").strip()
        if not title or not body_text or title.lower() in known:
            continue
        kws = _clean_keywords(e.get("keywords") or [], title)
        if not kws:
            continue
        eid = store.add_lore(
            session_id, title, body_text, kws,
            status="suggested", source="auto", chapter=int(chapter.get("number") or 0),
        )
        known.add(title.lower())
        made.append({"id": eid, "title": title, "body": body_text, "keywords": kws})
    return made


_BAD_KEYWORDS = {
    "man", "woman", "boy", "girl", "city", "town", "road", "house", "door", "room",
    "people", "person", "place", "thing", "day", "night", "work", "job", "money",
}


def _clean_keywords(raw: Iterable[Any], title: str) -> list[str]:
    """Drop triggers so generic they would fire every turn.

    A keyword that always matches is worse than no entry at all: it spends the
    keyword_notes allowance permanently and pushes out notes that were actually
    relevant to the scene.
    """
    out: list[str] = []
    for k in raw:
        k = " ".join(str(k).split()).strip().lower()
        if not k or len(k) < 3 or k in _BAD_KEYWORDS or k in _STOP:
            continue
        if k not in out:
            out.append(k)
    if title.lower() not in out:
        out.insert(0, title.lower())
    return out[:4]
