"""Runtime-editable tuning knobs.

config.py holds the defaults and stays the source of truth for what a knob *is*.
This module adds a thin layer on top: a declarative description of which of those
values are safe to change while the server runs, plus persistence so a change
survives a restart.

Why this exists
---------------
The tuning constants were always going to be wrong until a few hundred turns of
real play said otherwise. Editing config.py meant a rebuild and a restart, which
is enough friction that nobody tunes anything. These are dials you want to move
*during* a session, notice the difference on the next turn, and move back.

How it works
------------
Every consumer reads `config.X` at call time rather than capturing it at import,
so writing a new value onto the config module takes effect on the very next turn.
That is the whole mechanism. Overrides are stored in state/settings.json as a
sparse dict — only what differs from the default — so upgrading config.py's
defaults still reaches anything the user never touched.

Deliberately absent: paths, host/port, and API keys. Those are deployment facts,
not tuning, and the keys must not be shipped to a browser.
"""
from __future__ import annotations

import json
import math
import threading
from typing import Any, Optional

import config

_lock = threading.Lock()
_defaults: dict[str, Any] = {}


# Knobs that used to exist. A stored settings.json carrying one of these is
# migrated — the key is dropped and the reason reported — rather than called
# "unknown", because "unknown setting" reads like a typo when the setting was in
# fact deliberately removed.
RETIRED = {
    "SPEAKER_LINES": "attributed dialogue is always on; it is part of CORE_RULES",
    "SPEAKER_FORMAT": "folded into CORE_RULES, where it can still be edited",
    "BUDGET_TOTAL": "now derived from the context window; set PROSE.num_ctx instead",
    "PROSE.thinking": "removed with the frontier providers",
    "PROSE_FALLBACK.url": "the refusal-fallback tier was removed",
    "PROSE_FALLBACK.provider": "the refusal-fallback tier was removed",
    "PROSE_FALLBACK.model": "the refusal-fallback tier was removed",
    "PROSE_FALLBACK.max_tokens": "the refusal-fallback tier was removed",
    "PROSE_FALLBACK_ON_REFUSAL": "the refusal-fallback tier was removed",
}


def _path() -> Any:
    return config.STATE_DIR / "settings.json"


# ---------------------------------------------------------------- knob schema


def K(key, type_, group, label, help_="", **kw) -> dict:
    return {"key": key, "type": type_, "group": group, "label": label, "help": help_, **kw}


