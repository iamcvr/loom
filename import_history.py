"""Import a story that was written somewhere else.

Why this exists
---------------
A story played elsewhere arrives as one long file of prose — 195,000 characters
in the case it was written for. `BUDGET_TOTAL` is 48,000 *characters*, so there
is no field it can be pasted into and no setting that makes it fit. Loom already
solves this problem for stories it plays itself: turns become chapters, chapters
become summaries, summaries become a synopsis, and durable facts go to long-term
memory where an embedding fetches them back when they are relevant. This script
does the same thing to imported text, so the result is a session indistinguishable
from one loom produced, rather than a special case the rest of the engine has to
know about.

What it does NOT do is guess at structure. The input is assumed to be continuous
assistant prose with paragraph breaks and nothing else — no headings, no speaker
labels, no scene markers. Heuristic scene detection was tried on the real file
and produced 521 false positives out of 1,608 paragraphs, because this kind of
prose uses one-line beats constantly. So turns are cut on paragraph boundaries at
a target length, and the model that writes each chapter summary reads the actual
span, which makes a boundary landing mid-scene cost accuracy nothing.

Usage
-----
    python3 import_history.py <story_id> <export.md> [options]

      --title TEXT        session title (default: the story's name)
      --turn-chars N      target characters per imported turn (default 3500)
      --carry-from SID    append the transcript of an existing session, from
                          --carry-after onward. Use when a session was already
                          started past the end of the imported history.
      --carry-after ID    only carry messages with an id greater than this
      --dry-run           print the plan and the token cost, write nothing

Costs one utility call per chapter for the summary, one more per chapter for the
memories, one for the synopsis if the chapters overflow the verbatim window, and
one for standing. Nothing is written until the plan prints clean.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any

import brain
import chapters as chapters_mod
import config
import store
import story as story_mod

# One utility call per chapter reads a whole span, so the span has to fit inside
# the utility model's own context comfortably. At the default turn size a chapter
# is ~70,000 characters, which does.
MEMORY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "salience": {"type": "number"},
                },
                "required": ["text", "salience"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["memories"],
    "additionalProperties": False,
}

STANDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["name", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["relationships"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- splitting


def split_turns(text: str, target: int) -> list[str]:
    """Cut prose into turn-sized blocks without ever splitting a paragraph.

    A paragraph is the smallest unit the author actually wrote; breaking one
    would put half a sentence at the end of a turn and the other half at the
    start of the next, and the transcript trimmer drops whole messages, so the
    orphaned half would eventually appear on its own.
    """
    paras = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]
    turns: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paras:
        if buf and size + len(p) > target:
            turns.append("\n\n".join(buf))
            buf, size = [], 0
        buf.append(p)
        size += len(p) + 2
    if buf:
        turns.append("\n\n".join(buf))
    return turns


# ---------------------------------------------------------------- model calls


def summarise(story: dict, number: int, body: str) -> dict:
    prompt = f"""You are writing the recap for chapter {number} of an ongoing story called "{story['name']}".

# The chapter
{body}

# Your task
Produce a title and a summary.

The title is at most six words, evocative rather than descriptive, and names this
chapter specifically — not the story.

The summary is at most 300 words, past tense, third person, chronological. It is the
ONLY record of these events that later turns will see, so it must carry what the story
has to stay consistent with: what happened, who was present, what was decided,
promised, revealed, injured, learned or changed. Keep proper nouns. Keep the emotional
state characters ended in. Drop atmosphere, scenery and dialogue you could rewrite.
"""
    return brain.utility(prompt, chapters_mod.CHAPTER_SCHEMA)


def extract_memories(story: dict, body: str, want: int) -> list[dict]:
    prompt = f"""You are seeding the long-term memory of an interactive story called "{story['name']}" from a span of it that has already been written.

# The text
{body}

# Your task
Return at most {want} durable facts an author would be angry to see contradicted later.

A good entry is one specific, checkable thing: a name and who they are, a promise made,
a debt, an injury, a habit, a running joke, a place someone lives or works, a number, a
thing someone said they would do. Write each as one self-contained sentence in the past
tense that makes sense with no surrounding context — these are retrieved individually
and shown alone.

Do NOT return the world's standing rules, its politics, or a character's general
personality. Those are already written down elsewhere and repeating them here only
crowds the retrieval.

Salience is 0-100: how badly a later scene would break if this were forgotten.
"""
    out = brain.utility(prompt, MEMORY_SCHEMA)
    return out.get("memories", []) or []


def standing(story: dict, leads: list[str], body: str) -> list[dict]:
    who = ", ".join(leads) or "the leads"
    prompt = f"""Here is the closing stretch of an interactive story called "{story['name']}".

# The text
{body}

