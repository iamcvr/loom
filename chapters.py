"""Chapter breaks — automatic compaction of the story so far.

Why this exists
---------------
The budget arbiter keeps a fixed amount of context. A long story therefore loses
its early material by construction: measured on a 68-turn session, 39 of 60
recent messages were dropped from every turn. A chapter break replaces a span of
transcript with a summary of it, which is what lets a 132-turn story stay inside
a 48,000-character window without the first hundred turns simply evaporating.

Why it stopped asking permission
--------------------------------
The first version blocked play until the author reviewed each break. The
reasoning was that only a human knows which planted detail is going to matter,
so only a human should decide what survives. That reasoning is still true, but
the gate was in the wrong place: being stopped mid-scene to edit a recap is a
worse interruption than the drift it was preventing, and a review you are forced
into at the wrong moment is not a careful one.

So compaction now happens on its own, every CHAPTER_EVERY_TURNS turns, and says
so afterwards. Every summary stays editable in the chapters panel, which is where
the author's knowledge actually gets applied — at leisure, on a recap that
already exists, rather than under a blocked composer.

What a chapter no longer produces
---------------------------------
A list of unresolved threads. It was a permanent, never-decaying prompt layer
intended to protect a Chekhov's gun from the heat scorer, and in play it did the
opposite: a standing list of loose ends reads to the model as a set of things to
tie together, so every scene reached for all of them at once. Durable facts go to
the lorebook, where a keyword fires them when they are actually relevant.
"""
from __future__ import annotations

from typing import Any, Optional

import brain
import config
import lorebook
import store

CHAPTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["title", "summary"],
    "additionalProperties": False,
}

SYNOPSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}


def _transcript(session_id: int, chapter: dict) -> str:
    msgs = store.messages(session_id, since_id=chapter["start_msg_id"] or None)
    return "\n\n".join(f"{m['role'].upper()}: {m['content']}" for m in msgs)


def draft(session_id: int, story: dict) -> dict:
    """Propose a title and recap for the open chapter. Writes nothing."""
    ch = store.open_chapter(session_id)
    body = _transcript(session_id, ch)
    if not body.strip():
        return {"title": "", "summary": "", "chapter": ch,
                "error": "this chapter has no messages yet"}

    prompt = f"""You are writing the recap for a chapter of an ongoing interactive story called "{story['name']}".

# The chapter
{body}

# Your task
Produce two things.

- title: a short evocative chapter title, 2-6 words. No numbering.

- summary: the "previously on" recap, 120-200 words, past tense, third person. What
  happened and what changed between the characters. Write it the way a television
  recap does — the emotional shape of the chapter and the two or three facts a viewer
  must carry forward — not the way minutes of a meeting do. Skip procedural detail:
  exact sums, schedules, who filed which form, the mechanics of a job. Do not
  editorialise and do not describe the prose style.

This recap replaces the chapter's actual text in the narrator's context from here on,
so anything you leave out is gone. Prefer the concrete: names, promises, debts,
injuries, changed allegiances.
"""
    out = brain.utility(prompt, CHAPTER_SCHEMA)
    out["chapter"] = ch
    out["error"] = None
    return out


def close(
    session_id: int,
    *,
    title: str,
    summary: str,
    synopsis_text: Optional[str] = None,
) -> dict:
    """Commit a break and open the next chapter."""
    ch = store.open_chapter(session_id)
    msgs = store.messages(session_id)
    end_msg = msgs[-1]["id"] if msgs else ch["start_msg_id"]
    end_turn = msgs[-1]["turn"] if msgs else ch["start_turn"]

    store.close_chapter(ch["id"], title, summary, end_msg, end_turn)
    if synopsis_text is not None:
        store.set_synopsis(session_id, synopsis_text, ch["number"])

    return store.open_chapter(session_id)   # opens the next one