# `key` may be dotted to reach into a config dict, e.g. "PROSE.model".
KNOBS: list[dict] = [
    # ---- context budget
    #
    # ONE knob. The character budget, and therefore every layer's share of it, is
    # derived from this window — see derive_budget(). Three numbers that had to be
    # kept in agreement by hand are now one that cannot disagree with itself.
    K("PROSE.num_ctx", "int", "Context budget", "Context window (tokens)",
      "The whole window, covering prompt AND reply. Everything else follows from "
      "it: the reply ceiling is reserved out of it and the remainder becomes the "
      "character budget the arbiter spends. On ollama loom sends this value, so it "
      "IS the window — changing it makes ollama reload the model, which costs about "
      "30 seconds on the next turn. On openai_compat loom cannot set the window, so "
      "declare what your server was started with and budgeting stays honest. Every "
      "token is KV cache in memory; the settings panel shows what the window costs.",
      min=2048, max=131072, step=1024),
    K("CORE_RULES", "text", "Story rules", "Rules every story inherits",
      "The engine's own contract, shared by every story, rendered ahead of the "
      "story's own rules. It covers the things that make loom what it is rather "
      "than what any one story is: that the player's character is theirs alone, "
      "that any input format is accepted without comment, that your narration is "
      "clean prose, that only one thing happens at a time and the player ends "
      "scenes, and how long a turn runs. A story file then carries only what is "
      "actually its own \u2014 tone, world, cast. Before this existed every story "
      "restated this in its own words, so each said it differently and the "
      "shortest left it out entirely. Its budget floor matches its length, so the "
      "arbiter can never cut it.",
      rows=18),
    K("BUDGET_LAYERS", "layers", "Context budget", "Layer floors and ceilings",
      "Priority order, top to bottom — the arbiter fills each layer in turn. A layer "
      "always gets its floor; it grows toward its ceiling only if budget remains. "
      "Unspent allowance carries downstream, so the transcript absorbs the slack."),

    # ---- memory
    K("MEM_DECAY_HALFLIFE_TURNS", "float", "Memory", "Decay half-life (turns)",
      "A temporary memory loses half its heat over this many turns. Lower means the "
      "story forgets small details faster.", min=1, max=500, step=1),
    K("MEM_REINFORCE_WEIGHT", "float", "Memory", "Reinforcement weight",
      "Multiplier on the log(times referenced) term. Higher makes repetition matter "
      "more than raw salience.", min=0, max=5, step=0.1),
    K("MEM_TEMP_FLOOR", "float", "Memory", "Forget below heat",
      "Temporary memories colder than this drop out entirely.", min=0, max=100, step=1),
    K("MEM_PROMOTE_THRESHOLD", "float", "Memory", "Promote above heat",
      "Heat at which a temporary memory may graduate to long-term.",
      min=0, max=200, step=5),
    K("MEM_PROMOTE_MIN_REINFORCED", "int", "Memory", "Promotion needs N references",
      "Promotion must be earned. At 0 a high-salience memory graduates on the turn "
      "it is created, which makes the temporary layer pointless.", min=0, max=10, step=1),
    K("MEM_TEMP_MAX_ROWS", "int", "Memory", "Max stored temp memories",
      "Hard cap per session, oldest-coldest pruned first.", min=20, max=5_000, step=10),
    K("MEM_LONG_MAX_ROWS", "int", "Memory", "Max stored long-term memories",
      "Retrieval pulls a fixed number per turn, so every row past what the search "
      "can surface only makes the search worse — a 132-turn session had reached 497. "
      "Over this cap, memories never referenced again are dropped, least salient "
      "first. Ones the story came back to are always kept.",
      min=20, max=5_000, step=10),

    # ---- retrieval
    K("MEM_RETRIEVE_K", "int", "Retrieval", "Long-term memories per turn",
      "How many durable facts the embedding search pulls in.", min=0, max=40, step=1),
    K("MEM_RETRIEVE_MIN_SIMILARITY", "float", "Retrieval", "Minimum similarity",
      "Cosine floor for a retrieved memory. Raise it if irrelevant facts keep "
      "surfacing; lower it if the story forgets things it should know.",
      min=0, max=1, step=0.01),
    K("MEM_QUERY_TURNS", "int", "Retrieval", "Query window (turns)",
      "How many recent turns are summed into the retrieval query.", min=1, max=20, step=1),
    K("KEYWORD_SCAN_TURNS", "int", "Retrieval", "Keyword scan depth (turns)",
      "How far back keyword triggers look for a match.", min=1, max=20, step=1),
    K("KEYWORD_MAX_NOTES", "int", "Retrieval", "Max keyword notes",
      "Cap on lore entries injected at once, before the budget trims further.",
      min=0, max=40, step=1),
    K("RECENT_TURNS_DEFAULT", "int", "Retrieval", "Transcript window (turns)",
      "How many recent turns are offered to the arbiter before it trims to budget.",
      min=4, max=200, step=2),

    # ---- models
    K("PROSE.provider", "choice", "Models", "Prose provider",
      "'ollama' talks to the native API on this host and is the only one that can "
      "send min_p, top_k and num_ctx. 'openai_compat' reaches the same models "
      "through /v1, which silently drops all three.",
      choices=["ollama", "openai_compat"]),
    K("PROSE.model", "model", "Models", "Prose model",
      "The model you actually read. Cost lives here.", provider_from="PROSE.provider"),
    K("PROSE.url", "str", "Models", "Prose base URL (openai_compat only)",
      "Required when the provider is openai_compat, ignored otherwise. For ollama "
      "on this host: http://127.0.0.1:11434/v1 — note the /v1, and "
      "note that ollama silently truncates anything past OLLAMA_CONTEXT_LENGTH "
      "rather than erroring, so keep the total budget under the context you "
      "actually loaded the model with."),
    K("PROSE.max_tokens", "int", "Models", "Prose max tokens",
      "Ceiling on reply length.", min=200, max=8_000, step=100),
    K("PROSE.temperature", "float", "Models", "Prose temperature",
      "1.0 is neutral. Pair a higher value with min_p rather than top_p.",
      min=0, max=2, step=0.05),
    K("PROSE.top_p", "float", "Models", "Prose top_p (local only)",
      "Nucleus sampling. 1.0 is off. Gemma 3's published config pairs 0.95 with "
      "top_k 64.", min=0.1, max=1.0, step=0.01),
    K("PROSE.top_k", "int", "Models", "Prose top_k (local only)",
      "Keep only the k likeliest tokens. 0 is off.", min=0, max=200, step=1),
    K("PROSE.min_p", "float", "Models", "Prose min_p (local only)",
      "Drop tokens below this share of the top token's probability. 0 is off. The "
      "single most useful knob on a Nemo finetune: it clips the incoherent tail, "
      "which is what lets a higher temperature stay creative without going strange. "
      "0.02-0.05 is the usual range.", min=0.0, max=0.5, step=0.01),
    K("PROSE.repeat_penalty", "float", "Models", "Prose repeat penalty (local only)",
      "1.0 is off. Above ~1.15 it starts eating ordinary English — 'the' is a "
      "repeated token too.", min=1.0, max=1.5, step=0.01),
    K("PROSE_RETRIES", "int", "Models", "Retries on provider failure",
      "How many times to retry a turn when the provider is overloaded, rate-limited "
      "or returns nothing. Backoff doubles each attempt (2s, 4s, 8s, 16s). Retries "
      "only happen before any text has reached the screen.", min=0, max=8, step=1),

    K("UTILITY.provider", "choice", "Models", "Utility provider",
      "", choices=["ollama", "openai_compat"]),
    K("UTILITY.model", "model", "Models", "Utility model",
      "Does the structured JSON delta after each turn. Cheap tier is the point.",
      provider_from="UTILITY.provider"),
    K("UTILITY.max_tokens", "int", "Models", "Utility max tokens",
      "Too low and the delta truncates mid-JSON.", min=500, max=16_000, step=500),
    K("UTILITY_ENABLED", "bool", "Models", "Run the memory pipeline",
      "Off means pure chat: no extraction, no memory, no goals. Useful for A/B."),
    K("EMBED_ENABLED", "bool", "Models", "Use embeddings",
      "Off falls back to recency ordering for long-term memory."),
    K("EMBED_MODEL", "str", "Models", "Embedding model",
      "Must match a model pulled in Ollama. Changing this invalidates stored vectors."),

    # ---- images
    K("IMAGES_ENABLED", "bool", "Images", "Generate images", ""),
    K("PORTRAIT_CAST_ONLY", "bool", "Images", "Portraits for story cast only",
      "On, only characters your story.yaml defines get a portrait automatically — "
      "everyone else is one click in the cast panel. Off, every newly named person "
      "gets one, which fills the panel with innkeepers rendered from no description."),
    K("SCENE_MIN_TURN_GAP", "int", "Images", "Minimum turns between scenes",
      "Debounce on scene art, however often the model reports a scene change.",
      min=0, max=50, step=1),
    K("COMFY_CHECKPOINT", "str", "Images", "Checkpoint", "Filename as ComfyUI sees it."),
    K("IMG_QUALITY_TAGS", "text", "Images", "Quality tags",
      "Prepended to every prompt."),
    K("IMG_NEGATIVE", "text", "Images", "Negative prompt", ""),

    # ---- chapters
    K("CHAPTERS_ENABLED", "bool", "Chapters", "Compact into chapters",
      "Off, the transcript is never summarised and the oldest turns simply fall out "
      "of context as the story grows."),
    K("CHAPTER_EVERY_TURNS", "int", "Chapters", "Compact every N turns",
      "How long a chapter runs before it is summarised and its turns are released "
      "from the prompt. Shorter chapters free more budget and lose more detail.",
      min=5, max=100, step=1),
    K("CHAPTER_TRIGGER_FILL", "float", "Chapters", "Compact early at this fill",
      "Safety valve under the turn count. When the context budget is this full, "
      "compact now rather than waiting — otherwise the arbiter starts "
      "shedding the oldest messages every turn, which loses history and forces the "
      "whole prompt to be reprocessed. At 1.0 it only fires once the budget is "
      "completely full, i.e. reactively, which is the behaviour this replaced.",
      min=0.5, max=1.0, step=0.05),
    K("CHAPTER_MIN_TURNS", "int", "Chapters", "Never compact before N turns",
      "Floor, so a burst of very long turns cannot trigger a two-turn chapter.",
      min=2, max=50, step=1),
    K("CHAPTER_VERBATIM", "int", "Chapters", "Recent summaries kept in full",
      "Older ones are folded into the running synopsis.", min=0, max=10, step=1),
    K("CHAPTER_OVERLAP_MSGS", "int", "Chapters", "Messages carried past a break",
      "Kept as live text after a compaction so the next turn doesn't read as a jump "
      "cut.", min=0, max=12, step=1),

    # ---- lorebook
    K("LOREBOOK_ENABLED", "bool", "Lorebook", "Use the lorebook",
      "Entries fire into the prompt when one of their keywords appears in recent "
      "play — the same mechanism as a story's authored notes."),
    K("LOREBOOK_SUGGEST_MAX", "int", "Lorebook", "Suggestions per chapter",
      "Entries proposed at each compaction. They contribute nothing until accepted.",
      min=0, max=10, step=1),
    K("LOREBOOK_MIN_MENTIONS", "int", "Lorebook", "Suggest after N mentions",
      "A name has to recur across this many turns of a chapter before it is even "
      "considered — this is the 'it keeps coming up' test.", min=1, max=10, step=1),

    # ---- ledger
    K("LEDGER_ENABLED", "bool", "Ledger", "Use the ledger",
      "Facts you have approved, injected into every turn just before generation. "
      "Separate from Memory and the Lorebook on purpose: nothing here reaches the "
      "model until you accept it. Reviewed at http://atlas:8100/ledger"),
    K("LEDGER_TRIGGER_FILL", "float", "Ledger", "Propose facts at fill",
      "When the context budget reaches this fraction, the turn finishes and then "
      "offers you facts to keep. Fires early on purpose - past the model's window "
      "the front of the prompt is silently dropped, which reads as the story "
      "forgetting.", min=0.5, max=0.98, step=0.01),
    K("LEDGER_PROPOSE_MAX", "int", "Ledger", "Facts offered per review",
      "Measured: roughly half of what the model proposes is scene detail rather "
      "than a durable fact, so expect to bin some.", min=1, max=12, step=1),
    K("LEDGER_MAX_ACTIVE", "int", "Ledger", "Maximum active facts",
      "Hard ceiling on how many can be in the prompt at once.",
      min=10, max=300, step=10),

    # ---- goals
    K("GOALS_MAX_ACTIVE", "int", "Goals", "Objectives at once",
      "One reads as a story; six reads as a task list. Anything extracted past this "
      "waits in the backlog and is promoted when a slot frees up.",
      min=1, max=6, step=1),
    K("GOALS_NEED_CONFIRMATION", "bool", "Goals", "Confirm extracted goals",
      "On, every goal the model extracts waits for a click and nothing is ever "
      "promoted automatically. Off, a goal takes a free slot immediately."),
    K("GOAL_NUDGE_ENABLED", "bool", "Goals", "Nudge toward the objective",
      "After a few quiet turns, add a note asking for the objective to surface once, "
      "in passing, from a character — then be dropped. Off, the story never reminds "
      "you, which in practice means it forgets."),
    K("GOAL_NUDGE_AFTER", "int", "Goals", "Nudge after N quiet turns",
      "Turns the objective may go unmentioned first. Low values make the story nag.",
      min=1, max=20, step=1),
    K("GOAL_NUDGE_ESCALATE", "int", "Goals", "Press harder after N turns",
      "Past this the reminder is allowed to be plainer — someone is waiting, the "
      "shop is closing. Still never forces the player's hand.", min=2, max=40, step=1),
]

