"""Natural language in, a settings proposal out.

"I want more creative prose." "Longer replies." Fifty-five knobs is a lot of
surface, and the mapping from intent to knob is genuinely not obvious — on
2026-10-05 "the prose seems stiff" had two unrelated causes in two different
places, one of them a layer quietly dropping content on every turn.

Two rules, both learned from that day:

DIAGNOSE BEFORE ADJUSTING. A knob-twiddler asked for livelier prose would have
raised temperature, declared success, and left the story's world details being cut
from every single turn. loom already knew and said so in `advice`; nobody was
reading that panel. So the proposal is built from the live allocation, the dropped
counts, the fill and the measured turn latency — not from the request alone.

PROPOSE, NEVER APPLY. Applying is `settings.update()`, reached by the user pressing
a button on a diff they can read. A change nobody can account for is worse than a
setting nobody found.

The `why` on each change is model prose and is NOT trustworthy on its own. On the
first real proposal it claimed that lowering min_p "more aggressively clips the
incoherent tail" — the opposite of what min_p does. Each change therefore carries
the knob's own help text so the two sit side by side and a reader can catch an
inverted claim. Treat `why` as a suggestion of where to look, never as a fact.

Scope is settings. This module reads stories and sessions and writes nothing.
"""
from __future__ import annotations

from typing import Any, Optional

import config
import settings

PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # What is actually wrong, in plain sentences, including anything found in
        # the diagnostics that the request did not ask about.
        "findings": {"type": "array", "items": {"type": "string"}},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    # String, then coerced per knob. A JSON schema cannot say
                    # "int here, float there, one of these strings over there"
                    # across fifty-five differently-typed knobs.
                    "value": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["key", "value", "why"],
            },
        },
    },
    "required": ["findings", "changes"],
}

_PROMPT = """You tune a local interactive-fiction engine for a reader who has asked
for something in plain words. You do not apply anything; you propose.

THE REQUEST
{request}

DIAGNOSTICS FROM THE LAST ASSEMBLED PROMPT
{diagnostics}

SETTINGS YOU MAY PROPOSE CHANGES TO
{knobs}

Rules:
- Read the diagnostics first. If a layer is dropping content, or turns are slow, or
  the budget is nearly full, say so in `findings` even when the request did not ask
  about it. A dropped layer means the model never saw that content at all, which
  looks like a bad model and is not.
- Only propose a key listed above, and only within its stated bounds or choices.
- Propose the fewest changes that address the request. Two knobs is usually plenty.
- Every change needs a `why` a reader can check, referring to the help text or the
  diagnostics. Never invent a measurement.
- If the request is already satisfied by the current values, return no changes and
  say so in `findings`.
- Before proposing PROSE.max_tokens, check how many replies actually reached the
  ceiling. If few or none did, the ceiling is NOT the constraint — the model is
  choosing to stop, raising it will change nothing, and the honest answer is that
  the story's own rules have to ask for longer scenes. Say that instead of
  proposing a knob that cannot work.
- Price the change. The diagnostics give measured latency: the prompt time tracks
  the context budget and the reply time tracks the reply ceiling. If a change moves
  either, say in `why` roughly what it does to the numbers you were given — a
  reader asking for longer replies deserves to know the wait grows with them. Use
  only the figures above; never invent one.
"""


def _knob_digest() -> str:
    """The settings schema as the model sees it. Help text included on purpose.

    The help strings carry the measured reasoning — "min_p clips the incoherent
    tail, which is what lets a higher temperature stay creative" — and that is
    exactly the knowledge needed to answer "make the prose more creative". Omitting
    them to save tokens would leave the model guessing from the label.
    """
    out: list[str] = []
    for k in settings.current()["knobs"]:
        # Only scalars. BUDGET_LAYERS is a thirteen-row table and the proposal
        # schema carries values as strings, so offering it only produces rejects —
        # which it did, on the second real request.
        if k["type"] not in ("int", "float", "bool", "str", "choice", "model"):
            continue
        bits = [f"{k['key']} ({k['type']})", f"= {k['value']!r}", f"default {k['default']!r}"]
        if k.get("choices"):
            bits.append("one of " + ", ".join(map(str, k["choices"])))
        elif k.get("min") is not None:
            bits.append(f"range {k['min']}..{k['max']}")
        out.append("- " + " | ".join(bits))
        if k.get("help"):
            out.append(f"    {k['help']}")
    return "\n".join(out)