# Your task
For each of {who}, and anyone else whose standing with them clearly matters, write how
they now regard the others. Present tense, 1-3 sentences each, using the canonical name.
This is the running record of where everyone stands as the story resumes.
"""
    out = brain.utility(prompt, STANDING_SCHEMA)
    return out.get("relationships", []) or []


# ---------------------------------------------------------------- import


def run(args: argparse.Namespace) -> int:
    st = story_mod.load(args.story, force=True)
    intro_id = args.intro or st["intros"][0]["id"]
    story_mod.intro(st, intro_id)                    # validate it exists

    raw = open(args.export, encoding="utf-8").read()
    turns = split_turns(raw, args.turn_chars)

    carried: list[dict] = []
    if args.carry_from:
        carried = [m for m in store.messages(args.carry_from)
                   if m["id"] > args.carry_after]

    every = config.CHAPTER_EVERY_TURNS
    # The final block stays open: it is the live transcript the next turn
    # continues from. Closing every block would leave the session with nothing
    # verbatim in front of the model at all.
    closed_blocks = [turns[i:i + every] for i in range(0, len(turns), every)][:-1]
    open_block = turns[len(closed_blocks) * every:]

    calls = len(closed_blocks) * 2 + 1 + (1 if len(closed_blocks) > config.CHAPTER_VERBATIM else 0)
    print(f"story          {st['name']} ({st['id']}), intro {intro_id}")
    print(f"source         {args.export}: {len(raw):,} chars")
    print(f"turns          {len(turns)} of ~{args.turn_chars:,} chars")
    print(f"chapters       {len(closed_blocks)} closed x {every} turns, "
          f"{len(open_block)} turns left open")
    print(f"carried over   {len(carried)} messages from session {args.carry_from or '-'}")
    print(f"utility calls  {calls}")
    if args.dry_run:
        print("\n-- dry run, nothing written --")
        for i, b in enumerate(closed_blocks, 1):
            print(f"  chapter {i}: {sum(len(t) for t in b):,} chars, "
                  f"opens {b[0][:70]!r}")
        print(f"  open      : {sum(len(t) for t in open_block):,} chars")
        return 0

    sid = store.create_session(st["id"], intro_id, args.title or st["name"])
    for d in st.get("stats", []):
        store.set_stat(sid, d["key"], d["default"])
    print(f"\nsession {sid} created")

    # The intro's prologue is deliberately NOT written. It is a recap of exactly
    # the events being imported, so with the real history in front of it it would
    # be both redundant and out of order.
    turn = 0
    written: list[int] = []
    for t in turns:
        turn += 1
        written.append(store.add_message(sid, "assistant", t, turn))
    print(f"{len(written)} messages written, turns 1-{turn}")

    # Chapters, oldest first. open_chapter() chains start_msg_id off the previous
    # chapter's end, so each has to be closed before the next is opened.
    for i, block in enumerate(closed_blocks):
        ch = store.open_chapter(sid)
        body = "\n\n".join(block)
        end_turn = (i + 1) * every
        d = summarise(st, ch["number"], body)
        store.close_chapter(ch["id"], d.get("title", "") or f"Chapter {ch['number']}",
                            d.get("summary", ""), written[end_turn - 1], end_turn)
        print(f"  chapter {ch['number']}: {d.get('title','')!r}")

        mems = extract_memories(st, body, args.memories_per_chapter)
        texts = [m["text"].strip() for m in mems if m.get("text", "").strip()]
        vecs = brain.embed(texts) if texts else []
        for j, m in enumerate(mems):
            text = (m.get("text") or "").strip()
            if not text:
                continue
            store.add_memory(sid, text, "long",
                             max(0.0, min(100.0, float(m.get("salience", 50)))),
                             end_turn, vecs[j] if j < len(vecs) and vecs[j] else None)
        print(f"              {len(texts)} long-term memories")

    # Fold whatever aged past the verbatim window into the synopsis, exactly as a
    # played session would have done on its way past chapter CHAPTER_VERBATIM.
    if chapters_mod.needs_refold(sid):
        syn = chapters_mod.refold_synopsis(sid, st)
        if syn.strip():
            store.set_synopsis(sid, syn, store.chapters(sid, closed_only=True)[-1]["number"])
            print(f"  synopsis: {len(syn)} chars")

    # Carried messages continue the same open chapter and the same turn counter.
    for m in carried:
        turn += 1
        store.add_message(sid, m["role"], m["content"], turn)
    if carried:
        print(f"{len(carried)} carried messages appended, through turn {turn}")

    store.set_turn(sid, turn)

    leads = [c["name"] for c in st.get("cast", [])[:2]]
    tail = "\n\n".join(open_block[-3:] + [m["content"] for m in carried])
    for r in standing(st, leads, tail):
        if r.get("name"):
            store.upsert_relationship(sid, r["name"], r.get("summary", ""), turn)
            print(f"  standing: {r['name']}")

    print(f"\ndone. session {sid}, turn {turn}, "
          f"{len(store.chapters(sid, closed_only=True))} closed chapters, "
          f"{len(store.memories(sid, 'long'))} long-term memories")
    return sid


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("story")
    ap.add_argument("export")
    ap.add_argument("--intro", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--turn-chars", type=int, default=3500)
    ap.add_argument("--memories-per-chapter", type=int, default=20)
    ap.add_argument("--carry-from", type=int, default=0)
    ap.add_argument("--carry-after", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    sys.exit(0 if run(ap.parse_args()) is not None else 1)


if __name__ == "__main__":
    main()