_BY_KEY = {k["key"]: k for k in KNOBS}
GROUPS = list(dict.fromkeys(k["group"] for k in KNOBS))


# ---------------------------------------------------------------- get / set


def _read(key: str) -> Any:
    head, _, tail = key.partition(".")
    val = getattr(config, head, None)
    return val.get(tail) if tail else val


def _write(key: str, value: Any) -> None:
    head, _, tail = key.partition(".")
    if tail:
        d = dict(getattr(config, head, {}) or {})
        d[tail] = value
        setattr(config, head, d)
    else:
        setattr(config, head, value)


def _snapshot_defaults() -> None:
    if not _defaults:
        for k in KNOBS:
            _defaults[k["key"]] = _read(k["key"])


def _coerce(knob: dict, value: Any) -> Any:
    """Coerce and clamp. Raises ValueError with a message the UI can show."""
    t = knob["type"]
    key = knob["key"]

    if t == "bool":
        return bool(value)

    if t in ("int", "float"):
        try:
            n = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: '{value}' is not a number")
        lo, hi = knob.get("min"), knob.get("max")
        if lo is not None:
            n = max(float(lo), n)
        if hi is not None:
            n = min(float(hi), n)
        return int(round(n)) if t == "int" else round(n, 6)

    if t == "choice":
        s = str(value)
        if s not in knob["choices"]:
            raise ValueError(f"{key}: '{s}' is not one of {', '.join(knob['choices'])}")
        return s

    if t in ("str", "text", "model"):
        s = str(value) if t == "text" else str(value).strip()
        if t != "text" and not s:
            raise ValueError(f"{key}: cannot be empty")
        return s

    if t == "layers":
        return _coerce_layers(value)

    raise ValueError(f"{key}: unknown type {t}")


