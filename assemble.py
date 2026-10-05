"""The context budget arbiter.

This is the part that makes the whole thing work, and the part SillyTavern
structurally cannot do: ONE system decides, every turn, what earns a slot in the
context window.

Model
-----
Each layer produces an ordered list of items, most important first. The arbiter
walks layers in priority order and takes the longest prefix of each that fits its
allocation. Trimming a layer therefore means dropping its least important items,
never truncating text mid-sentence.

Allocation
----------
1. Reserve every layer's floor (or its real size, whichever is smaller).
2. Walk layers in priority order, growing each from its floor toward
   min(ceiling, real size) out of whatever budget remains.
3. Anything still unspent is left on the table — transcript's ceiling is the
   natural sink and it is last in priority, so it absorbs slack in practice.

Every number comes from config.BUDGET_* — there are no constants here.
"""
from __future__ import annotations

from typing import Any, Optional

import config
import store
from story import cast_member  # noqa: F401  (re-exported for callers)
from story import protagonist_defaults, subst, tokens as _pro_tokens


# ---------------------------------------------------------------- layer items


def _items_director(session_id: int) -> list[str]:
    text = store.notes(session_id).strip()
    return [text] if text else []


def protagonist(session_id: int, story: dict) -> Optional[dict]:
    """Who the player is this session: their own record, or the story's default.

    The fallback matters for sessions that predate a story gaining a protagonist
    block — they get the declared default rather than an empty identity.
    """
    return store.protagonist(session_id) or protagonist_defaults(story)


def _items_protagonist(pro: Optional[dict]) -> list[str]:
    """The player's own character sheet.

    Its own layer rather than a paragraph inside `rules` because it is the one
    part of the prompt the player wrote themselves. Sharing the rules allowance
    would mean a long story bible could push a player's own character
    description out of the prompt, which is the worst possible thing to trim.
    """
    if not pro or not (pro.get("name") or pro.get("description")):
        return []

    head = f"## Who you are\n\nYou are {pro['name'].strip()}"
    if pro.get("short") and pro["short"].strip() != pro["name"].strip():
        head += f", called {pro['short'].strip()}"
    if pro.get("pronouns"):
        head += f" ({pro['pronouns'].strip()})"
    head += "."
    if pro.get("description", "").strip():
        head += "\n\n" + pro["description"].strip()

    out = [head]
    if pro.get("power", "").strip() or pro.get("power_name", "").strip():
        label = (pro.get("power_label") or "Ability").strip()
        title = (pro.get("power_name") or "").strip()
        block = f"### Your {label}" + (f" — {title}" if title else "")

        # One item per paragraph, not one blob. As a single item the power was
        # all-or-nothing, and a description longer than the allowance vanished
        # entirely rather than losing its tail — the same failure the threads
        # layer had. The heading rides with the first paragraph so it can never
        # be left standing on its own.
        paras = [x.strip() for x in pro.get("power", "").split("\n\n") if x.strip()]
        if paras:
            out.append(block + "\n\n" + paras[0])
            out.extend(paras[1:])
        else:
            out.append(block)
    return out


def _items_rules(story: dict, intro: dict) -> list[str]:
    out = [story["rules"].strip()]
    if story.get("details"):
        out.append("## The world\n\n" + story["details"].strip())
    if intro.get("opening_scene"):
        out.append("## How this began\n\n" + intro["opening_scene"].strip())
    return out


def current_act(session_id: int, story: dict) -> Optional[dict]:
    """Which act this session is in, or None for a story without an arc.

    Chapters are the clock. They close every CHAPTER_EVERY_TURNS turns, which
    makes act length a predictable number of turns without the author having to
    reason about the budget. An act may span several chapters via its own
    `chapters:` span.

    A manual override is stored per session and wins outright, because the reader
    is the one who can tell that a beat has actually landed — the counter only
    knows that time has passed.
    """
    arc = story.get("arc") or {}
    acts = arc.get("acts") or []
    if not acts:
        return None

    override = store.get_meta(f"arc:{session_id}", None)
    index: Optional[int] = None
    if override is not None:
        by_id = {a["id"]: i for i, a in enumerate(acts)}
        if isinstance(override, str) and override in by_id:
            index = by_id[override]
        elif isinstance(override, int):
            index = override

    if index is None:
        if arc.get("advance") == "manual":
            index = 0
        else:
            chapter = int(store.open_chapter(session_id).get("number") or 1)
            index, seen = len(acts) - 1, 0
            for i, a in enumerate(acts):
                seen += int(a.get("chapters") or 1)
                if chapter <= seen:
                    index = i
                    break

    index = max(0, min(len(acts) - 1, index))
    return {
        **acts[index],
        "index": index,
        "number": index + 1,
        "total": len(acts),
        "final": index == len(acts) - 1,
        "manual": override is not None,
    }


