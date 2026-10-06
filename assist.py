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


# ---------------------------------------------------------------- keyword notes
#
# Not a field rewrite: this proposes whole notes, which the author accepts or
# dismisses one at a time. Still generated as prose, in a plain delimited format,
# for the same reason the fields are -- a JSON body string runs until a bound stops
# it. Bodies are short and the count is fixed, so this ends on its own.

_KW_FRAME = """You are helping someone build the lorebook for an interactive-fiction story.

A keyword note is reference material for the narrator. It enters the narrator's context
only on turns where one of its triggers appears in the recent text, so it costs nothing
when it is not needed. This is where the bulk of a world belongs: people, places, groups,
kinds of creature, institutions, customs, how something works.

Each note:
- Title: the thing's name, 1-4 words, spelled exactly as the story spells it.
- Triggers: 2-4 lowercase words or phrases that would appear in prose when this thing
  comes up. Include the name. Triggers are matched as plain text INSIDE other words, so
  avoid short ones that hide in common words ("orc" fires on "force" and "torch" --
  write "orcs", "orcish"), and never generic ones like "man", "city", "house", "money".
- Body: 2-5 sentences of plain fact, present tense, written for the narrator. Who or
  what it is, what it is like, what it wants or how it works. No instructions about
  tone, no plot, nothing that has to happen.

Reply with the notes only, each in exactly this form, nothing before or after:

=== Title
from: story
triggers: first, second, third
The body, as plain sentences.

"from" is "story" when the note organises what the author has already written, and
"new" when it is your own addition."""

_KW_FIRST = """Go through everything the author has written and pull out what deserves its
own note: every named person, place, group, kind of creature, institution or custom
that the narrator will need to keep straight. Each of these notes restates and gathers
what the author wrote about it -- from wherever it appears -- and fills a gap only where
it plainly follows from what they wrote. Mark these "from: story". At most {n_story}.

Then add {n_new} notes of your own that extend the world in the direction it already
points: a place these people would go, a custom or institution this world would have.
Mark these "from: new". New notes may name places and institutions, but do not invent
new named people."""

_KW_MORE = """Propose {n_new} NEW notes that extend the world in the direction it already
points: places, institutions, customs, kinds of creature, how things work here. Each
must fit everything already written and must not repeat or overlap any existing note
below. Mark them all "from: new". Do not invent new named people."""


def _kw_context(story: dict, have: list[dict], seen: list[str]) -> str:
    parts = []
    if story.get("name"):
        parts.append(f"The story is called {story['name']}.")
    if story.get("rules"):
        parts.append("Its rules:\n" + story["rules"].strip()[:3000])
    if story.get("details"):
        parts.append("The world:\n" + story["details"].strip()[:3000])
    for it in (story.get("intros") or [])[:4]:
        for key, label in (("prologue", "An opening scene"),
                           ("opening_scene", "The narrator's notes on that opening")):
            if (it.get(key) or "").strip():
                parts.append(f"{label}:\n" + it[key].strip()[:2500])
    cast = [c.get("name") for c in (story.get("cast") or []) if c.get("name")]
    if cast:
        parts.append("The cast: " + ", ".join(cast[:20]))
    if have:
        parts.append("Notes that ALREADY EXIST -- do not repeat or overlap these:\n" + "\n".join(
            f"- {k.get('title')} ({', '.join(k.get('keywords') or [])}): "
            + (k.get("body") or "").strip().replace("\n", " ")[:160]
            for k in have[:40]))
    if seen:
        parts.append("Already offered to the author, do not offer again: " + ", ".join(seen[:60]))
    return "\n\n".join(parts)


_KW_SPLIT = re.compile(r"^\s*={3,}\s*", re.M)

_ARTICLE = re.compile(r"^(the|a|an|her|his|their|its|my|your|our)\s+", re.I)
# Words that name a kind of thing rather than this thing. "Pinecrest Park" may trigger
# on "pinecrest" but not on "park", or it fires whenever anyone mentions any park.
_GENERIC = {
    "park", "bar", "pub", "tavern", "street", "road", "avenue", "hall", "house", "home",
    "office", "shop", "store", "school", "club", "city", "town", "suburb", "district",
    "registry", "bank", "banks", "company", "guild", "group", "family", "woman", "man",
    "girl", "boy", "horse", "dog", "cat", "creature", "creatures", "people", "service",
    "lost", "found", "headless", "black", "white", "old", "new", "little", "big",
}