def _coerce_layers(value: Any) -> list[tuple[str, int, int]]:
    """Validate the budget layer table.

    The layer *names* are fixed — assemble.py looks them up by name — so the
    editable parts are the order, the floors and the ceilings.

    Names that no longer exist are dropped and names that did not exist yet are
    spliced in from the defaults, rather than either being an error. A stored
    override is a record of the tuning someone did; it goes stale across a release
    that adds or retires a layer, and throwing the whole thing away over one
    obsolete row would silently undo every other choice in it.
    """
    known = {n for n, _, _ in _defaults.get("BUDGET_LAYERS", config.BUDGET_LAYERS)}
    if not isinstance(value, list) or not value:
        raise ValueError("BUDGET_LAYERS: expected a non-empty list")

    out: list[tuple[str, int, int]] = []
    seen: set[str] = set()
    for i, row in enumerate(value):
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise ValueError(f"BUDGET_LAYERS[{i}]: expected [name, floor, ceiling]")
        name, floor, ceil = row
        name = str(name)
        if name not in known:
            continue          # retired layer from an older release
        if name in seen:
            raise ValueError(f"BUDGET_LAYERS[{i}]: duplicate layer '{name}'")
        seen.add(name)
        try:
            floor, ceil = int(floor), int(ceil)
        except (TypeError, ValueError):
            raise ValueError(f"BUDGET_LAYERS[{i}] ('{name}'): floor and ceiling must be whole numbers")
        floor, ceil = max(0, floor), max(0, ceil)
        if floor > ceil:
            raise ValueError(f"BUDGET_LAYERS[{i}] ('{name}'): floor {floor} exceeds ceiling {ceil}")
        out.append((name, floor, ceil))

    # A stored override written before a new layer existed is not an error — it is
    # simply out of date. Splice the missing layers in at their default positions
    # and keep the user's edits to the layers they did configure. Rejecting the
    # whole value would silently discard every tuning choice they had made.
    if known - seen:
        defaults = _defaults.get("BUDGET_LAYERS", config.BUDGET_LAYERS)
        merged: list[tuple[str, int, int]] = []
        by_name = {n: (n, f, c) for n, f, c in out}
        for name, floor, ceil in defaults:
            merged.append(by_name.get(name, (name, floor, ceil)))
        out = merged
    return out