def _items_arc(act: Optional[dict]) -> list[str]:
    """The current act, and the fact that there is a last one.

    Position is the whole point. The model is handed a transcript with no turn
    numbers and no sense of how much story is left, so "build toward an ending"
    is a measurement it cannot make. Naming the act and its number out of the
    total is what lets it pace, and telling it plainly when it is in the final
    act is what lets it land rather than continue.
    """
    if not act or not act.get("shape", "").strip():
        return []

    head = f"## The story's shape — act {act['number']} of {act['total']}: {act['name']}"
    if act.get("final"):
        head += ("\n\nThis is the LAST act. The story ends inside it. Bring things to "
                 "rest rather than opening anything new.")

    # One item per paragraph so a long act degrades by losing its tail rather
    # than vanishing whole — the same reason the protagonist layer is split.
    paras = [x.strip() for x in act["shape"].split("\n\n") if x.strip()]
    out = [head + "\n\n" + paras[0]] if paras else [head]
    out.extend(paras[1:])
    return out


def _items_state(session_id: int, story: dict) -> list[str]:
    out: list[str] = []

    vals = store.stats(session_id)
    if vals:
        lines = []
        for sd in story.get("stats", []):
            if sd["key"] in vals:
                unit = f" {sd['unit']}" if sd["unit"] else ""
                lines.append(f"- {sd['name']}: {vals[sd['key']]:g}{unit}")
        if lines:
            out.append("## Current state\n\n" + "\n".join(lines))

    rels = store.relationships(session_id)
    if rels:
        lines = [f"- {r['name']}: {r['summary']}" for r in rels]
        # A narrative story has no "you" to be seen by anyone.
        head = ("## Where everyone stands with each other"
                if story.get("mode") == "narrative" else "## How people see you")
        out.append(head + "\n\n" + "\n".join(lines))

    return out


def _items_goals(session_id: int) -> list[str]:
    """The objective, singular by default.

    Phrased as one thing rather than a list on purpose. Given a bulleted list the
    model tries to service every item in the same scene; given one sentence it
    treats it as the thing the story is currently about.
    """
    active = store.goals(session_id, ["active"])
    if not active:
        return []
    if len(active) == 1:
        return ["## What is outstanding\n\n" + active[0]["text"]]
    return ["## What is outstanding\n\n" + "\n".join(f"- {g['text']}" for g in active)]


def _items_long_memory(retrieved: list[dict]) -> list[str]:
    if not retrieved:
        return []
    return ["## What you remember\n\n" + "\n".join(f"- {m['text']}" for m in retrieved)]


def _items_temp_memory(hot: list[dict]) -> list[str]:
    """One item per memory so the arbiter can drop the coldest individually."""
    if not hot:
        return []
    return [f"- {m['text']}" for m in hot]


def _items_nudge(session_id: int, story: dict, turn: int) -> list[str]:
    """Pacing, timed by a counter rather than asked for in prose.

    Two nudges live here and they exist for the same structural reason: the model
    is handed the current chapter's transcript with no turn numbers on it, so it
    cannot tell how long it has been since anything — since the objective last
    came up, or since the last fight. Any instruction phrased as a frequency
    ("mention the goal occasionally", "combat should be common") delegates a
    measurement the model has no way to make. So the harness counts, and the
    prompt only says what to do once the count is up.

    This layer replaced a standing list of unresolved plot threads. That list was
    permanent by design and the model read it as a set of ends to tie together,
    so every scene reached for all of them at once. What is here now is the
    opposite shape: nothing at all most turns, and one specific ask when it fires.
    """
    from memory import action_level, action_nudge, goal_nudge  # local: siblings

    # Only the SOFT action nudge belongs here. Once a fight is properly overdue it
    # goes through the depth channel instead, because system-prompt text does not
    # win against the transcript — see build().
    out = []
    if action_level(story, session_id, turn) == "soft":
        out.append(action_nudge(story, session_id, turn))
    goal = goal_nudge(session_id, turn)
    if goal:
        out.append(goal)
    return out


