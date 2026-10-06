"""Simple mode: the engine interviews the author and writes the story.

Writing a story by hand means producing several thousand characters of specific,
opinionated craft before anyone has played a turn. Most people cannot, a vague
ruleset produces vague stories, and the model then takes the blame for a prompt that
never told it anything.

So the model asks instead. Two calls, deliberately separate:

  ask()      a small schema -- what to say, what to ask next, whether it has enough
  compose()  a large one    -- the finished story, called once

One call doing both would mean a schema holding an entire story alongside a
conversational reply, and constrained generation gets worse as the schema grows. It
also lets the interview run on short, fast calls and spend the long one only once.

What it writes is a PROPOSAL. It lands in the editor for the author to read and
change, exactly like the settings advisor's diff; nothing is saved by this module.
"""
from __future__ import annotations

from typing import Any

import brain
import config

# ---------------------------------------------------------------- the interview

ASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # `learned` comes first and is required for one reason: without it the model
        # re-asks questions the author has already answered, word for word. Measured
        # -- a rich two-paragraph answer covering tone, cast and setting produced the
        # same three questions again on the next turn. Making it write down what it
        # now knows, before it is allowed to ask anything, is what breaks the loop.
        "learned": {"type": "array", "maxItems": 10, "items": {"type": "string"}},
        "still_needed": {"type": "array", "maxItems": 5, "items": {"type": "string"}},
        "reply": {"type": "string"},
        "questions": {"type": "array", "maxItems": 3, "items": {"type": "string"}},
        "ready": {"type": "boolean"},
    },
    "required": ["learned", "still_needed", "reply", "questions", "ready"],
}

# What CORE_RULES already covers. The single most valuable thing in these prompts:
# a story that restates the engine's contract wastes budget and can contradict it.
_ALREADY_HANDLED = """The engine already instructs the narrator on all of this, and the
story must NOT restate any of it:

- it is the world and every character in it, narrating in second person
- the player's character is theirs alone: it never writes their dialogue, decides
  their actions, or narrates their thoughts
- it accepts any input format without remarking on it
- its narration is clean prose, no asterisk actions or stage directions
- one thing happens at a time, and the player ends scenes
- a weighty spoken line gets its own paragraph as **Name** | "what they say."
- how long a turn runs"""

_INTERVIEW = """You are interviewing someone who wants to play in a story you are about
to write for them. They are the author and the player both.

Work in this order, every turn:

1. `learned` -- everything you now know, one short line each, from the WHOLE
   conversation and not just the last message. Include what you inferred.
2. `still_needed` -- only what is genuinely missing after that.
3. `questions` -- at most three, drawn ONLY from `still_needed`.

**Never ask about anything in `learned`.** Re-asking a question the author already
answered is the worst thing you can do here: it reads as not listening, and it is the
single most common way this goes wrong. Before you write a question, check it against
`learned` and against every question already in the transcript. If it appears in
either, drop it.

Be conversational, not a form. Say what you inferred so they can correct it.

What you need before you can write it:

  the world      where and when, and what the player is doing there when it opens
  the cast       two to five people who matter, and who each is to the player
  the feel       warm and funny, grounded, grim, or pulp adventure
  the people     adults, young adults, teenagers, or a mix; and how capable they are
  conflict       what violence looks like here, or whether there is any
  focus          whether this is about the people or the plot
  romance        slow and player-led, background, or none -- and how explicit
  mystery        deduction, a little, or none
  continuity     one connected story, or mostly unrelated events
  the session    what a typical one is MADE of: ordinary life first, incident first,
                 or an even mix. ASK THIS EXPLICITLY -- nobody volunteers it, and it
                 decides whether the story reads as a life or a highlight reel
  opposition     individual people, organised factions, or circumstance
  stakes         real consequences but the player survives; mortal danger where only
                 the player decides their character dies; or nothing truly bad
  powers         whether this world has named powers, and what it calls them

Set `ready` true once you could write a specific, opinionated story -- NOT once you
have asked everything on the list. The world, the cast and the feel are enough; the
rest you may infer and state as an inference. Two good exchanges is usually plenty,
and if `still_needed` is down to details you could reasonably decide yourself, you are
ready. Being over-thorough is its own failure: people abandon interviews. A rich first answer can leave you ready after one
exchange. When you set it, say in `reply` what you understood, in two short paragraphs,
so they can correct you before anything is written.

%s

The conversation so far:
%s""" % (_ALREADY_HANDLED, "%s")


def _transcript(history: list[dict]) -> str:
    if not history:
        return "(nothing yet -- open the interview)"
    out = []
    for m in history:
        who = "AUTHOR" if m.get("role") == "user" else "YOU"
        out.append(f"{who}: {(m.get('text') or '').strip()}")
    return "\n\n".join(out)


def ask(history: list[dict]) -> dict:
    """One turn of the interview."""
    out = brain.utility(_INTERVIEW % _transcript(history), ASK_SCHEMA)
    # `learned` is returned, not just required. Writing it is what stops the model
    # re-asking answered questions, and it costs real time -- about 46s a turn
    # against 8s without it. Showing it back makes that a running summary the author
    # can correct rather than latency they cannot see the point of.
    return {
        "learned": [str(x).strip() for x in (out.get("learned") or []) if str(x).strip()],
        "still_needed": [str(x).strip() for x in (out.get("still_needed") or []) if str(x).strip()],
        "reply": str(out.get("reply") or "").strip(),
        "questions": [str(q).strip() for q in (out.get("questions") or []) if str(q).strip()],
        "ready": bool(out.get("ready")),
    }


