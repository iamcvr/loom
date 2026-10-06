"""Per-field help: say it roughly, get it back written for the engine.

The editor asks for several blocks of prose, each with its own job, and nothing on the
screen says what a good one looks like. So each field gets a button: write what you
mean however you like -- a line or a page -- and this rewrites it into the shape the
narrator reads well.

Per-field rather than one "write my story" button, because each field is a different
job: `details` wants facts and no instructions, `rules` wants instructions and no
facts, and one prompt covering both does each worse.

GENERATED AS PROSE, through the same brain.prose() path story turns use. The first
version asked for a JSON array of paragraph strings, and inside a JSON string the
model never reaches a natural end: it wrote until the token cap cut the JSON in half,
then -- with the cap removed -- until a per-string maxLength cut sentences in two, and
whatever ceiling was set became the length. Prose stops when the thought ends, the
way every turn already does. It also runs at PROSE's num_ctx, so calling it between
turns cannot make ollama reload the model at a different context size.

What comes back REPLACES the field in an unsaved editor, with an Undo beside it.
Nothing here writes to disk.
"""
from __future__ import annotations

import re

import brain
import config

_FRAME = """You are helping someone write one field of an interactive-fiction story
file. They will play in this story; a narrator model reads this field to run it. Rewrite
what they have written into what that narrator will use best. Keep their intent, their
names and their specifics; improve the shape, not the substance.

The engine already instructs the narrator separately on player agency, input formats,
clean prose, scene pacing, turn length and how dialogue is attributed. Never write any of
that here.

Reply with the field's new contents and nothing else -- no preamble, no heading, no
commentary, no markdown."""

FIELDS: dict[str, dict] = {
    "details": {
        "label": "World details",
        "guide": """This field is WHAT IS TRUE about the world: the facts the narrator needs
so it never contradicts itself -- the place, the era, how things work, who holds what,
what is normal here and what is not. It is read on every turn, so every sentence in it is
paid for on every turn.

Do this:
- Keep every fact, place, name and specific they gave you. Drop nothing, change nothing.
- Collapse repetition. Something said three ways should be said once, well.
- Group related facts into paragraphs -- the place itself, how the world works, ordinary
  life in it.
- Add a few concrete specifics that FOLLOW from what they wrote and that a scene could be
  built from. "A Miami suburb" becomes particular streets, buildings, routines. Only
  consequences of their premise; never a new premise of your own.
- If they wrote only a line or two, build outward from it in the same way.

Never:
- Instructions to the narrator ("keep it light", "scenes should..."). Those belong in the
  rules, and here they fight with them.
- Named people. The cast is its own field; a name invented here is a character the story
  does not know about. Describe roles and groups instead.
- Headings, lists, or bold text.""",
    },

    "prologue": {
        "label": "First page",
        # Seiran's runs about 2,400; a short first page undersells a story.
        "length": (1200, 800, 2000),
        "context": ("rules", "details", "cast"),
        # It carries the attributed-line format, which is bold. Stripping bold here
        # would turn every speaker block back into plain prose.
        "keep_bold": True,
        "guide": """This is the FIRST PAGE: what the player reads when they sit down, shown
once as the narrator's opening turn. It is a scene, not a summary and not a setup.

THIS TEXT IS NARRATION, so it obeys the same contract the narrator does on every turn.
The player is "you", and "you" is theirs alone:
- You never write a single word the player says. Speaker lines are for OTHER people.
- You never name the player, describe their appearance, or give them a past.
- You never decide what the player does, beyond the situation they are standing in at
  the first line. No "you grab your bag", no "you step out of the car".
- You never say what the player thinks or feels, or how they react -- not even "you
  freeze" or "you stare". Show what is in front of them and let them react.
If someone must say the player's name aloud, write {{short}}; it is filled in with
whoever they chose to be.

Do this:
- Second person, addressed to the player as "you".
- Open in a concrete place at a concrete moment, grounded in the world details.
- Put one or two people on the page doing something and wanting something. Use names
  from the cast where they exist; if the cast is empty, you may name the one or two
  people this scene needs.
- AT LEAST TWO lines of dialogue get their own paragraph in the speaker form, with
  nothing else on the line. Not inline quotes -- the speaker form. For example, not this:

    "I need help," she says. "Lost my body somewhere in this park."

  but this:

    **Mara** | "I need help. Lost my body somewhere in this park."

  Ordinary narration around those lines stays ordinary prose.

- Describe the situation the player is in, then stop: end on an open beat, something
  just said or just happened, with nothing resolved and the next move theirs.
- Keep whatever they wrote: their events, their people, the order things happen in.

Never:
- A title, a heading, or a premise summary ("In a world where...").""",
    },

    "opening_scene": {
        "label": "Narrator's secret notes",
        # Read on every turn and inside the 11,000-character rules layer, so short.
        "length": (600, 400, 1000),
        "context": ("rules", "details", "cast", "prologue"),
        "guide": """These are the NARRATOR'S SECRET NOTES. The player never sees them. The
narrator reads them on every turn for the whole story -- unlike the first page, which on
a long story scrolls out of its view. Write what it must never forget about how this
began.

Do this:
- The situation at the moment the story starts: who is present, where, when.
- What has happened, and just as important, what has NOT happened yet.
- What the player does not know: the truth behind what they are seeing, what people
  want and are not saying. This is the main reason the field exists.
- Plain statements of fact, in a few short paragraphs.
- DECIDE THE ANSWERS. The player does not know them, but the narrator must. Do not
  list what is unknown ("the player does not know why...") -- say what is TRUE: why it
  happened, who is behind it, where things are, what someone is hiding. One answer each,
  stated as fact, never a menu of possibilities. If the first page raises a question,
  these notes answer it.
- No commentary on tone or how the story will feel. Only facts.

Never:
- Retell the first page. The narrator has already read it; these notes are what is
  underneath it.
- Instructions about tone or style. The rules hold those.
- Name the player, describe them, or give them a history. They choose who they are
  when they start; refer to them only as "the player".
- A new premise. Extend what the first page and world details already imply. If a
  hidden actor is needed and the cast has no one for it, describe them by role
  ("whoever took her body") rather than inventing a name.""",
    },
}