def _items_chapters(session_id: int) -> list[str]:
    """The story so far: a curated synopsis, then recent chapters verbatim.

    Ordered by PRIORITY, not chronology, because _fit keeps the prefix and drops
    the tail: synopsis first (it is short and covers everything old), then the
    most recent chapter, then backwards. Listing them chronologically meant a
    squeeze discarded the newest summary — the one covering the turns that had
    just fallen out of the transcript — while keeping a chapter from hours ago.
    Each block is headed with its number, so reverse order still reads clearly.
    """
    out: list[str] = []
    syn = store.synopsis(session_id)
    if syn.get("text"):
        out.append("## The story so far\n\n" + syn["text"].strip())

    closed = [c for c in store.chapters(session_id, closed_only=True) if c["summary"].strip()]
    for ch in reversed(closed[-config.CHAPTER_VERBATIM:]):
        head = f"### Chapter {ch['number']}"
        if ch["title"]:
            head += f" — {ch['title']}"
        out.append(head + "\n" + ch["summary"].strip())
    return out


def _items_keyword_notes(notes: list[dict]) -> list[str]:
    return [f"### {n['title']}\n{n['body'].strip()}" for n in notes]


# ---------------------------------------------------------------- allocation


def _fit(items: list[str], allowance: int, joiner: str = "\n\n",
         *, min_items: int = 0) -> tuple[str, int, int]:
    """Longest prefix of `items` fitting in `allowance`. Returns (text, used, dropped).

    `min_items` is kept regardless of the allowance, and it exists because
    "longest prefix that fits" degrades to NOTHING the moment the first item is
    bigger than the allowance on its own. For most layers that is harmless — the
    unspent budget carries downstream. The transcript is the last layer, so
    nothing catches what it drops, and a single long reply was enough: with the
    budget squeezed to 8,819 characters and the newest message at 20,082, every
    message in the packet was discarded and the model was handed a system prompt
    and one instruction. Asked to resume a reply it could not see, it invented a
    different scene, and the two halves were stored as one turn.

    Overrunning the allowance to keep the newest message is the lesser evil: the
    budget is a cost control, and the text immediately before the generation
    point is the single most load-bearing thing in the prompt."""
    if not items:
        return "", 0, 0
    kept: list[str] = []
    used = 0
    for it in items:
        cost = len(it) + (len(joiner) if kept else 0)
        if used + cost > allowance and len(kept) >= min_items:
            break
        kept.append(it)
        used += cost
    return joiner.join(kept), used, len(items) - len(kept)


def allocate(layers: dict[str, list[str]]) -> dict[str, Any]:
    """Split config.BUDGET_TOTAL across layers in priority order."""
    spec = {name: (floor, ceil) for name, floor, ceil in config.BUDGET_LAYERS}
    order = [name for name, _, _ in config.BUDGET_LAYERS]

    sizes = {
        n: (sum(len(i) for i in layers.get(n, [])) + max(0, len(layers.get(n, [])) - 1) * 2)
        for n in order
    }

    # 1. floors
    alloc = {n: min(spec[n][0], sizes[n]) for n in order}
    spent = sum(alloc.values())
    budget = config.BUDGET_TOTAL
    floors_unmet = spent > budget

    # 2. grow in priority order
    for n in order:
        if spent >= budget:
            break
        want = min(spec[n][1], sizes[n]) - alloc[n]
        if want <= 0:
            continue
        give = min(want, budget - spent)
        alloc[n] += give
        spent += give

    # 3. realise each layer, carrying unused allowance downstream.
    #    A layer whose smallest item doesn't fit would otherwise sit on budget it
    #    cannot spend while a lower-priority layer starves. Transcript is last, so
    #    it absorbs whatever nobody above it could use.
    report: list[dict] = []
    text: dict[str, str] = {}
    carry = 0
    for n in order:
        joiner = "\n" if n == "temp_memory" else "\n\n"
        allowance = alloc[n] + carry
        # The transcript never comes back empty. See _fit's min_items.
        body, used, dropped = _fit(layers.get(n, []), allowance, joiner,
                                   min_items=1 if n == "transcript" else 0)
        # An over-allowance keep must not hand a negative carry to the next
        # layer. Transcript is last today, so this is guarding the ordering
        # rather than a live path.
        carry = max(0, allowance - used)
        text[n] = body
        report.append(
            {
                "layer": n,
                "items": len(layers.get(n, [])),
                "dropped": dropped,
                "used": used,
                "allocated": allowance,
                "floor": spec[n][0],
                "ceiling": spec[n][1],
                "wanted": sizes[n],
            }
        )

    total_used = sum(r["used"] for r in report)
    return {
        "text": text,
        "layers": report,
        "used": total_used,
        "budget": budget,
        "fill": round(total_used / budget, 3) if budget else 0.0,
        "floors_unmet": floors_unmet,
        "advice": _advice(report, total_used, budget, floors_unmet),
    }


