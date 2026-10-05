"""Every tunable in one place.

The algorithms in assemble.py and memory.py read from here and contain no
constants of their own. That is deliberate: the budget splits, decay rates and
salience thresholds cannot be derived up front — they get tuned against real
play. Tuning should mean editing a value here, never editing an algorithm.

Overridable per-deployment via config.local.py (gitignored) or LOOM_* env vars.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- paths

ROOT = Path(__file__).resolve().parent
STORIES_DIR = Path(os.environ.get("LOOM_STORIES", ROOT / "stories"))
STATE_DIR = Path(os.environ.get("LOOM_STATE", ROOT / "state"))
MEDIA_DIR = Path(os.environ.get("LOOM_MEDIA", STATE_DIR / "media"))
DB_PATH = str(STATE_DIR / "loom.db")

# ---------------------------------------------------------------- server

HOST = os.environ.get("LOOM_HOST", "0.0.0.0")
PORT = int(os.environ.get("LOOM_PORT", "8100"))

# ---------------------------------------------------------------- context budget
#
# The arbiter fills layers in priority order. Each gets at least `floor` and at
# most `ceiling` characters. Leftover slack flows to the lowest-priority layers
# (temp memory, then transcript). If the floors cannot all be met, the UI says
# so and recommends a compact.
#
# Characters, not tokens — cheap to measure, and the ratio is stable enough for
# budgeting.

BUDGET_TOTAL = 48_000

# What turns a character budget into a token count. MEASURED at 3.15 on a real
# dialogue-heavy turn, not assumed: the 3.6 this used to claim ran ~14% light, so
# every estimate derived from it understated what the prompt actually costs.
CHARS_PER_TOKEN = 3.15

# Fraction of the usable window the arbiter is allowed to fill. CHARS_PER_TOKEN is
# an average and dialogue-heavy turns run worse than it, so spending the window to
# the last token would truncate on exactly the turns that matter most.
BUDGET_SAFETY = 0.95

# NOTE: BUDGET_TOTAL above is a fallback. It is DERIVED from the context window at
# startup and on every settings save — see settings.derive_budget(). The window is
# the single knob; the character budget follows from it.

# Order matters. This list IS the priority order.
BUDGET_LAYERS = [
    # Three tiers, and they have different growth profiles. Sized from that.
    #
    #   CONCRETE      written once, never grows. The world, the cast, the
    #                 protagonist, the shape of the arc. Small and fixed.
    #   INTERMEDIATE  the story's accumulated state — who stands where with whom,
    #                 what has happened, which goals closed. Grows over a campaign
    #                 and is kept bounded by compaction and memory decay, not by
    #                 being starved. This is the tier that carries continuity.
    #   TRANSCRIPT    raw recent prose. Takes whatever is left, and is ALSO the
    #                 input the utility model reads to update the intermediate
    #                 tier — so starving it degrades the intermediate tier too,
    #                 and the symptom looks like a stupid model rather than a bad
    #                 budget.
    #
    # name              floor   ceiling
    # ---- per-turn direction: tiny, and volatile by nature
    ("director_notes",  0,      2_000),
    # ---- CONCRETE
    ("rules",           1_500,  8_000),   # the world, the concept, the cast
    # ---- INTERMEDIATE (high priority: it outranks the transcript that displaces it)
    ("ledger",          500,    6_000),   # facts you approved; see ledger.py
    # 2,000 was a guess and it was wrong: a real player wrote a 2,500-character
    # power and the whole block was dropped from every single turn.
    ("protagonist",     800,    3_000),   # CONCRETE: who the player chose to be
    ("arc",             400,    1_500),   # CONCRETE: the current act's shape
    # The hinge between the tiers. As the transcript scrolls off, this is what
    # catches it — so it has to be able to hold a campaign, not a scene. 4,000
    # could not, which is what made long stories feel amnesiac.
    ("chapters",        1_500,  14_000),  # synopsis + recent chapter summaries
    ("nudge",           0,      1_600),   # per-turn pacing prompt, volatile
    # Where "they are in love" becomes "it is complicated". Upserted by name, so
    # it is current state rather than history.
    ("state",           400,    4_000),   # relationships + stats, structured
    ("goals",           0,      1_500),
    ("long_memory",     500,    5_000),   # embedding-retrieved durable facts
    ("keyword_notes",   0,      4_000),   # story lore + the session lorebook
    ("temp_memory",     0,      2_500),   # heat-sorted recent salience
    # ---- TRANSCRIPT: the slack absorber. Its ceiling is deliberately larger than
    # any sane budget so that spare space becomes verbatim story rather than going
    # unspent. See TODO.md — it wants to be formally unbounded.
    ("transcript",      8_000,  60_000),
]

# ---------------------------------------------------------------- memory
#
# heat = salience * decay(age_turns) * (1 + log1p(reinforcements))

MEM_DECAY_HALFLIFE_TURNS = 40.0   # temp memory loses half its weight over N turns
MEM_REINFORCE_WEIGHT = 1.0        # multiplier on the log1p(reinforcement) term
MEM_TEMP_FLOOR = 8.0              # heat below this and a temp memory is dropped

# Promotion is the tap that fills long-term storage, and it was left open too far.
# At threshold 75 / min-reinforced 1, a 132-turn session accumulated 497 long-term
# memories, 416 of them promoted temps referenced exactly once. Eight are retrieved
# per turn, so the other 489 were pure retrieval noise: the more the store holds,
# the worse cosine similarity discriminates. Durable world facts now belong in the
# lorebook, which fires deterministically on a keyword instead of competing in a
# similarity search, so promotion can afford to be rare.
MEM_PROMOTE_THRESHOLD = 110.0     # heat above this and a temp memory may graduate
MEM_PROMOTE_MIN_REINFORCED = 2    # ...and only after being referenced twice.
MEM_TEMP_MAX_ROWS = 300           # hard cap on stored temp memories per session
MEM_LONG_MAX_ROWS = 250           # cap on long-term rows; see prune_long() for what goes

MEM_RETRIEVE_K = 8                # long-term memories fetched per turn
MEM_RETRIEVE_MIN_SIMILARITY = 0.35
MEM_QUERY_TURNS = 3               # how many recent turns form the retrieval query

KEYWORD_SCAN_TURNS = 4            # how far back keyword triggers look
KEYWORD_MAX_NOTES = 10            # cap on simultaneously injected notes

# ---------------------------------------------------------------- models
#
# Two tiers. Prose is what you read. Utility does the structured JSON delta
# after each turn — extraction, scoring, relationships, goals, stats.

PROSE = {
    "provider": os.environ.get("LOOM_PROSE_PROVIDER", "ollama"),  # ollama|openai_compat
    # No default worth shipping: the right model is whatever this host has
    # pulled. Empty means "unset" and the model picker lists what ollama holds.
    "model": os.environ.get("LOOM_PROSE_MODEL", ""),
    # Only read when provider is openai_compat, and required then — brain.prose
    # raises without it. Its absence is why the LOCAL spec below was unreachable:
    # the provider could be selected in the UI and then failed every turn, so a
    # local model was never actually a switch you could throw.
    "url": os.environ.get("LOOM_PROSE_URL", ""),
    # 1200 was tight for the "three to five paragraphs" the stories ask for —
    # a full five-paragraph turn runs 2,500-2,800 characters, which is most of
    # that budget. Headroom costs nothing when unused; output is billed on what
    # is actually generated.
    #
    # 2000 was in turn too tight for a long-form story that asks for whole
    # scenes: a measured turn of The Mercy stopped at 5,372 characters, exactly
    # mid-sentence, because dialogue-heavy prose tokenises at roughly 2.7
    # characters per token. A reasoning model's scratchpad counts against this
    # ceiling too, before a word is written. A cutoff is now recoverable either
    # way — see RESUME_TURN_TEXT — but it should be rare.
    "max_tokens": 4000,
    "temperature": 1.0,
    # ---- local sampler controls, sent ONLY by the "ollama" provider.
    #
    # ollama's OpenAI-compat /v1 surface silently accepts only temperature and
    # top_p — it has no min_p
    # and no top_k, which are exactly the two knobs the Mistral-Nemo finetunes
    # are tuned around. That gap is why the native provider exists.
    #
    # Defaults here are "off" so the provider is a faithful passthrough until
    # somebody deliberately tunes it. 0 disables top_k and min_p; 1.0 disables
    # repeat_penalty.
    "top_p": 1.0,
    "top_k": 0,
    "min_p": 0.0,
    "repeat_penalty": 1.0,
    "repeat_last_n": 64,
    # The one that bites. ollama does not error when the prompt exceeds the
    # context it loaded the model with — it silently drops the front of it, so a
    # story looks like it developed amnesia and the model takes the blame. This
    # is sent per request so the budget and the context window cannot drift
    # apart. It must cover prompt AND generation, and every token of it is KV
    # cache resident in VRAM: at 160KB/token on a 12B, 8192 is ~1.3GB.
    "num_ctx": 8192,
}

UTILITY = {
    "provider": os.environ.get("LOOM_UTILITY_PROVIDER", "ollama"),
    "model": os.environ.get("LOOM_UTILITY_MODEL", ""),
    "max_tokens": 4000,
    "temperature": 0.3,
}

# Optional third tier: the desktop's KoboldCpp. Set url to enable.
LOCAL = {
    "provider": "openai_compat",
    "url": os.environ.get("LOOM_LOCAL_URL", ""),   # e.g. http://192.168.5.234:5001/v1
    "model": "local",
    "max_tokens": 1200,
    "temperature": 1.0,
}

# Providers fail transiently, and a fault can arrive inside a 200 as a mid-stream
# `error` event rather than an HTTP status. Retried only while nothing has been
# shown, so a partly-streamed reply is never duplicated. See MEASUREMENTS.md.
PROSE_RETRIES = 4
PROSE_RETRY_BACKOFF = 2.0     # seconds; doubles each attempt (2, 4, 8, 16)

# ---------------------------------------------------------------- LEDGER
#
# The human-curated fact list. A separate system from memories and the lorebook
# on purpose -- ledger.py's header explains why. Nothing reaches the model here
# without someone approving it.

LEDGER_ENABLED = True

# Fire the review BEFORE the window is full. Past the model's context ollama
# silently drops the front of the prompt, so a reactive trigger loses the oldest
# history and the story just seems to forget. 0.85 leaves room for one more turn.
LEDGER_TRIGGER_FILL = 0.85

LEDGER_PROPOSE_MAX = 6        # facts offered per review; you accept or bin each
LEDGER_MAX_ACTIVE = 80        # hard cap on what can be in the prompt at once
LEDGER_SOURCE_CHARS = 12_000  # how much transcript the proposal call reads

# None => use the PROSE model. That is the point: a second model would evict the
# first from a 12GB card and cost an 11.8s reload on every turn afterwards.
LEDGER_SPEC = None

# ---------------------------------------------------------------- embeddings

# Native ollama, for the "ollama" prose provider. No /v1 — that is the
# OpenAI-compat surface, and it cannot carry min_p or top_k.
# Defaults assume ollama on this host, which is what a native install has.
# A containerised loom reaches it by compose service name instead and sets
# these in the environment — see the Dockerfile. Getting this backwards is
# worth avoiding: an unresolvable hostname fails as "cannot reach", which
# reads as "ollama is down" rather than "that address is wrong".
OLLAMA_URL = os.environ.get("LOOM_OLLAMA_URL", "http://127.0.0.1:11434")

EMBED_URL = os.environ.get("LOOM_EMBED_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("LOOM_EMBED_MODEL", "nomic-embed-text")
EMBED_ENABLED = True   # off => long-term memory falls back to recency ordering

# ---------------------------------------------------------------- images

IMAGES_ENABLED = os.environ.get("LOOM_IMAGES", "1") == "1"
# Same reasoning as OLLAMA_URL: default to this host. A containerised loom
# reaches ComfyUI on the host via the bridge gateway, whose address is
# deployment-specific, so that belongs in compose rather than baked in here.
COMFY_URL = os.environ.get("LOOM_COMFY_URL", "http://127.0.0.1:8188")
COMFY_CHECKPOINT = "waiIllustrious.safetensors"

IMG_PORTRAIT = {"width": 832, "height": 1216, "steps": 30, "cfg": 6.0}
IMG_SCENE = {"width": 1216, "height": 832, "steps": 28, "cfg": 6.0}

IMG_QUALITY_TAGS = "masterpiece, best quality, amazing quality, very aesthetic, highly detailed"
IMG_NEGATIVE = (
    "bad quality, worst quality, worst detail, low quality, lowres, jpeg artifacts, "
    "sketch, censored, watermark, signature, text, logo, extra digits, bad hands, "
    "bad anatomy, deformed, blurry, multiple views"
)

# Only characters defined in the story's cast get a portrait automatically.
# Everyone else is one click away in the cast panel, which is the right default:
# a named innkeeper is not a character you need a picture of.
PORTRAIT_CAST_ONLY = True

SCENE_MIN_TURN_GAP = 6      # don't render scene art more often than every N turns
IMG_MAX_CONCURRENT = 1      # one render at a time; the 4070 has 12GB

# ---------------------------------------------------------------- behaviour

RECENT_TURNS_DEFAULT = 30   # transcript window before the budget trims it

# Some APIs reject a conversation that does not end with a user message, and a
# narrative-mode story has no player typing. It is also what the depth channel
# attaches to — with no user turn, the director's note silently degrades into the
# system prompt (where it is measured not to work) and an overdue pacing beat is
# dropped entirely. See MEASUREMENTS.md.
CONTINUE_TURN_TEXT = "Continue."

# What the resume button sends. Deliberately not CONTINUE_TURN_TEXT: "Continue."
# after a reply that stopped mid-sentence reads as an invitation to start the
# next beat, and the model duly began a new paragraph, leaving the broken
# sentence broken forever. This says what actually happened.
RESUME_TURN_TEXT = (
    "Your previous reply was cut off mid-flow because it reached the length "
    "limit, not because the beat ended. Resume it from exactly the character it "
    "stopped at and carry the scene to its natural close. Do not repeat, "
    "re-summarise or restart any part of what you already wrote, and do not "
    "acknowledge this instruction — begin mid-sentence if that is where it "
    "broke off, and match the voice and tense of the text above.\n\n"
    # Measured: the break landed between "the best" and "thing", the model
    # resumed with "thing", and the join read "bestthing". Nothing downstream can
    # tell that apart from a genuine mid-word cut, so the model has to say which
    # it was — and the only way it can is by emitting the space itself.
    "Your reply is concatenated onto the previous text with nothing inserted "
    "between them. If the break fell between two words, start your reply with a "
    "single leading space. If it fell inside a word, start with the rest of that "
    "word and no space."
)

# ---------------------------------------------------------------- chapters
#
# A chapter is a compaction point: the turns it covers are replaced in the prompt
# by their summary, which is what keeps a long story inside a fixed budget.
#
# This used to require the author to review each break before play could continue.
# It worked, but the gate was the wrong shape — being stopped mid-scene to edit a
# recap is a worse interruption than the drift it prevents. Compaction now happens
# on its own every CHAPTER_EVERY_TURNS turns and simply tells you it happened; the
# summary is editable afterwards, in the chapters panel, whenever you feel like it.

CHAPTERS_ENABLED = True
CHAPTER_EVERY_TURNS = 20     # auto-compact this many turns into a chapter
CHAPTER_MIN_TURNS = 8        # never compact sooner than this, even under pressure
CHAPTER_OVERLAP_MSGS = 3     # exchanges carried past a break so the voice doesn't cut
CHAPTER_VERBATIM = 3         # recent chapter summaries kept in full

# ---------------------------------------------------------------- lorebook
#
# The lorebook is the durable half of memory, and the only half you ever see. An
# entry fires when one of its keywords appears in the recent transcript — the same
# mechanism as a story's authored notes, because that is exactly what it is: notes
# the story earned during play rather than ones written up front.
#
# Suggestions are drafted at each chapter break, from that chapter's material, and
# wait for a yes or no. Nothing is added to the prompt without a click.

LOREBOOK_ENABLED = True
LOREBOOK_SUGGEST_MAX = 3      # entries proposed per chapter break
LOREBOOK_MIN_MENTIONS = 2     # a candidate must recur across this many turns

# ---------------------------------------------------------------- goals
#
# One objective at a time. Observed in comparable systems and confirmed in play:
# a story with a single outstanding thing to do reads as a story, and one with six
# reads as a task list. Extra objectives still get captured — they wait in the
# backlog, and you promote one when the current one is done.
#
# The nudge is the other half. Left alone the model either forgets the objective
# entirely or lurches the plot at it. So after a few turns of not coming up, a soft
# reminder is added to the prompt: let a character mention it in passing, then let
# the scene continue wherever the player takes it.

GOALS_MAX_ACTIVE = 1             # objectives active at once; the rest wait in backlog
GOALS_NEED_CONFIRMATION = False  # with one slot, a goal takes it rather than queueing
GOAL_NUDGE_ENABLED = True
GOAL_NUDGE_AFTER = 3             # turns the goal may go unmentioned before a nudge
GOAL_NUDGE_ESCALATE = 8          # ...and after this many, a slightly firmer one

UTILITY_ENABLED = True      # off => pure chat, no memory pipeline (useful for A/B)

# ---------------------------------------------------------------- overrides

try:  # pragma: no cover - deployment-local overrides
    from config_local import *  # type: ignore  # noqa: F401,F403
except ImportError:
    pass