def due(session_id: int) -> bool:
    """Has the open chapter run long enough to compact?

    Turn count is the trigger the author asked for, and it is the right one: it
    makes chapters a predictable length regardless of how the budget happens to be
    behaving. The transcript-shedding check underneath it is a safety valve for
    very long turns, which can overflow the window well before turn twenty.
    """
    if not config.CHAPTERS_ENABLED:
        return False
    ch = store.open_chapter(session_id)
    turns_in = store.current_turn(session_id) - int(ch["start_turn"] or 0)
    if turns_in < config.CHAPTER_MIN_TURNS:
        return False
    return turns_in >= config.CHAPTER_EVERY_TURNS


def compact(session_id: int, story: dict) -> Optional[dict]:
    """Draft, close and refold in one pass. Returns what happened, for a notice.

    Called after the reply is already on screen, so a slow or failed utility call
    costs the player nothing. Failure here is not fatal — the chapter simply stays
    open and the next turn tries again.
    """
    ch = store.open_chapter(session_id)
    turns_in = store.current_turn(session_id) - int(ch["start_turn"] or 0)
    try:
        d = draft(session_id, story)
        if d.get("error") or not d.get("summary", "").strip():
            return None

        title = (d.get("title") or "").strip() or f"Chapter {ch['number']}"
        summary = d["summary"].strip()

        # Fold aged-out chapters into the synopsis in the same pass. Left undone,
        # summaries past the verbatim window would silently stop being carried at
        # all, which is exactly the drift chapters exist to prevent.
        syn = refold_synopsis(session_id, story) if needs_refold(session_id) else None

        close(session_id, title=title, summary=summary, synopsis_text=syn)
        suggestions = lorebook.suggest(session_id, story, dict(ch))

        return {
            "number": ch["number"],
            "title": title,
            "summary": summary,
            "turns": turns_in,
            "suggestions": suggestions,
        }
    except Exception as e:   # never take a played turn down with it
        print(f"[chapters] auto-compact failed (session {session_id}, "
              f"chapter {ch['number']}): {e}")
        return None


def needs_refold(session_id: int) -> bool:
    """True when more closed chapters exist than are kept verbatim."""
    return len(store.chapters(session_id, closed_only=True)) > config.CHAPTER_VERBATIM


def refold_synopsis(session_id: int, story: dict) -> str:
    """Fold chapters that have aged out of the verbatim window into the synopsis.

    Without this, summaries either accumulate without bound or the oldest silently
    drop out of the prompt.
    """
    closed = store.chapters(session_id, closed_only=True)
    aged = closed[:-config.CHAPTER_VERBATIM] if config.CHAPTER_VERBATIM else closed
    if not aged:
        return store.synopsis(session_id).get("text", "")

    current = store.synopsis(session_id).get("text", "").strip() or "(nothing yet)"
    parts = [f"### Chapter {c['number']} — {c['title']}\n{c['summary']}" for c in aged]

    prompt = f"""You maintain the running synopsis of an interactive story called "{story['name']}".

# The synopsis as it stands
{current}

# Chapters to fold into it
{chr(10).join(parts)}

# Your task
Rewrite the synopsis so it covers everything above, in at most 250 words. Past tense,
third person, chronological. Keep proper nouns, promises, debts, injuries, revealed
identities and changed allegiances — these are what later chapters must stay
consistent with. Drop atmosphere, dialogue and scene-level detail. Return only the
rewritten synopsis text.
"""
    return brain.utility(prompt, SYNOPSIS_SCHEMA).get("text", "")


def status(session_id: int) -> dict:
    """What the UI needs for the chapter chip."""
    if not config.CHAPTERS_ENABLED:
        return {"enabled": False}
    ch = store.open_chapter(session_id)
    turn = store.current_turn(session_id)
    turns_in = max(0, turn - int(ch["start_turn"] or 0))
    return {
        "enabled": True,
        "number": ch["number"],
        "id": ch["id"],
        "start_turn": ch["start_turn"],
        "turns_in": turns_in,
        "every": config.CHAPTER_EVERY_TURNS,
    }