def _advice(report: list[dict], used: int, budget: int, floors_unmet: bool) -> str:
    if floors_unmet:
        return (
            "Layer floors exceed the total budget — raise the total or lower a floor "
            "under Settings → Context budget."
        )
    # Which layer is starving changes the answer entirely, and the old advice gave
    # the transcript's answer for all of them: it told you to compact a chapter
    # when the layer being cut was `rules`, where compaction does nothing at all.
    starved = [r["layer"] for r in report if r["dropped"] and r["layer"] != "temp_memory"]
    fixed = [n for n in starved if n != "transcript"]
    if fixed:
        return ("Dropping content from: " + ", ".join(fixed)
                + ". This is a ceiling, not a squeeze — raise it under Settings → "
                  "Context budget, or shorten the layer's content. It will be cut "
                  "on every turn until you do.")
    if "transcript" in starved:
        return ("The transcript is being trimmed — the oldest turns are leaving the "
                "prompt. This is normal; compacting the chapter converts them to a "
                "summary rather than losing them.")
    if used / budget > 0.9 if budget else False:
        return "Context is nearly full. A compact will keep replies sharp."
    return "Healthy."


# ---------------------------------------------------------------- assembly


_SECTION_TITLES = {
    "rules": None,             # already carries its own headings
    "protagonist": None,       # carries its own heading
    "arc": None,               # carries its own heading
    "chapters": "## Previously",
    "nudge": None,             # carries its own heading
    "keyword_notes": "## Relevant lore",
    "long_memory": None,
    "temp_memory": "## Recently, in short",
    "state": None,
    "goals": None,
    "director": "## Director's note — highest authority, obey immediately",
}

# Order within the system prompt. Deliberately NOT the priority order: the
# director's note goes last because recency inside the prompt carries weight.
_RENDER_ORDER = [
    "rules",
    "protagonist",
    "chapters",
    "keyword_notes",
    "long_memory",
    "temp_memory",
    "state",
    # The directives cluster at the end. Recency inside the prompt carries
    # weight, and these are the three things the story should be pointed at:
    # where the whole arc is going, what is outstanding, and what is overdue.
    "arc",
    "goals",
    "nudge",
    "director",
]


def _items_ledger(session_id: int) -> list[str]:
    """Approved facts, one budget item per line so trimming drops whole facts."""
    import ledger
    if not config.LEDGER_ENABLED:
        return []
    body = ledger.render(session_id)
    return body.split("\n") if body else []