def _triggers(raw: list[str], title: str) -> list[str]:
    """Triggers that name THIS thing. The model is told not to write "the bar" or
    "the woman" and writes them anyway; matching is plain substring, so each one would
    fire the note on nearly every turn. A trigger is kept only if it shares a
    distinctive word with the title, and a three-letter one is dropped because it
    fires inside ordinary words ("orc" in "force")."""
    own = {w for w in re.findall(r"[a-z0-9']+", title.lower()) if w not in _GENERIC and len(w) >= 4}
    out = []
    for t in raw:
        t = _ARTICLE.sub("", " ".join(t.split()).lower().strip(" .\"'"))
        words = set(re.findall(r"[a-z0-9']+", t))
        # A plural or adjective of a title word counts: "dullahan" for "Dullahans",
        # "orcish" for "Orcs".
        mine = any(w.startswith(o.rstrip("s")) or o.startswith(w) for w in words for o in own)
        # A phrase of two or more real words is specific on its own ("lost and found"),
        # which matters when every word of the title is generic.
        phrase = len([w for w in words if len(w) >= 3]) >= 2
        if len(t) >= 4 and (mine or phrase) and t not in out:
            out.append(t)
    return out


def _parse_notes(text: str) -> list[dict]:
    import lorebook  # its trigger cleaning: drops generic and stop-word triggers
    out = []
    for chunk in _KW_SPLIT.split(text)[1:]:
        lines = chunk.strip().splitlines()
        if not lines:
            continue
        title = " ".join(lines[0].strip(" *#=").split())
        src, trig, body = "new", [], []
        for ln in lines[1:]:
            low = ln.strip().lower()
            if low.startswith("from:") and not body:
                src = "story" if "story" in low else "new"
            elif low.startswith("triggers:") and not body:
                trig = [t for t in ln.split(":", 1)[1].split(",")]
            else:
                body.append(ln)
        body_text = re.sub(r"\*\*([^*]+)\*\*", r"\1", "\n".join(body)).strip()
        if not title or not body_text:
            continue
        out.append({"title": title,
                    "keywords": lorebook._clean_keywords(_triggers(trig, title),
                                                         _ARTICLE.sub("", title)),
                    "body": body_text, "from": src})
    return out


def keyword_notes(story: dict, mode: str = "first", seen: list | None = None) -> list[dict]:
    """Proposed keyword notes. "first" pulls notes out of what is written and adds a
    couple of new ones; "more" only extends. Nothing is saved here."""
    story = story or {}
    have = [k for k in (story.get("keywords") or []) if isinstance(k, dict)]
    seen = [str(s) for s in (seen or [])]
    ask = (_KW_FIRST.format(n_story=6, n_new=2) if mode == "first"
           else _KW_MORE.format(n_new=4))
    system = _KW_FRAME + "\n\n" + _kw_context(story, have, seen)
    stop: list[str] = []
    out = brain.prose(system, [{"role": "user", "content": ask}],
                      spec={**config.PROSE, "temperature": 0.7 if mode == "first" else 0.85},
                      on_stop=stop.append)
    taken = {k.get("title", "").lower() for k in have} | {s.lower() for s in seen}
    notes = [n for n in _parse_notes(out) if n["title"].lower() not in taken]
    # A note cut off by the reply ceiling ends mid-sentence; drop it rather than offer it.
    if stop and stop[-1] == "max_tokens" and notes:
        notes.pop()
    return notes


# ---------------------------------------------------------------- cast
#
# Same shape as keyword notes: proposals as cards, accepted one at a time. A cast
# entry is deliberately thin -- name, aliases, how narration refers to them, portrait
# tags -- because who a person IS lives in a keyword note. So each proposal also
# carries a note body, which the editor offers to add when no note covers them yet.

