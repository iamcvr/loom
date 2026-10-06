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
}


def _target(n: int) -> int:
    """Length to aim for, relative to what they gave.

    One-and-a-half times their own text, so a single line is expanded and a dense
    paragraph is tightened rather than inflated -- the second version of this asked for
    a fixed paragraph count and turned 600 good characters into 4,400. Floored so a
    one-liner has room to become something, capped because this field is paid for on
    every turn and shares an 11,000-character ceiling with the rules and the opening.
    """
    if n <= 0:
        return 900
    return max(600, min(1500, round(n * 1.5)))


_PREAMBLE = re.compile(
    r"^\s*(here('s| is| are)|sure|certainly|okay|ok|rewritten|revised|world details)\b[^\n]*\n+",
    re.IGNORECASE)


def _clean(text: str, stopped: str) -> str:
    """Strip what a chat model wraps around an answer, and never return half a sentence."""
    t = text.strip()
    t = re.sub(r"^```[a-z]*\s*|\s*```$", "", t).strip()       # stray fences
    t = _PREAMBLE.sub("", t, count=1).strip()                  # "Here's the rewrite:"
    if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'":         # quoted whole
        t = t[1:-1].strip()
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)                   # bold it was told not to use
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
    ctx = []
    if story.get("name"):
        ctx.append(f"The story is called {story['name']}.")
    if story.get("tagline"):
        ctx.append(f"Its tagline: {story['tagline']}")
    # The rules set register and content, and this field must not repeat or fight them.
    if story.get("rules"):
        ctx.append("Its rules, which you must not repeat or contradict:\n"
                   + story["rules"].strip()[:3000])

    mine = (text or "").strip()
    target = _target(len(mine))
    paras = max(2, min(5, round(target / 350)))
    system = "\n\n".join([
        _FRAME,
        f"THE FIELD: {spec['label']}",
        spec["guide"],
        f"LENGTH: about {target} characters, in {paras} short paragraphs separated by "
        f"blank lines.",
        "\n".join(ctx) if ctx else "(no other fields written yet)",
    ])
    ask = ("Here is what I have for this field:\n\n" + mine + "\n\nRewrite it.") if mine \
        else "I have not written this field yet. Write it from the story's name, tagline and rules."

    stop: list[str] = []
    # The story-turn spec, so it shares the loaded model's context size, with the
    # temperature lowered: this is a rewrite of their facts, not invention.
    out = brain.prose(system, [{"role": "user", "content": ask}],
                      spec={**config.PROSE, "temperature": 0.7},
                      on_stop=stop.append)
    return _clean(out, stop[-1] if stop else "stop")