def build(
    session_id: int,
    story: dict,
    intro: dict,
    *,
    retrieved_long: Optional[list[dict]] = None,
    hot_temp: Optional[list[dict]] = None,
    keyword_notes: Optional[list[dict]] = None,
    transcript: Optional[list[dict]] = None,
    closing_user: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the full prompt packet plus an allocation report.

    `closing_user` overrides the text appended when the transcript does not end
    in a user turn. Resuming a reply that hit the length limit needs a different
    instruction from starting the next one.
    """
    if transcript is None:
        # Only the current chapter (plus a short overlap) is live text. Closed
        # chapters are carried by their summaries, which is what frees the budget.
        floor = (store.chapter_transcript_floor(session_id, config.CHAPTER_OVERLAP_MSGS)
                 if config.CHAPTERS_ENABLED else None)
        transcript = store.messages(
            session_id, limit=config.RECENT_TURNS_DEFAULT * 2, since_id=floor
        )

    # Transcript items are newest-first for trimming (drop oldest), then reversed.
    tr_items = [f"{m['role']}: {m['content']}" for m in reversed(transcript)]

    pro = protagonist(session_id, story)

    layers = {
        "director_notes": _items_director(session_id),
        "rules": _items_rules(story, intro),
        "protagonist": _items_protagonist(pro),
        "arc": _items_arc(current_act(session_id, story)),
        "chapters": _items_chapters(session_id),
        "nudge": _items_nudge(session_id, story, store.current_turn(session_id)),
        "state": _items_state(session_id, story),
        "goals": _items_goals(session_id),
        "long_memory": _items_long_memory(retrieved_long or []),
        "keyword_notes": _items_keyword_notes(keyword_notes or []),
        "temp_memory": _items_temp_memory(hot_temp or []),
        "transcript": tr_items,
        "ledger": _items_ledger(session_id),
    }

    # A raw story is not the old engine with its features switched off -- the
    # producers simply do not run. Memory, chapters, lorebook, goals and pacing
    # are absent by construction, so a global settings toggle cannot switch them
    # back on underneath a story that was written without them.
    if story.get("mode") == "raw":
        keep = {"director_notes", "rules", "ledger", "transcript"}
        layers = {k: (v if k in keep else []) for k, v in layers.items()}

    result = allocate(layers)
    text = result["text"]

    # The director's note is injected at DEPTH, not in the system prompt.
    #
    # Placing it in the system prompt does not work: the transcript sits in the
    # messages array, after the system block and immediately before generation.
    # Several thousand characters of vivid recent scene reliably outrank one line
    # of system text, and the note gets ignored. Appending it to the final user
    # turn puts it closer to the generation point than anything else in the
    # packet, which is the whole reason the channel exists.
    #
    # An overdue fight rides the same channel, for the same measured reason: the
    # identical demand in the system block produced five consecutive peaceful
    # turns past the cadence, because the scene in the transcript was louder.
    from memory import action_demand
    director = text.get("director_notes", "")
    demand = action_demand(story, session_id, store.current_turn(session_id))
    render = [n for n in _RENDER_ORDER
              if n != "director" and n not in config.DEPTH_LAYERS]

    parts: list[str] = []
    for name in render:
        body = text.get(name, "")
        if not body:
            continue
        title = _SECTION_TITLES.get(name)
        parts.append(f"{title}\n\n{body}" if title else body)
    system = "\n\n".join(parts)

    # {{name}} and friends resolve here, once, on the finished system prompt.
    # Doing it at the end rather than per-layer means a lore note or a story
    # rule can reference the player by name without every producer of text
    # having to know about substitution.
    system = subst(system, _pro_tokens(pro))

    # Transcript back into chronological message form. Take the kept count from
    # the report — splitting the rendered text would miscount any message that
    # contains a blank line of its own.
    tr = next(r for r in result["layers"] if r["layer"] == "transcript")
    kept = tr["items"] - tr["dropped"]
    messages = transcript[-kept:] if kept else []

    out_msgs = [{"role": m["role"], "content": m["content"]} for m in messages]

    # Somebody has to be holding the conversation open.
    #
    # Some APIs reject a packet that does not end with a user message, and a
    # narrative-mode story has nobody typing. This also gives the depth channel
    # something to attach to: with no user turn at all, the director's note
    # silently degraded into the system prompt, where it has been measured not to
    # work, and an overdue pacing beat was dropped entirely. See MEASUREMENTS.md.
    if not out_msgs or out_msgs[-1]["role"] != "user":
        out_msgs.append({"role": "user",
                         "content": closing_user or config.CONTINUE_TURN_TEXT})

    # Both depth notes attach to the last user turn. The director's note goes last
    # of the two: it is the author speaking directly and outranks a pacing timer.
    depth: list[str] = []
    # Facts first: they are context for everything after them, and they are the
    # part that changes between turns, so they sit at the front of the volatile
    # tail where llama.cpp reprocesses the least.
    # Volatile reference layers, moved here from the system block by
    # config.DEPTH_LAYERS. Position only — the text and its headings are
    # unchanged, and they are still budgeted at their place in BUDGET_LAYERS.
    #
    # They sit ahead of the directives below because those are short, are the
    # author speaking, and need to be the last thing before generation. Bulk
    # reference first, instructions last.
    for name in _RENDER_ORDER:
        if name not in config.DEPTH_LAYERS:
            continue
        body = text.get(name, "")
        if not body:
            continue
        title = _SECTION_TITLES.get(name)
        depth.append(f"{title}\n\n{body}" if title else body)

    led = text.get("ledger", "")
    if led:
        depth.append("[Established facts - these are still true. Do not "
                     "contradict them:\n" + led + "]")
    if demand:
        depth.append("[Pacing — " + demand + "]")
    if director:
        depth.append(
            "[Director's note — highest authority. This overrides anything earlier "
            "in this conversation, including the established scene. Apply it "
            "immediately and without comment: " + director.strip() + "]"
        )

    if depth:
        last_user = next((i for i in range(len(out_msgs) - 1, -1, -1)
                          if out_msgs[i]["role"] == "user"), None)
        if last_user is not None:
            out_msgs[last_user]["content"] += "\n\n" + "\n\n".join(depth)
        elif director:
            # No user turn to attach to (a retry from the opening beat). Fall back
            # to the system prompt so the note is at least present.
            system = system + "\n\n" + _SECTION_TITLES["director"] + "\n\n" + director

    result["depth"] = depth
    result["system"] = system
    result["messages"] = out_msgs
    result["transcript_kept"] = len(messages)
    result["transcript_total"] = len(transcript)
    return result


def story_fit(story: dict) -> list[dict]:
    """Will this story's always-present text survive the budget?

    Validation catches malformed stories; this catches valid ones that are simply
    too big. The failure it prevents is nasty because it is silent: the rules
    layer is filled in order, so when rules plus details plus opening scene
    overshoot the ceiling, the arbiter drops the opening scene — every turn, for
    the life of the story, with nothing in the prose to say it happened.

    Only the layers that are present on every turn are checked. Keyword notes are
    triggered, memory is retrieved, and the transcript trims by design.
    """
    spec = {name: (floor, ceil) for name, floor, ceil in config.BUDGET_LAYERS}
    out: list[dict] = []

    for intro in story.get("intros", []):
        items = _items_rules(story, intro)
        wanted = sum(len(i) for i in items) + max(0, len(items) - 1) * 2
        ceiling = spec.get("rules", (0, 0))[1]
        out.append({
            "layer": "rules",
            "scope": intro.get("name") or intro.get("id") or "intro",
            "wanted": wanted,
            "ceiling": ceiling,
            "fits": wanted <= ceiling,
            "detail": "story rules + world details + this opening's scene",
        })

    for act in (story.get("arc") or {}).get("acts", []):
        items = _items_arc({**act, "number": 1, "total": 1, "final": False})
        wanted = sum(len(i) for i in items) + max(0, len(items) - 1) * 2
        ceiling = spec.get("arc", (0, 0))[1]
        out.append({
            "layer": "arc",
            "scope": act.get("name") or act.get("id") or "act",
            "wanted": wanted,
            "ceiling": ceiling,
            "fits": wanted <= ceiling,
            "detail": "this act's shape, present on every turn while the act is current",
        })

    always = [k for k in story.get("keywords", []) if k.get("always")]
    if always:
        items = _items_keyword_notes(always)
        wanted = sum(len(i) for i in items) + max(0, len(items) - 1) * 2
        ceiling = spec.get("keyword_notes", (0, 0))[1]
        out.append({
            "layer": "keyword_notes",
            "scope": f"{len(always)} always-on note(s)",
            "wanted": wanted,
            "ceiling": ceiling,
            "fits": wanted <= ceiling,
            "detail": "notes marked always-include, before any keyword match adds more",
        })

    return out


def context_report(
    session_id: int, story: dict, intro: dict, **kw: Any
) -> dict[str, Any]:
    """Same allocation, without the prompt text — for the UI's context panel."""
    r = build(session_id, story, intro, **kw)
    return {
        "layers": r["layers"],
        "used": r["used"],
        "budget": r["budget"],
        "fill": r["fill"],
        "floors_unmet": r["floors_unmet"],
        "advice": r["advice"],
        "transcript_kept": r["transcript_kept"],
        "transcript_total": r["transcript_total"],
    }