# ---------------------------------------------------------------- persistence


def _load_file() -> dict:
    p = _path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_file(overrides: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(overrides, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def apply_saved() -> list[str]:
    """Load overrides from disk onto the config module. Returns any complaints.

    A knob that no longer exists, or a value config.py has since outgrown, is
    reported and skipped rather than raised — a stale settings.json must never
    stop the server booting.
    """
    with _lock:
        _snapshot_defaults()
        problems: list[str] = []
        stored = _load_file()
        retired = [k for k in stored if k in RETIRED]
        for key, value in stored.items():
            if key in RETIRED:
                problems.append(f"retired setting '{key}' dropped — {RETIRED[key]}")
                continue
            knob = _BY_KEY.get(key)
            if not knob:
                problems.append(f"unknown setting '{key}' ignored")
                continue
            try:
                _write(key, _coerce(knob, value))
            except ValueError as e:
                problems.append(str(e))
        # Written back once so the complaint is not repeated on every boot.
        if retired:
            for key in retired:
                stored.pop(key, None)
            _save_file(stored)

        # The budget is derived, so it is computed after the knobs land.
        derive_budget()
        if msg := _plan_problem(context_plan()):
            problems.append(msg)
        problems.extend(_report_warnings(context_report()))
        return problems


def check_models() -> list[str]:
    """Warn — never block — when a configured model isn't in the provider's list.

    Deliberately advisory. A provider's listing and the name that actually
    serves need not match — ollama tags drift from what a Modelfile registers —
    so treating "absent from the list" as "invalid" would reject a model that
    demonstrably works. The list is good for populating a menu and for catching
    typos; it is not an authority on what the API will accept. See
    MEASUREMENTS.md.
    """
    import brain

    warnings: list[str] = []
    for knob in KNOBS:
        if knob["type"] != "model":
            continue
        provider = _read(knob["provider_from"])
        chosen = _read(knob["key"])
        listing = brain.models(provider)

        if listing["error"]:
            warnings.append(
                f"{knob['label']}: could not check '{chosen}' against {provider} "
                f"({listing['error']}). Saved anyway."
            )
        elif chosen not in listing["models"]:
            near = [m for m in listing["models"] if m.startswith(chosen[:12])]
            hint = f" Closest listed: {', '.join(near[:3])}." if near else ""
            warnings.append(
                f"{knob['label']}: '{chosen}' is not in {provider}'s model list. "
                f"It may still work as an alias.{hint}"
            )
    return warnings


def derive_budget() -> int:
    """Set config.BUDGET_TOTAL from the context window. The window is the knob.

    One number instead of three. The window covers prompt and reply together: the
    reply ceiling is reserved out of it, and what remains — less a safety margin,
    because CHARS_PER_TOKEN is an average — becomes the character budget the
    arbiter has to spend.

    Deriving it is what makes ollama's silent front-truncation *structurally*
    impossible rather than merely warned about: there is no second number left to
    disagree with the first.
    """
    plan = context_plan()
    if plan["window"] > 0:
        config.BUDGET_TOTAL = plan["budget"]
    return config.BUDGET_TOTAL


def context_plan(pending: Optional[dict] = None) -> dict:
    """The context arithmetic. Pure — no network, safe to call during validation.

    `pending` overlays validated-but-unwritten values so a save is judged on its
    result rather than on a half-applied state.
    """
    pending = pending or {}

    def val(key, current):
        return pending.get(key, current)

    window = int(val("PROSE.num_ctx", config.PROSE.get("num_ctx") or 0))
    reply = int(val("PROSE.max_tokens", config.PROSE.get("max_tokens") or 0))
    layers = val("BUDGET_LAYERS", config.BUDGET_LAYERS)
    floors = sum(int(row[1]) for row in layers)

    usable = max(0, window - reply)
    budget = int(usable * config.CHARS_PER_TOKEN * config.BUDGET_SAFETY)
    return {
        "provider": val("PROSE.provider", config.PROSE.get("provider")),
        "model": val("PROSE.model", config.PROSE.get("model")),
        "window": window,
        "reply_tokens": reply,
        "usable_tokens": usable,
        "budget": budget,
        "floors": floors,
        # The only way the derivation can fail: a window too small to seat the
        # layer minimums. Everything else fits by construction.
        "floors_fit": budget >= floors,
    }


def _plan_problem(plan: dict) -> Optional[str]:
    """The blocking complaint about a plan, or None. Pure."""
    if plan["window"] <= 0 or plan["floors_fit"]:
        return None
    reply = plan["reply_tokens"]
    needed = int(plan["floors"] / (config.CHARS_PER_TOKEN * config.BUDGET_SAFETY)) + reply
    return (
        f"context window too small: {plan['window']} tokens leaves "
        f"{plan['budget']} characters of budget after reserving {reply} for the "
        f"reply, but the layer floors need {plan['floors']}. Raise the window to "
        f"at least {needed} tokens, lower the reply ceiling, or lower the floors "
        f"in BUDGET_LAYERS."
    )


def context_report(pending: Optional[dict] = None) -> dict:
    """context_plan() plus what the model says about itself. Hits ollama (cached)."""
    import brain
    plan = context_plan(pending)
    limits = brain.model_limits(plan["model"], provider=plan["provider"])
    per_token = limits.get("kv_bytes_per_token") or 0
    trained = limits.get("trained_ctx") or 0
    # Written ceilings are stretched by this much, so the panel can explain why a
    # layer's ceiling is not the number typed into the layers table. Derived from
    # the PLAN's budget, not the live one, or a preview of a bigger window would
    # report the stretch it has today instead of the one it is asking for.
    scale = (max(1.0, plan["budget"] / config.BUDGET_REFERENCE)
             if config.BUDGET_REFERENCE else 1.0)
    return {
        **plan,
        "ceiling_scale": round(scale, 2),
        "kv_bytes_per_token": per_token,
        "kv_bytes": per_token * plan["window"],
        "trained_ctx": trained,
        # None when unknown — "cannot verify" is not "does not fit".
        "within_trained": None if not trained else plan["window"] <= trained,
        "limits_error": limits.get("error"),
    }


def _report_warnings(report: dict) -> list[str]:
    """Advisory complaints — reported, never blocking."""
    out: list[str] = []
    if report["within_trained"] is False:
        out.append(
            f"context window {report['window']} exceeds what "
            f"{report['model']} was trained for ({report['trained_ctx']}). "
            f"Quality degrades past the trained length even when the model loads."
        )
    return out


def update(changes: dict) -> dict:
    """Validate every change, then apply all of them or none.

    All-or-nothing because these knobs interact: half-applying a budget change
    can leave floors summing past the total, which degrades every turn until
    someone notices.
    """
    with _lock:
        _snapshot_defaults()
        coerced: dict[str, Any] = {}
        problems: list[str] = []
        for key, value in (changes or {}).items():
            knob = _BY_KEY.get(key)
            if not knob:
                problems.append(f"unknown setting '{key}'")
                continue
            try:
                coerced[key] = _coerce(knob, value)
            except ValueError as e:
                problems.append(str(e))
        if problems:
            raise ValueError("; ".join(problems))

        # BUDGET_TOTAL, PROSE.max_tokens and PROSE.num_ctx are three knobs
        # describing one constraint, so the whole change is judged together —
        # raising the window and the budget in a single save is legal, raising
        # only the budget past the window is not.
        if msg := _plan_problem(context_plan(coerced)):
            raise ValueError(msg)

        for key, value in coerced.items():
            _write(key, value)
        derive_budget()

        overrides = _load_file()
        for key, value in coerced.items():
            if value == _defaults.get(key):
                overrides.pop(key, None)      # back to default => stop storing it
            else:
                overrides[key] = value
        _save_file(overrides)
        return current()


def reset(keys: Optional[list[str]] = None) -> dict:
    """Restore defaults — all of them, or just the named ones."""
    with _lock:
        _snapshot_defaults()
        targets = [k for k in (keys or list(_defaults)) if k in _defaults]
        for key in targets:
            _write(key, _defaults[key])
        overrides = _load_file()
        for key in targets:
            overrides.pop(key, None)
        _save_file(overrides)
        return current()


# ---------------------------------------------------------------- view


def _jsonable(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def current() -> dict:
    """Schema plus live values plus which are non-default — everything the UI needs."""
    _snapshot_defaults()
    overrides = _load_file()
    knobs = []
    for k in KNOBS:
        knobs.append({
            **{kk: vv for kk, vv in k.items() if kk != "help"},
            "help": k["help"],
            "value": _jsonable(_read(k["key"])),
            "default": _jsonable(_defaults.get(k["key"])),
            "overridden": k["key"] in overrides,
        })
    total = sum(f for _, f, _ in config.BUDGET_LAYERS)
    return {
        "groups": GROUPS,
        "knobs": knobs,
        "path": str(_path()),
        "floors_total": total,
        "floors_fit": total <= config.BUDGET_TOTAL,
        "context": context_report(),
    }