def diagnostics(session_id: Optional[int] = None) -> dict:
    """What loom currently knows about its own prompt and latency.

    The packet is rebuilt rather than remembered: the allocation report is emitted
    over SSE each turn and never stored, and rebuilding costs no model call for the
    prose — only the retrieval embedding.
    """
    out: dict[str, Any] = {"context": settings.context_report()}
    if not session_id:
        return out

    import assemble
    import memory
    import store
    import story as story_mod

    try:
        s = store.get_session(session_id)
        st = story_mod.load(s["story_id"])
        intro = story_mod.intro(st, s["intro_id"])
        turn = int(s["turn"])
        p = assemble.build(
            session_id, st, intro,
            retrieved_long=memory.retrieve_long(session_id, memory.retrieval_query(session_id)),
            hot_temp=memory.hot_temp(session_id, turn, limit=25),
            keyword_notes=memory.match_keywords(st, session_id),
        )
        out["story"] = {"id": s["story_id"], "mode": st.get("mode") or "play", "turn": turn}
        out["fill"] = p["fill"]
        out["advice"] = p["advice"]
        out["layers"] = [
            {k: l[k] for k in ("layer", "wanted", "allocated", "used", "dropped", "ceiling")}
            for l in p["layers"] if l["wanted"] or l["used"]
        ]
        out["timing"] = store.timing_stats(session_id)
        out["replies"] = _reply_shape(session_id)
    except Exception as e:  # pragma: no cover - diagnostics must never break advice
        out["error"] = str(e)
    return out


def _reply_shape(session_id: int, limit: int = 10) -> dict:
    """Are replies actually hitting the ceiling, or stopping on their own?

    The question that makes "I want longer replies" answerable. A reply that ends
    because the model finished is not constrained by PROSE.max_tokens, and raising
    the ceiling will change nothing — the lever is the story's rules asking for
    longer scenes. Measured on the first real request: none of four replies had hit
    the ceiling, and the advisor proposed raising it anyway.

    `truncated` is already recorded per message for the resume button, so this costs
    one query and no new state.
    """
    import store
    rows = store.recent_assistant_shape(session_id, limit)
    if not rows:
        return {}
    ceiling_chars = int(config.PROSE.get("max_tokens", 0) * config.CHARS_PER_TOKEN)
    lens = [r["chars"] for r in rows]
    hit = sum(1 for r in rows if r["truncated"])
    return {
        "replies": len(rows),
        "hit_ceiling": hit,
        "avg_chars": sum(lens) // len(lens),
        "max_chars": max(lens),
        "ceiling_chars": ceiling_chars,
        "ceiling_tokens": int(config.PROSE.get("max_tokens", 0)),
    }


def _render_diagnostics(d: dict) -> str:
    lines: list[str] = []
    c = d.get("context") or {}
    if c:
        lines.append(
            f"context window {c.get('window')} tokens, reply ceiling "
            f"{c.get('reply_tokens')}, giving a budget of {c.get('budget')} characters"
            + (f" (ceilings stretched x{c['ceiling_scale']})" if c.get("ceiling_scale", 1) > 1 else "")
        )
    if d.get("story"):
        s = d["story"]
        lines.append(f"story {s['id']}, mode {s['mode']}, turn {s['turn']}")
    if d.get("fill") is not None:
        lines.append(f"budget fill {d['fill']:.0%} — advice: {d.get('advice') or 'none'}")
    for l in d.get("layers") or []:
        note = f"  DROPPING {l['dropped']} item(s) — ceiling {l['ceiling']}" if l["dropped"] else ""
        lines.append(f"layer {l['layer']}: wanted {l['wanted']}, used {l['used']}{note}")
    t = d.get("timing")
    if t:
        lines.append(
            f"latency over {t['turns']} turns: prompt {t['med10']['ttft_ms']}ms median, "
            f"reply {t['med10']['gen_ms']}ms median "
            f"(last turn {t['last']['ttft_ms']}ms + {t['last']['gen_ms']}ms)"
        )
    r = d.get("replies") or {}
    if r:
        lines.append(
            f"reply lengths over {r['replies']} replies: {r['avg_chars']} characters "
            f"average, {r['max_chars']} longest, against a ceiling of "
            f"{r['ceiling_tokens']} tokens (about {r['ceiling_chars']} characters). "
            f"{r['hit_ceiling']} of {r['replies']} actually reached the ceiling."
        )
    if d.get("error"):
        lines.append(f"(diagnostics incomplete: {d['error']})")
    return "\n".join(lines) or "none available"


