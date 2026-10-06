"""Per-field help: say it roughly, get it back written for the engine.

The editor asks for several blocks of prose, each with its own job and its own
failure mode, and nothing on the screen says what a good one looks like. So each
field gets a button: write what you mean however you like, and this rewrites it into
the shape the narrator actually reads well.

It is deliberately per-field rather than one "write my story" button. Each field is a
different task with different rules -- `details` wants facts and no instructions,
`rules` wants instructions and no facts -- and a single prompt covering all of them
does each one worse.

What comes back REPLACES the field in an unsaved editor. Nothing here writes to disk.
"""
from __future__ import annotations

from typing import Any

import brain
import config

def _schema(lo: int, hi: int) -> dict[str, Any]:
    """Paragraphs as an ARRAY, joined afterwards -- not one string.

    Asking in the prompt for four to eight paragraphs separated by blank lines
    produced one block, twice, including when the instruction was in capitals. The
    schema is the only part of this the model reliably obeys: an array with minItems
    cannot come back as a single paragraph. Same lesson as requiring lorebook entries
    rather than asking for them.
    """
    return {
        "type": "object",
        "properties": {
            "paragraphs": {"type": "array", "minItems": lo, "maxItems": hi,
                           "items": {"type": "string"}},
        },
        "required": ["paragraphs"],
    }

# Shared by every field, because the most common failure is a block that repeats what
# the engine already says or what another field already covers.
_FRAME = """You are helping someone write one field of an interactive-fiction story
file. Rewrite what they have into something the narrator will read well. Keep their
intent, their names and their specifics exactly; improve the shape, not the substance.

The engine already instructs the narrator separately on player agency, input formats,
clean prose, scene pacing, turn length and the attributed-dialogue format. Never write
any of that here.

Return only the field's new contents. No preamble, no explanation, no markdown fences."""

FIELDS: dict[str, dict] = {
    "details": {
        "label": "World details",
        # Four to six, not eight. At eight it returned 4,345 characters, and
        # `rules` + `details` + the opening scene share an 11,000-character ceiling
        # that a story with substantial rules would then overrun.
        "paragraphs": (4, 6),
        "guide": """This field is WHAT IS TRUE about the world. Facts the narrator needs
in order not to contradict itself: the place, the era, how things work, who holds
power, what is normal here and what is not. It is read on every single turn.

It is NOT instructions to the narrator -- no "be funny", no "keep scenes short". Those
belong in the rules and will be ignored or will fight with them here.

Make it concrete. "A port town" is worth nothing; "a port town of four thousand, two
canneries, one of them closed since the spring" is worth a great deal, because every
one of those details is something a scene can be built from. Prefer specifics that
generate scenes over adjectives that describe a mood.

DO NOT INVENT NAMED PEOPLE. The cast is its own field and inventing names here
creates characters the story does not know about, which the narrator will then
contradict. Describe roles and groups -- "three boatbuilders", "the harbourmaster" --
never "Marcus, who has been doing it since he was sixteen".

Each paragraph is a separate item, three to five sentences, on one aspect of the
world. 800-2,000 characters across all of them. If they gave you two sentences,
expand by adding the concrete consequences of what they said, never by padding.

Plain prose. No headings, no bullet lists.""",
    },
}


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
    # The rules are the single most useful context: they establish register and
    # content, and this field must not contradict or repeat them.
    if story.get("rules"):
        ctx.append("Its rules, which you must not repeat or contradict:\n"
                   + story["rules"].strip()[:3000])

    prompt = "\n\n".join([
        _FRAME,
        f"THE FIELD: {spec['label']}",
        spec["guide"],
        "\n".join(ctx) if ctx else "(no other fields written yet)",
        "WHAT THEY HAVE WRITTEN SO FAR:\n" + (text.strip() or "(empty -- write it from "
                                              "the story's name, tagline and rules)"),
    ])
    lo, hi = spec.get("paragraphs", (3, 8))
    out = brain.utility(prompt, _schema(lo, hi),
                        spec={**config.UTILITY, "max_tokens": 1500}, timeout=300)
    paras = [str(x).strip() for x in (out.get("paragraphs") or []) if str(x).strip()]
    return "\n\n".join(paras)