_CAST_FRAME = """You are helping someone build the cast list for an interactive-fiction story.

The player is never in the cast. They choose who they are when they start; never
propose them, and never give anyone a relationship that defines who the player is.

First write a plan, one numbered line per person: gender, what they are, their role.
Then the entries, in the same order, each in exactly this form:

PLAN:
1. female, fairy, nurse at the clinic
2. male, orc, bartender

=== Full Name
from: story
kind: what they are -- human, orc, fairy, and so on
aka: First name, nickname
seen as: how narration describes them before the player learns their name
portrait: comma-separated image tags
note: 2-4 sentences for the narrator: who they are, what they do, what they want, how
they behave. Plain fact, present tense.

- ONE PERSON PER ENTRY. A group is never an entry; its members are, each on their own.
- "from" is "story" for someone the author already wrote, "new" for someone you made.
- "aka": the names people would actually call them -- first name, surname, a nickname.
- "seen as": under twelve words, visual, e.g. "the floating head with the sharp tongue".
- "portrait": booru-style tags for an image generator, short tags only, never phrases or
  sentences. The first tag is exactly one of 1girl (any woman or girl, of any age), 1boy
  (any man or boy, of any age) or 1other (neither) --
  then solo, then whatever makes their body unusual -- this comes BEFORE everything
  else, because an image model draws an ordinary human otherwise: "disembodied head,
  floating head, no body", "green skin, goblin, pointed ears", "dragon horns, scales".
  Their kind is always one of the tags ("orc", "fairy", "goblin", "human"). Then age,
  build, skin, hair, eyes, expression, clothing. No quality tags and no art-style
  tags; those are added for them.
- For someone the author wrote, keep every fact they gave and contradict nothing, and
  NEVER invent a name for them: no first name, no surname, no nickname the author did
  not write. "aka" holds only names that appear in the author's text."""

_CAST_STORY = """List every individual person the author has already NAMED -- in the rules,
the world, the openings and the keyword notes -- who is not in the cast yet. Mark them
"from: story". At most 8.

A person with their own keyword note still needs a cast entry: the note is what the
narrator knows about them, the cast entry is their name and portrait. Start with whoever
the story is most about.

Include people mentioned only in passing inside another note -- a librarian named in a
library's note, a bartender named in a bar's note. Read every note for them.

Skip unnamed groups ("three banshee sisters") and anyone the author did not name. If
there is no one, reply with nothing."""

_CAST_ASK = """The author wants these characters:

{want}

Create exactly what they asked for: their counts, genders, kinds and roles. Your plan
must match their request line for line -- count the men and the women before you
write a single entry. Where they
gave a name, use it; where they did not, choose one that fits the world. Make each
person belong here -- a job, a place and a want that follow from the world as written.

Every one of them is someone NEW. People the story already has are not part of this
request; do not include them.

Choose what each person IS from the kinds of people this world actually has, following
its own patterns -- if the world says who holds which jobs, a person in that job is
that kind. Do not default to human unless they asked for one or the world is human.
Mark them "from: new"."""

_CAST_FILL = """Propose 3 people this story will need, who are not in it yet: people the
player would plausibly meet in the opening situation and the world around it. Choose what
each person IS from the kinds of people this world has, following its own patterns; do
not default to human. Mark them "from: new"."""


def _cast_context(story: dict, seen: list[str]) -> str:
    parts = [_kw_context({k: v for k, v in story.items() if k not in ("keywords", "cast")}, [], [])]
    cast = [c for c in (story.get("cast") or []) if c.get("name")]
    if cast:
        parts.append("ALREADY IN THE CAST -- do not propose these again:\n" + "\n".join(
            f"- {c['name']}" + (f" ({c['short']})" if c.get("short") else "") for c in cast[:30]))
    notes = [k for k in (story.get("keywords") or []) if isinstance(k, dict)]
    if notes:
        # Fuller than for keyword suggestions: this is where people are described.
        parts.append("The story's keyword notes:\n" + "\n".join(
            f"- {k.get('title')}: " + (k.get("body") or "").strip().replace("\n", " ")[:600]
            for k in notes[:30]))
    if seen:
        parts.append("Already offered to the author, do not offer again: " + ", ".join(seen[:60]))
    return "\n\n".join(p for p in parts if p)


_HONORIFIC = {"mr", "mrs", "ms", "miss", "dr", "doctor", "sir", "lady", "lord", "the"}


def _name_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z']+", name.lower()) if len(w) >= 3 and w not in _HONORIFIC]


def _story_names(story: dict) -> set[str]:
    """Capitalised words in everything the author wrote -- the names already in use."""
    text = " ".join([story.get("rules") or "", story.get("details") or ""]
                    + [(it.get("prologue") or "") + " " + (it.get("opening_scene") or "")
                       for it in story.get("intros") or []]
                    + [(k.get("title") or "") + " " + (k.get("body") or "")
                       for k in story.get("keywords") or [] if isinstance(k, dict)]
                    + [c.get("name") or "" for c in story.get("cast") or []])
    return {w.lower() for w in re.findall(r"\b[A-Z][a-z']{2,}\b", text)}


def _covered(name: str, notes: list[dict]) -> bool:
    """Does an existing keyword note already describe this person?"""
    words = {w for w in re.findall(r"[a-z']+", name.lower()) if len(w) >= 3}
    for k in notes:
        hay = (k.get("title") or "").lower() + " " + " ".join(k.get("keywords") or [])
        if any(re.search(rf"\b{re.escape(w)}\b", hay) for w in words):
            return True
    return False