# ---------------------------------------------------------------- the story

STORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "tagline": {"type": "string"},
        "power_label": {"type": "string"},
        "rules": {"type": "string"},
        "details": {"type": "string"},
        "protagonist": {
            "type": "object",
            "properties": {
                "name": {"type": "string"}, "short": {"type": "string"},
                "pronouns": {"type": "string"}, "description": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["name", "description", "prompt"],
        },
        "prologue": {"type": "string"},
        "opening_scene": {"type": "string"},
        "suggestions": {"type": "array", "items": {"type": "string"}},
        "cast": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"}, "short": {"type": "string"},
                    "prompt": {"type": "string"},
                },
                "required": ["name", "short", "prompt"],
            },
        },
        "keywords": {
            # minItems and a required entry, because asking in prose did not work:
            # three runs returned none at all. Constrained decoding honours the
            # schema; it only reads the instructions.
            "type": "array",
            "minItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "body": {"type": "string"},
                },
                "required": ["title", "keywords", "body"],
            },
        },
    },
    "required": ["name", "tagline", "rules", "details", "protagonist", "prologue",
                 "cast", "keywords", "opening_scene", "suggestions"],
}

_COMPOSE = """Write the story you have just been told about. Output only the fields asked
for; it will be loaded into an editor for the author to change.

%s

HOW TO WRITE `rules`. This is the heart of it, and the difference between a story that
reads well and one that does not. Short, titled, imperative paragraphs -- the way a
showrunner briefs a writers' room, not the way a spec describes a feature.

Be specific and opinionated. "No conspiracies" is weak. This is strong:

  DO NOT CONNECT THINGS. Events are mostly unrelated to each other. A robbery on
  Thursday, a fire on Saturday and a rude letter are three ordinary things, not one
  plot. Coincidence is realistic. If you notice yourself arranging details into a
  pattern, throw the pattern away.

The difference is that the second tells the narrator what to do when it catches itself.
Every rule should be actionable mid-sentence.

Write a doctrine for whatever this story does MOST. If there is fighting, say exactly
what a fight looks like here. If it is a workplace, say what a meeting looks like. A
narrator defaults to the most generic version of any scene and the rules are where you
take that away. Say what NOT to do, in particular rather than in general.

Include a WHAT A SESSION IS MADE OF block, numbered, in order of how much room each
thing gets. It is the rule that most changes how the story reads.

LENGTH IS NOT OPTIONAL. `rules` must be **at least 3,000 characters** and should be
4,000-7,000. This is the single most common way this goes wrong: a short ruleset reads
as reasonable and produces bland, generic prose forever, because every sentence you did
not write is a decision the narrator makes generically. Eight to twelve titled blocks,
each a full paragraph. If you find yourself finishing early, you have written headings
where paragraphs belong.

SEPARATE EVERY BLOCK WITH A BLANK LINE. One wall of text is unreadable and the narrator
weights it worse.

DO NOT WRITE A TURN LENGTH, an input-format rule, a "never write the player's dialogue"
rule, or an asterisk rule. Those are the engine's and are already in the prompt. Writing
them again is the most common failure here.

LIMITS, which matter because exceeding them silently truncates the prompt:
- `rules` plus `details` under 9,000 characters total.
- `details` is FACTS about the world, not instructions to the narrator.
- `protagonist.description` under 2,000 characters.
- `keywords`: write THREE to FIVE entries. They are the lorebook -- a place, a
  faction, a piece of history, a recurring object -- each fired by the words that
  would appear when it comes up. Body under 1,500 characters each. Returning none
  wastes the one mechanism that gives a world depth it does not pay for every turn.
- `protagonist.name` is a real name the author can change, never "You" or a
  placeholder. Give them an age and a history in `description`.
- `prompt` fields are comma-separated image tags, never prose.

THE PROLOGUE is the first thing the author reads. Establish the place, put two named
people on the page disagreeing about something small, and stop without resolving it.
Use the attributed form two or three times so the narrator sees the pattern:

  **Name** | "Something that lands."

The conversation:
%s""" % (_ALREADY_HANDLED, "%s")


def compose(history: list[dict]) -> dict:
    """The finished story, as a dict the editor can load. Saves nothing."""
    # Six thousand tokens of structured output against a schema with a minimum
    # keyword count takes minutes on a local model, and the default HTTP timeout is
    # sized for the short calls everything else makes. Measured at 81-97s before the
    # schema required keywords, and it timed out after.
    spec = {**config.UTILITY, "max_tokens": 6000}
    return brain.utility(_COMPOSE % _transcript(history), STORY_SCHEMA,
                         spec=spec, timeout=600)


def to_story(d: dict) -> dict:
    """Shape compose()'s output into the story schema story.py validates.

    The two differ on purpose: a flat prologue and one intro is far easier for a
    model to produce reliably than a nested intros list, and the editor is where a
    second intro gets added anyway.
    """
    return {
        "name": d.get("name") or "Untitled",
        "tagline": d.get("tagline") or "",
        "power_label": (d.get("power_label") or "").strip(),
        "rules": d.get("rules") or "",
        "details": d.get("details") or "",
        "protagonist": d.get("protagonist") or None,
        "intros": [{
            "id": "start",
            "name": "Start",
            "prologue": d.get("prologue") or "",
            "opening_scene": d.get("opening_scene") or "",
            "suggestions": d.get("suggestions") or [],
        }],
        "cast": d.get("cast") or [],
        "keywords": [{**k, "always": False} for k in (d.get("keywords") or [])],
        "stats": [],
    }