def _ineffective(key: str, value: Any, diag: dict) -> Optional[str]:
    """Would this change demonstrably do nothing? Computed, not asked.

    The model diagnoses this correctly and then proposes the knob anyway — observed
    twice on the same request, with the finding and the change contradicting each
    other in one response. Telling it not to did not work, so the check lives here
    where it is deterministic.

    Only conditions that can be shown from recorded data belong in here. A guard
    that guesses is worse than no guard.
    """
    r = diag.get("replies") or {}
    if key == "PROSE.max_tokens" and r:
        try:
            raising = float(value) > float(r.get("ceiling_tokens") or 0)
        except (TypeError, ValueError):
            raising = False
        if raising and not r.get("hit_ceiling"):
            return (
                f"None of the last {r['replies']} replies reached the "
                f"{r['ceiling_tokens']}-token ceiling — they average "
                f"{r['avg_chars']} characters against a ceiling worth about "
                f"{r['ceiling_chars']}. The model is choosing to stop, so the "
                f"ceiling is not what is limiting length and raising it changes "
                f"nothing. Longer replies have to be asked for in the story's own "
                f"rules."
            )
    return None


def propose(request: str, session_id: Optional[int] = None,
            spec: Optional[dict] = None) -> dict:
    """Findings and a validated set of proposed changes. Writes nothing."""
    import brain

    request = (request or "").strip()
    if not request:
        raise ValueError("say what you want changed")

    diag = diagnostics(session_id)
    prompt = _PROMPT.format(request=request,
                            diagnostics=_render_diagnostics(diag),
                            knobs=_knob_digest())
    raw = brain.utility(prompt, PROPOSAL_SCHEMA, spec=spec or config.UTILITY)

    by_key = {k["key"]: k for k in settings.current()["knobs"]}
    changes: list[dict] = []
    rejected: list[dict] = []
    ineffective: list[dict] = []
    for ch in raw.get("changes") or []:
        key = str(ch.get("key") or "").strip()
        knob = by_key.get(key)
        if not knob:
            rejected.append({"key": key, "reason": "no such setting"})
            continue
        try:
            # Validated here as well as in update(), so a proposal is never shown
            # to the reader as applicable when it would be refused on the way in.
            value = settings._coerce(knob, ch.get("value"))
        except ValueError as e:
            rejected.append({"key": key, "reason": str(e)})
            continue
        if value == knob["value"]:
            continue                      # already there; not a change
        if reason := _ineffective(key, value, diag):
            ineffective.append({"key": key, "label": knob["label"],
                                "from": knob["value"], "to": value,
                                "why": str(ch.get("why") or "").strip(),
                                "reason": reason})
            continue
        changes.append({
            "key": key, "label": knob["label"], "from": knob["value"], "to": value,
            "why": str(ch.get("why") or "").strip(),
            # The knob's own documentation, shown beside the model's reasoning so
            # the reader can check one against the other. Measured need: on the
            # first real proposal the model wrote that LOWERING min_p "more
            # aggressively clips the incoherent tail", which is backwards. The
            # change was defensible and the reason was inverted, and nothing in
            # the response said which. Authoritative text beside the claim is the
            # cheapest defence; verifying the claim itself is not tractable.
            "help": knob.get("help") or "",
        })

    return {
        "request": request,
        "findings": [str(f) for f in (raw.get("findings") or [])],
        "changes": changes,
        # Proposed, valid, and provably pointless. Shown rather than hidden: the
        # reason is the useful part, and silently dropping a change the model
        # explained would leave the reader wondering what happened to it.
        "ineffective": ineffective,
        "rejected": rejected,
        "diagnostics": diag,
    }