def _target(n: int, empty: int = 900, lo: int = 600, hi: int = 1500) -> int:
    """Length to aim for, relative to what they gave.

    One-and-a-half times their own text, so a single line is expanded and a dense
    paragraph is tightened rather than inflated -- the second version of this asked for
    a fixed paragraph count and turned 600 good characters into 4,400. Floored so a
    one-liner has room to become something, capped because this field is paid for on
    every turn and shares an 11,000-character ceiling with the rules and the opening.
    """
    if n <= 0:
        return empty
    return max(lo, min(hi, round(n * 1.5)))


_PREAMBLE = re.compile(
    r"^\s*(here('s| is| are)|sure|certainly|okay|ok|rewritten|revised|world details|"
    r"first page|prologue|secret notes|narrator's (secret )?notes|opening scene)\b[^\n]*\n+",
    re.IGNORECASE)


def _clean(text: str, stopped: str, keep_bold: bool = False) -> str:
    """Strip what a chat model wraps around an answer, and never return half a sentence."""
    t = text.strip()
    t = re.sub(r"^```[a-z]*\s*|\s*```$", "", t).strip()       # stray fences
    t = re.sub(r"^#{1,6}\s+[^\n]*\n+", "", t).strip()         # a markdown heading on top
    t = _PREAMBLE.sub("", t, count=1).strip()                  # "Here's the rewrite:"
    # Wrapped in quotes as a whole -- but only if those are the ONLY two. A first page
    # can legitimately open and close on a line of dialogue.
    if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" and t.count(t[0]) == 2:
        t = t[1:-1].strip()
    if not keep_bold:
        t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)               # bold it was told not to use
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    # Only if it ran into the reply ceiling. In prose that costs the tail of one
    # sentence, which is trimmed -- unlike JSON, where it cost the whole call.
    if stopped == "max_tokens":
        cut = max(t.rfind(". "), t.rfind(".\n"), t.rfind("! "), t.rfind("? "))
        if t.rstrip()[-1:] not in ".!?\"'" and cut > len(t) // 2:
            t = t[:cut + 1]
    return t.strip()


def improve(field: str, text: str, story: dict | None = None) -> str:
    """Rewrite one field. Returns the new text; writes nothing."""
    spec = FIELDS.get(field)
    if not spec:
        raise ValueError(f"no assist for '{field}'")

    story = story or {}
    wanted = spec.get("context", ("rules",))
    ctx = []
    if story.get("name"):
        ctx.append(f"The story is called {story['name']}.")
    if story.get("tagline"):
        ctx.append(f"Its tagline: {story['tagline']}")
    # The rules set register and content; nothing here may repeat or fight them.
    if "rules" in wanted and story.get("rules"):
        ctx.append("Its rules, which you must not repeat or contradict:\n"
                   + story["rules"].strip()[:3000])
    if "details" in wanted and story.get("details"):
        ctx.append("The world, as already written:\n" + story["details"].strip()[:3000])
    if "cast" in wanted:
        cast = [c for c in (story.get("cast") or []) if c.get("name")][:15]
        ctx.append("The cast: " + ("; ".join(
            c["name"] + (f" ({c['short']})" if c.get("short") else "") for c in cast)
            if cast else "none written yet."))
    if "prologue" in wanted and (story.get("prologue") or "").strip():
        ctx.append("The first page, which the player reads and these notes sit underneath:\n"
                   + story["prologue"].strip()[:3000])

    mine = (text or "").strip()
    # A placeholder is not input. "Test", "tbd" and "todo" were being treated as text to
    # rewrite; under fifteen characters, it is written from the context instead.
    if len(mine.replace(" ", "")) < 15:
        mine = ""
    empty, lo, hi = spec.get("length", (900, 600, 1500))
    target = _target(len(mine), empty, lo, hi)
    paras = max(2, min(6, round(target / 350)))
    system = "\n\n".join([
        _FRAME,
        f"THE FIELD: {spec['label']}",
        spec["guide"],
        f"LENGTH: about {target} characters, in {paras} short paragraphs separated by "
        f"blank lines.",
        "\n\n".join(ctx) if ctx else "(no other fields written yet)",
    ])
    ask = ("Here is what I have for this field:\n\n" + mine + "\n\nRewrite it.") if mine \
        else "I have not written this field yet. Write it from what the story already has."

    stop: list[str] = []
    # The story-turn spec, so it shares the loaded model's context size, with the
    # temperature lowered: this is a rewrite of their facts, not invention.
    out = brain.prose(system, [{"role": "user", "content": ask}],
                      spec={**config.PROSE, "temperature": 0.7},
                      on_stop=stop.append)
    return _clean(out, stop[-1] if stop else "stop", spec.get("keep_bold", False))