def _parse_cast(text: str, notes: list[dict]) -> list[dict]:
    import lorebook
    out = []
    for chunk in _KW_SPLIT.split(text)[1:]:
        lines = chunk.strip().splitlines()
        if not lines:
            continue
        name = " ".join(lines[0].strip(" *#=").split())
        f = {"from": "new", "kind": "", "aka": "", "seen as": "", "portrait": "", "note": ""}
        cur = None
        for ln in lines[1:]:
            m = re.match(r"\s*(from|kind|aka|seen as|portrait|note)\s*:\s*(.*)$", ln, re.I)
            if m:
                cur = m.group(1).lower()
                f[cur] = m.group(2).strip()
            elif cur in ("note", "portrait") and ln.strip():
                f[cur] += " " + ln.strip()      # a wrapped tag list or note
        if not name or not f["portrait"]:
            continue
        aliases = [a.strip() for a in f["aka"].split(",")
                   if a.strip() and a.strip().lower() != name.lower()
                   and len(a.split()) <= 3      # a name, not a sentence about names
                   and not re.match(r"(none|n/?a|unknown|not given|none given|-)\b", a.strip(), re.I)][:4]
        # The first tag is the image model's subject count. "1woman" is not one it knows.
        prompt = re.sub(r"\s+", " ", f["portrait"]).strip().rstrip(",")
        prompt = re.sub(r"^\s*1\s*(woman|female|lady)\b", "1girl", prompt, flags=re.I)
        prompt = re.sub(r"^\s*1\s*(man|male|guy)\b", "1boy", prompt, flags=re.I)
        # Without its kind as a tag an orc is drawn as a man with green skin.
        kind = f["kind"].strip().strip(".").lower()
        if kind and len(kind.split()) <= 3 and kind not in prompt.lower():
            head, _, rest = prompt.partition(",")
            if rest and re.match(r"\s*solo\b", rest):
                _, _, rest = rest.partition(",")
                head += ", solo"
            prompt = f"{head}, {kind}," + rest if rest else f"{head}, {kind}"
        trig = [name.lower()] + [a.lower() for a in aliases if len(a) >= 4]
        out.append({
            "name": name,
            "short": f["seen as"].strip().rstrip("."),
            "aliases": aliases,
            "prompt": prompt,
            "note": re.sub(r"\*\*([^*]+)\*\*", r"\1", f["note"]).strip(),
            "keywords": lorebook._clean_keywords(trig, name),
            "from": "story" if "story" in f["from"].lower() else "new",
            "kind": f["kind"].strip().strip("."),
            "has_note": _covered(name, notes),
        })
    return out


def cast_members(story: dict, mode: str = "story", want: str = "",
                 seen: list | None = None) -> list[dict]:
    """Proposed cast. "story" lists people already written but not cast; "ask" makes
    who the author described, or a few the story needs if they described no one."""
    story = story or {}
    seen = [str(s) for s in (seen or [])]
    want = (want or "").strip()[:1500]
    if mode == "story":
        ask = _CAST_STORY
    elif want:
        ask = _CAST_ASK.format(want=want)
    else:
        ask = _CAST_FILL
    system = _CAST_FRAME + "\n\n" + _cast_context(story, seen)
    stop: list[str] = []
    out = brain.prose(system, [{"role": "user", "content": ask}],
                      spec={**config.PROSE, "temperature": 0.7 if mode == "story" else 0.85},
                      on_stop=stop.append)
    notes = [k for k in (story.get("keywords") or []) if isinstance(k, dict)]
    taken = {c.get("name", "").lower() for c in (story.get("cast") or [])} | {s.lower() for s in seen}
    people = [c for c in _parse_cast(out, notes) if c["name"].lower() not in taken]
    if mode != "story":
        # Asked for new people; one the model pulled back in from the story is not that,
        # and comes with whatever surname it gave them. Nor is one it relabelled "new"
        # under a borrowed name -- "Dr. Sarah Chen" for the story's Mrs. Chen -- so a
        # new person sharing a name with anyone already written is dropped, unless the
        # author typed that name in their request.
        written = _story_names(story)
        asked = want.lower()
        people = [c for c in people if c["from"] == "new" and not any(
            w in written and w not in asked for w in _name_words(c["name"]))]
    if stop and stop[-1] == "max_tokens" and people:
        people.pop()
    return people
