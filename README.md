# loom

An interactive-narrative engine. A model narrates, a player types, and a harness
around the model decides what it is allowed to remember, what it is reminded of,
and when. Roughly 200KB of Python and a vanilla-JS frontend, no dependencies
beyond `pyyaml`.

Everything runs on one machine and nothing leaves it: prose and embeddings from
a local ollama or any OpenAI-compatible server, images from a local
stable-diffusion.cpp. There are no API keys to configure because there is nowhere
to send them.

**Writing stories:** [`stories/README.md`](stories/README.md).
**What is measured, and what it cost:** [`MEASUREMENTS.md`](MEASUREMENTS.md).

---

## The idea in one paragraph

Every turn, exactly one system decides what earns a slot in the context window,
and exactly one model call updates the world state. That is the whole thesis.
The predecessor build was SillyTavern with four extensions, each firing its own
model call with no shared ordering and no shared budget; the result was
unpredictable cost, unpredictable prompts, and memory that was neither. Here the
arbiter (`assemble.py`) is a single pure function over a fixed character budget,
and the memory pipeline (`memory.py`) makes one structured call per turn that
returns one validated JSON delta. Everything else — decay, similarity, heat,
keyword matching, pacing counters — is plain arithmetic in code, where it can be
tuned, measured and reasoned about.

---

## Running it

**Directly:**

```bash
python3 run.py          # needs pyyaml; LOOM_STORIES and LOOM_STATE
                        # default to ./stories and ./state
```

**As a systemd `--user` unit**, which is how it is meant to run — set
`PYTHONUNBUFFERED=1` in the unit or every diagnostic loom prints is buffered away
where you cannot see it:

```bash
systemctl --user restart loom
journalctl --user -u loom -f
```

**Under Docker**, with `compose up -d --build loom`. Code changes need the rebuild
because `COPY *.py /app/` bakes them in.

**What needs what:**

| Changed | Native | Docker |
|---|---|---|
| `*.py` | restart the unit | **rebuild** — `COPY *.py /app/` bakes them in |
| `static/*` | browser refresh | **rebuild**, though served `no-cache` |
| `stories/*` | nothing — hot-reloaded on mtime | nothing — bind-mounted |
| a settings knob | nothing — edited live in the UI, persisted to `state/settings.json` | same |

A first install has neither a prose nor a utility model configured — see
[`TODO.md`](TODO.md) §8, which is the gap where a setup guide belongs.

### What it talks to

| | Environment variable | Default |
|---|---|---|
| Port | `LOOM_PORT` / `LOOM_HOST` | `8100` / `127.0.0.1` |
| State | `LOOM_STATE` | `./state` — SQLite, media, `settings.json` |
| Stories | `LOOM_STORIES` | `./stories` |
| Prose and utility models | `LOOM_OLLAMA_URL` | `http://127.0.0.1:11434` |
| Embeddings | `LOOM_EMBED_URL` | same host, `nomic-embed-text` |
| Images | `LOOM_IMAGE_URL` | `http://127.0.0.1:1234` |
| Healthcheck | — | `GET /api/stories` |

Every one of these is reached over HTTP, never the filesystem, so each can live in
its own container, on its own port, or not exist at all. Images are optional: with
no image server reachable, loom runs with text and no portraits.

---

## Module map

| File | Lines | Responsibility |
|---|---|---|
| `run.py` | 11 | entry point |
| `config.py` | ~460 | **every** tunable constant, plus paths and provider specs |
| `server.py` | ~945 | `ThreadingHTTPServer`, all routes, the turn loop |
| `assemble.py` | ~720 | the context-budget arbiter — builds the prompt |
| `memory.py` | ~730 | heat, decay, retrieval, keyword matching, nudges, the delta |
| `store.py` | ~1190 | SQLite. All of it. No ORM, no migration framework |
| `brain.py` | ~550 | model routing: streaming prose, structured utility, embeddings |
| `story.py` | ~770 | story files: validate, load, hot-reload, save, duplicate |
| `settings.py` | ~680 | which constants are live-editable, and persistence |
| `images.py` | ~360 | the render queue, entirely off the turn loop |
| `rulebuilder.py` | ~340 | twelve questions that add up to a story's rules |
| `advisor.py` | ~320 | plain-language requests to a proposed settings change |
| `import_history.py` | ~310 | one-off: a story written elsewhere becomes a session |
| `lorebook.py` | ~250 | durable keyword-fired notes, and suggesting new ones |
| `chapters.py` | ~240 | compaction — turns become summaries |
| `ledger.py` | ~210 | facts the player approved, which outrank the prose |
| `static/` | ~4500 | vanilla JS/CSS/HTML. No build step, no framework |

Dependency direction is strictly downward: `server` → `assemble`/`memory`/
`chapters`/`images` → `store`/`brain` → `config`. `story.py` is a leaf that only
knows `config`. There are two deliberate local imports inside functions
(`assemble` ↔ `memory`) to break a cycle around the nudges.

**`config.py` is load-bearing.** The algorithms contain no constants of their
own — every threshold, floor, ceiling, half-life and cap lives there. That is on
purpose: these numbers cannot be derived up front, they get tuned against real
play, and tuning should mean editing a value, never editing an algorithm. If you
find yourself typing a magic number into `assemble.py` or `memory.py`, put it in
`config.py` instead and consider adding a knob for it in `settings.py`.

---

## The turn loop

`server.run_turn` (`server.py:125`) is the product, in about forty lines:

```
POST /api/send  →  SSE stream
  1. bump turn, store the user message
  2. gather — all local arithmetic, no model calls:
       memory.match_keywords()   story notes + lorebook, ranked by recency
       memory.hot_temp()         heat-sorted recent salience
       memory.retrieve_long()    embedding similarity over durable facts
  3. assemble.build()            → system prompt + messages, within budget
       emit "context"            (the budget meter updates before generation)
  4. brain.prose()               → streams, emit "token" per chunk
       emit "thinking"/"retrying" when a reasoning model or a flaky provider needs it
  5. store the reply, emit "message"
  6. memory.digest()             ONE utility call → one JSON delta → applied
  7. images.request_*()          queued, never awaited
  8. chapters.compact() if due   costs 1-2 utility calls, invisible to the player
  9. emit "state" (full panel refresh), emit "done"
```

Everything before step 4 is arithmetic. Everything after it happens with the
reply already on screen — which is why compaction, image generation and the
memory digest can afford to be slow.

**SSE events:** `context`, `token`, `thinking`, `retrying`, `message`,
`warn`, `chapter`, `state`, `done`, `error`.

**A reply that hits the length limit is unfinished, not over.** The stream ends
identically either way, so a beat cut off mid-sentence used to be stored as a
finished one and digested as if the scene had ended. `brain.prose` now reports
the provider's stop reason (`max_tokens`, or OpenAI's `finish_reason: length`),
the message is flagged `truncated`, and `POST /api/continue` resumes it: same
turn, no counter bumped, no new message — the output is appended to the message
it is finishing, so the transcript, the chapter clock and the budget arbiter all
still see one reply. The resume sends `config.RESUME_TURN_TEXT` in place of
`CONTINUE_TURN_TEXT`, because "Continue." after a broken sentence reads as an
invitation to start the *next* beat, and the model duly did.

**Every packet ends with a user message**, because some APIs reject one that
does not.
`assemble.build` appends `config.CONTINUE_TURN_TEXT` when the transcript does not
already end in a user turn. That is what makes narrative mode possible at all,
and it also repaired a quieter bug: with no user turn to attach to, the depth
channel had nowhere to go, so the director's note silently degraded into the
system prompt and an overdue pacing beat was dropped entirely.

**A dropped browser does not cost you the turn.** The emit closure flips a flag
on a dead socket and stops pushing; generation runs to completion, the reply and
the delta are stored, and the turn is simply there on the next page load. This
used to let the write error propagate out of `brain.prose()` and abandon a reply
that had already been generated and paid for — locking a phone was enough to do
it (`server._stream_turn`).

---

## The context budget arbiter

`assemble.py`. The part that makes the whole thing work.

**Model.** Each layer produces an *ordered list of items*, most important first.
The arbiter walks layers in priority order and takes the longest prefix of each
that fits its allocation. Trimming a layer therefore means dropping its least
important items — never truncating text mid-sentence.

**Allocation**, in `allocate()`:

1. Reserve every layer's floor (or its real size, whichever is smaller).
2. Walk layers in priority order, growing each from its floor toward
   `min(ceiling, real size)` out of whatever budget remains.
3. Realise each layer, **carrying unused allowance downstream** — a layer whose
   smallest item does not fit would otherwise sit on budget it cannot spend
   while a lower-priority layer starves. `transcript` is last, so it absorbs the
   slack.

**Characters, not tokens.** Cheap to measure, and the ratio is stable enough —
3.15 chars/token, measured against this engine's own prompts rather than taken from
a rule of thumb.

**One knob sets all of it.** The character budget is derived from `PROSE.num_ctx`:
the reply ceiling is reserved out of the context window and the remainder becomes
what the arbiter has to spend. Three numbers that had to be kept in agreement by
hand are now one that cannot disagree with itself, and the layer ceilings scale with
it — raising the window is otherwise pointless for every layer except the transcript,
which is the only one that absorbs slack.

**The layers**, in priority order (`config.BUDGET_LAYERS`):

| Layer | Floor | Ceiling | Contents |
|---|---|---|---|
| `director_notes` | 0 | 2,000 | the author speaking directly; never trimmed |
| `rules` | 1,500 | 11,000 | story `rules` + `details` + intro `opening_scene` |
| `protagonist` | 800 | 4,500 | who the player chose to be |
| `arc` | 400 | 2,500 | the current act, for a story that declares one |
| `chapters` | 800 | 6,500 | synopsis + recent chapter summaries |
| `nudge` | 300 | 1,600 | the soft goal reminder, the soft action prompt |
| `state` | 300 | 2,500 | stats + relationship summaries |
| `goals` | 0 | 1,000 | the active objective |
| `long_memory` | 0 | 5,000 | embedding-retrieved durable facts |
| `keyword_notes` | 0 | 12,000 | story lore + session lorebook |
| `temp_memory` | 0 | 6,000 | heat-sorted recent salience |
| `transcript` | 8,000 | 24,000 | recent messages; gets the slack |

**Render order is deliberately not priority order** (`_RENDER_ORDER`,
`assemble.py:333`). Recency *inside* the prompt carries weight, so `goals` and
`nudge` are rendered late, just before the end of the system block.

### Depth injection

Two things do **not** go in the system prompt, and this is measured, not
theoretical:

- **The director's note.** Placing it in the system block does not work. The
  transcript sits in the messages array, after the system block and immediately
  before generation; several thousand characters of vivid recent scene reliably
  outrank one line of system text. So it is appended to the *last user message*,
  closer to the generation point than anything else in the packet.
- **An overdue action beat.** Same channel, same reason: with the identical text
  in the system block the model produced five consecutive peaceful turns past
  the cadence.

Both attach to the last user turn, director's note last — it is the author
speaking and outranks a pacing timer.

### `{{token}}` substitution

Resolved once, at the very end, on the finished system prompt. Doing it there
rather than per-layer means a lore note or a story rule can reference the player
by name without every producer of text having to know about substitution
(`story.subst`, `story.pronouns`).

---

## Memory

Four layers, **one model call**.

| | |
|---|---|
| `long` | durable facts, retrieved by cosine similarity over Ollama embeddings |
| `temp` | recent salience, heat-sorted and decaying |
| `rels` | how each character sees the player, as a running summary |
| `goals` | one active objective, the rest in a backlog |

**Heat** = `salience × 0.5^(age/halflife) × (1 + w·log1p(reinforcements))`.
Below `MEM_TEMP_FLOOR` (8) a temp memory is dropped. Above
`MEM_PROMOTE_THRESHOLD` (110) **and** referenced at least twice, it graduates to
long-term.

Those promotion numbers were tightened after a real failure: at threshold 75 /
min-reinforced 1, a 132-turn session accumulated 497 long-term memories, 416 of
them referenced exactly once. Retrieval pulls a fixed 8 per turn, so the other
489 were pure noise — the more the store holds, the worse cosine similarity
discriminates. `prune_long()` now caps it at 250, dropping fewest-referenced
first. **Durable world facts belong in the lorebook**, where a keyword fires
them deterministically instead of competing in a similarity search.

**The delta.** `memory.digest()` makes one `brain.utility()` call with
`DELTA_SCHEMA` (`memory.py:318`) and gets back a validated object:
`new_memories`, `reinforced`, `relationships`, `goals_add`, `goals_complete`,
`goal_touched`, `action_beat`, `stats`, `new_characters`, `scene_changed`,
`scene_prompt`. `apply_delta()` writes it, and is separated from `digest()` so
it can be tested and replayed without a model call.

Three filters sit between the model and the store, each one built after
something specific went wrong:

- `_clean_goals` — rejects questions ("Determine…", "Investigate…") and caps the
  count. It used to run *after* the insert loop, mutating a list nothing read
  again, which is how sixteen open objectives happened.
- `_clean_character_names` — proper names only. Unfiltered, a two-hander's cast
  panel filled with "the boy's mother", "The clerk in the knitted cap", and the
  name of the world itself.
- Stat deltas are matched against the story's declared keys and clamped.

**Embeddings degrade, they do not break.** `brain.embed()` returns `[]` per text
on failure and `retrieve_long()` falls back to recency ordering, so a dead
Ollama costs relevance, not the turn.

---

## Pacing: goals and action

Both nudges exist for the same structural reason. The model is handed the
current chapter's transcript with **no turn numbers on it** — often under ten
messages — so any instruction phrased as a frequency ("mention the goal
occasionally", "combat should be common") delegates a measurement it is
physically unable to make. The harness counts; the prompt only says what to do
once the count is up.

**Goals.** One active objective by default (`GOALS_MAX_ACTIVE = 1`). Observed in
play: a story with a single outstanding thing to do reads as a story, and one
with six reads as a task list. Extras wait in a backlog and are promoted newest-
first when a slot frees. After `GOAL_NUDGE_AFTER` (3) turns without the story
engaging with it, a soft reminder enters the prompt; after
`GOAL_NUDGE_ESCALATE` (8), a firmer one. The counter resets on `goal_touched`
from the digest.

The soft template originally shipped with an escape clause — *"if the player is
mid-conversation, let them finish first"* — which fired every single time,
because conversation is the baseline register. It is gone. An instruction the
model can always decline is not a cadence, it is a suggestion.

**Action.** Driven by the story's `pacing.action_every`. Soft nudge at the
interval, hard demand at `interval + max(2, interval//2)`, the hard one injected
at depth. `action_beat` in the digest resets the clock.

⚠️ **No story currently sets `pacing`, and the templates are Seiran-flavoured** —
`_ACTION_NUDGE_FIRM` says *"This is a city with Quirks in it"*. Generalising
those three templates (`memory.py:180-226`, `action_demand`) is the obvious
prerequisite to using pacing anywhere else.

---

## Narrative mode and the arc

A story declares `mode: narrative` when there is no player in it — the model
writes every character and the reader watches. Three things change:

1. **The composer becomes the director's channel.** What the reader types is
   still stored as a user message, but the story's `rules` establish that a line
   from outside is the author speaking, not a character. The UI labels it
   *Direction* and sets it apart from the prose. Empty sends are the normal case.
2. **The extractor stops asking about "the player."** `memory._digest_prompt`
   swaps its player-centric wording — relationships become how the leads regard
   *each other*, goals become what the leads have committed to, and the exchange
   is presented as a passage rather than a PLAYER/NARRATOR pair. Left unchanged
   it attributes the leads' feelings to a "you" that is nowhere in the prose and
   the relationship layer fills with nonsense.
3. **`_items_state` renames its heading**, because there is no "you" to be seen
   by anyone.

**The arc is the part that matters.** Loom otherwise has no concept of position:
goals are tactical, chapters are compaction, the pacing counter is cadence.
Nothing knows how far through a story is, and nothing knows a story can be over —
so a story left alone wanders pleasantly forever and never arrives.

```yaml
arc:
  advance: chapter        # chapter | manual
  acts:
    - id: convenience
      name: Two Men Walking
      chapters: 1         # how many chapters this act spans
      shape: |
        What this act is for.
```

`assemble.current_act` resolves it. Chapters are the clock — they close every
`CHAPTER_EVERY_TURNS` turns, which makes act length a predictable number of turns
without the author reasoning about the budget. A per-session manual override in
`meta` (`arc:<session_id>`, set via `POST /api/arc`) wins outright, because the
counter only knows that time has passed while the reader knows whether the beat
actually landed.

`_items_arc` injects the act, its number out of the total, and — in the last act
— a plain statement that this is the last one. That final line is what lets the
model land an ending instead of continuing.

---

## Chapters and the lorebook

**Chapters are compaction, not checkpoints.** A chapter break replaces a span of
transcript with a summary of it, which is what lets a 132-turn story stay inside
a 48,000-character window. Without it a long story loses its early material by
construction — measured on a 68-turn session, 39 of 60 recent messages were
dropped from every turn.

It happens automatically every `CHAPTER_EVERY_TURNS` (20), *after* the reply is
on screen, and simply says so. It used to block play until the author reviewed
each break; the reasoning (only a human knows which planted detail will matter)
was right, but the timing was indefensible — being stopped mid-scene to edit a
recap is a worse interruption than the drift it prevents, and a review you are
forced into is not a careful one. Summaries stay editable in the chapters panel,
which is where that knowledge actually gets applied.

`_items_chapters` orders by **priority, not chronology**: synopsis first (short,
covers everything old), then the most recent chapter, then backwards. Listing
chronologically meant a squeeze discarded the newest summary — the one covering
the turns that had just fallen out of the transcript.

Chapters past `CHAPTER_VERBATIM` (3) are folded into a running synopsis by
`refold_synopsis()`.

**The lorebook** is the durable half of memory and the only half you ever see.
Entries fire *deterministically* on a keyword, which is the whole point: mention
"Diamond Dogs" and that entry is in the prompt whatever else is competing for
retrieval. It is the same mechanism and the same dict shape as a story's
authored `keywords`, so both come down one layer, ranked against each other by
recency of mention rather than by which file they live in
(`lorebook.notes` → `memory.match_keywords`).

Suggestions are drafted at each chapter break from that chapter's material,
pre-filtered locally by `recurring_terms()` (a capitalised term must appear in
at least `LOREBOOK_MIN_MENTIONS` distinct turns), capped at 3, and **inert until
accepted** — a suggested entry contributes nothing to the prompt. Dismissed ones
count as known, so nothing is re-offered every twenty turns for the life of the
story.

---

## Model routing

`brain.py`. Two tiers, both stdlib-only (`urllib`, no `requests`).

**`prose()`** streams narration. loom is local-only: the providers are `ollama`
(native API) and `openai_compat` (any OpenAI-shaped `/v1`, including llama.cpp's
server and KoboldCpp). `ollama` is the richer path — it is the only one that can
send `min_p`, `top_k` and `num_ctx`, which are exactly the knobs the Mistral
finetunes are tuned around. `openai_compat` reaches the same models through `/v1`
and silently drops all three.

Reasoning models stream a scratchpad before any visible text, sometimes for over
a minute with long silent gaps. Those are surfaced as `thinking` events rather
than dropped, so the reader sees progress and the SSE socket keeps moving.

**Retries** (`PROSE_RETRIES = 4`, backoff 2/4/8/16s) fire only while **nothing
has been shown**. Once tokens have reached the browser a retry would duplicate
half a reply. A provider fault can arrive inside a 200 as a mid-stream `error`
event rather than an HTTP status — ignoring that turned a transient fault into a
silently empty turn.

**`utility()`** makes one structured call using provider-native structured
output: ollama constrains generation with `format`, and `openai_compat` is sent
strict `json_schema`, falling back to `json_object` only when the server rejects
the parameter outright. Not `json_object` by preference — it honours "give me
JSON" and nothing else, and has been measured silently dropping half a delta. The
memory pipeline therefore never parses JSON out of prose and never retries on
malformed output.

Pointing `utility` at the **same** model as `prose` is usually right: ollama keeps
one model resident, so two different models mean multi-gigabyte swaps between the
lanes on every single turn.

**`models_list()` / `probe()`.** A listing says what exists; only a real request
says what is *answering*. `probe()` distinguishes three outcomes — `up`, `busy`
(rate limit, timeout: come back later) and `unusable` (400: this model will never
serve this request shape). Model names are **advisory-checked, never gated**,
because a provider's listing and the name that actually serves need not match:
ollama tags drift from what a Modelfile registers.

Every measurement behind the decisions above is written up in
[`MEASUREMENTS.md`](MEASUREMENTS.md), including findings from the frontier
providers loom used to support — the APIs are gone, the behaviours they revealed
are still what this code guards against.

---

## Persistence

One SQLite file at `/state/loom.db`, WAL mode. Tables created on demand by
`_SCHEMA`; new columns added defensively via `_ADDED_COLUMNS` so an older
database keeps working. No ORM, no migration framework.

`sessions` · `messages` · `memories` · `relationships` · `goals` · `stats` ·
`notes` · `media` · `chapters` · `lorebook` · `synopsis` · `protagonist` ·
`meta` · ~~`threads`~~ (retired, kept unread so the rows are not destroyed).

**Turn numbers, not wall-clock.** Decay is measured in turns because a story's
pace has nothing to do with how long you left the tab open.

**`rewind_to()` is the interesting one.** Deleting messages does not undo turns:
every turn also writes memories, goals, relationships, stat changes, scene art, a
chapter boundary and a pacing marker, and all of that outlives the text it came
from. Measured on a ten-turn session cut in half, the transcript lost five turns
and the prompt still carried ten long-term memories, ten goals and a
relationship summary describing a conversation that no longer existed. So the
cut is **by turn, not by row**, in one `BEGIN IMMEDIATE` transaction, with two
deliberate exceptions: stats (only the current value is stored, so there is
nothing to reverse) and the lorebook (the one layer the author curates by hand).

---

## Images

`images.py`. A single background worker drains a queue; nothing here is ever
awaited by a reply. A render takes 20–40s on a discrete GPU and about 43s on a
Strix Halo iGPU, and must never be in the critical path.

loom speaks the **AUTOMATIC1111 `/sdapi/v1/txt2img` shape**, which
stable-diffusion.cpp's `sd-server` implements, as do A1111, Forge and reForge — so
the backend is a choice rather than a dependency. One request, one base64 PNG back.
A story's own `checkpoint` field is currently accepted and ignored: an `sd-server`
process loads one model at startup and this API cannot switch it per request.

**Portraits are chosen, not just generated.** Saving a story offers to draw its
cast, one row per character, each re-rollable on the spot; the chosen image is
copied into `stories/<id>/portraits/` so it belongs to the story and every
playthrough sees the same faces. The player's own portrait is drawn the same way at
the end of character creation, and belongs to the session instead, because who you
are can differ between playthroughs while the cast does not. Filenames carry a
timestamp: a re-roll that reused the name served a year-old cached image to
everything that had already loaded it.

Portraits are cached per character name forever and keyed by the **canonical**
name — aliases are resolved *before* the cache check, or the same character gets
rendered twice. Scene art is debounced by `SCENE_MIN_TURN_GAP` (6).
`PORTRAIT_CAST_ONLY` limits automatic portraits to characters the story defines;
without it, a two-hander accumulated eighteen portraits of innkeepers rendered
from no description.

Filenames are folded to ASCII (`_slug`). `str.isalnum()` is true for accented
letters, so "Ōbayashi" survived into a filename verbatim, which is legal on disk
and legal over HTTP but only works if every hop percent-decodes identically —
one of them did not. `server._media` unquotes before resolving, so existing
files still serve.

---

## Settings

`config.py` holds defaults and stays the source of truth for what a knob *is*.
`settings.py` adds a declarative description of which values are safe to change
at runtime, plus persistence to `state/settings.json`.

**The mechanism is one line of discipline:** every consumer reads `config.X` at
call time rather than capturing it at import, so writing a new value onto the
config module takes effect on the very next turn. Overrides are stored sparsely
— only what differs from the default — so upgrading `config.py`'s defaults still
reaches anything the user never touched.

`CORE_RULES` lives here too, which is the one knob that is prose rather than a
number: the engine's own contract, shared by every story and rendered ahead of each
story's own rules. It covers what makes loom loom rather than what makes a story a
story — the player's character is theirs alone, any input format is accepted without
comment, narration is clean prose, one thing happens at a time and the player ends
scenes, and a weighty spoken line gets its own attributed paragraph. A story file
then carries only what is actually its own: tone, world, cast.

Deliberately absent: paths and host/port. Those are deployment facts, not tuning.

`update()` is all-or-nothing, because these knobs interact — half-applying a
budget change can leave floors summing past the total, which degrades every turn
until someone notices.

---

## Importing a story written somewhere else

`import_history.py` turns a file of finished prose into a session. It exists
because there is no field the prose can go in: a real import ran 194,893
characters against a budget of about 48,000, four times the whole window. So it does to imported text exactly what the engine does to text it
writes itself — cuts it into turns, closes chapters over them, summarises those,
folds the aged ones into the synopsis, and seeds long-term memory — and the
result is an ordinary session rather than a special case.

```bash
docker cp import_history.py loom:/app/ && docker exec loom \
  python3 /app/import_history.py the-mercy /stories/the-mercy/export.md \
  --turn-chars 3000 --carry-from 12 --carry-after 578 --dry-run
```

**Always `--dry-run` first**, and tune `--turn-chars` from what it prints. The
number that matters is the size of the block left open: it becomes the live
transcript, so sizing it near the transcript ceiling (24,000) means nothing is
orphaned. Too large and the middle of it sits in neither a summary nor the
prompt until the chapter eventually compacts; too small and there is no verbatim
text to carry the voice. On the real import, 3,500 left 60,000 characters open
and 3,000 left 23,054 — the same file, one parameter apart.

The input is assumed to be continuous prose with paragraph breaks and nothing
else. It does not look for scene boundaries: tried against the real file, a
heuristic found 521 candidates in 1,608 paragraphs, because this kind of writing
uses one-line beats constantly. Turns are cut on paragraph boundaries instead,
and since the model summarising a chapter reads the whole span, a boundary
landing mid-scene costs the summary nothing.

`--carry-from` appends an existing session's transcript to the end, for when
play already started past where the import ends. The intro's prologue is
deliberately not written: it is a recap of the events being imported, and in
front of the real thing it is both redundant and out of order.

## HTTP API

Session/turn: `POST /api/session` · `POST /api/send` (SSE) · `POST /api/retry`
(SSE) · `POST /api/continue` (SSE; finishes a reply the provider cut off —
defaults to the last assistant message) · `GET /api/session/<id>` ·
`GET /api/sessions` ·
`POST /api/session/delete` · `POST /api/protagonist`

Transcript: `POST /api/message` · `POST /api/message/delete` (`after: true`
rewinds) · `POST /api/notes`

State: `POST /api/goal` · `POST /api/memory/forget` · `POST /api/lore` ·
`POST /api/synopsis` · `POST /api/arc` (`{act}` to pin, `{clear: true}` to
return to the chapter clock)

Chapters: `GET /api/chapters/<id>` · `POST /api/chapter/draft` ·
`POST /api/chapter/close` · `POST /api/chapter/update`

Authoring: `GET /api/stories` · `GET /api/story/new` · `GET /api/story/<id>` ·
`POST /api/story/validate` · `POST /api/story/save` · `POST /api/story/duplicate`
· `POST /api/story/slug`

Introspection: `GET /api/health` · `GET /api/context/<id>` · `GET /api/prompt` ·
`GET /api/models` · `GET /api/probe` · `GET`/`POST /api/settings` ·
`POST /api/settings/reset`

Images: `POST /api/portrait` · `POST /api/portrait/describe` ·
`POST`/`GET /api/candidates` (queue portrait options, poll as each lands) ·
`POST /api/cast-portrait` · `POST /api/cast-portrait/delete` · `GET /media/<f>` ·
`GET /story-img/<story>/<file>`

Authoring aids: `GET`/`POST /api/rulebuilder` (the twelve questions, and the prose
they generate) · `POST /api/advise` (a plain-language settings request in, a
validated proposal out)

A story's rules can be generated rather than written: twelve questions about tone,
cast, action, romance, pacing, stakes and how explicit it gets, each mapping to an
authored paragraph rather than a flag. The output lands in the Rules box as ordinary
editable text — never a hidden config behind the answers, because an author who
dislikes how their story reads has to be able to find the sentence responsible.

**`GET /api/prompt?session=<id>` returns the entire assembled packet** — system
prompt, messages and the per-layer allocation report. It is the first thing to
look at when the model is behaving oddly: it shows exactly what the model saw,
including what got dropped.

---

## Frontend

`static/index.html` + `app.js` + `app.css`. No framework, no build step, no
bundler. One global `S = { id, state, busy }`, one `render(state)` that repaints
panels from the `state` SSE event, and direct DOM construction via a small `el()`
helper.

Four screens, toggled by class: `#boot` (stories + sessions), `#settings`,
`#editor` (the story editor), `#app` (the player).

The editor's controls (`field`, `txt`, `area`, `num`, `chk`, `csvList`,
`lineList`, `repeatable`) mutate `ED.data` in place and never re-render — that
is what keeps a textarea from losing focus and cursor position on every
keystroke. Sections are `secStory`, `secIntros`, `secStats`, `secKeywords`,
`secCast`; `edPayload()` spreads all of `ED.data`, which is why `protagonist:`
and `pacing:` survive a round-trip despite having no UI.

Static files are served `Cache-Control: no-cache`. There is no cache-busting in
the URLs, so a rebuilt image would otherwise serve new HTML against a
browser-cached `app.js` — a few KB on a LAN, revalidated every load, removes a
whole class of "the fix didn't deploy" confusion.

---

## Extending it

### Add a tunable

1. Constant in `config.py`, in the right section, with a comment saying what it
   is for and — if it was tuned against real play — what the old value did wrong.
2. Read it as `config.X` **at call time**, never `from config import X`.
3. If it should be live-editable, add a `K(...)` entry to `settings.KNOBS` with
   a `group`, a `label` and real `min`/`max`. The UI renders it automatically.

### Add a prompt layer

1. `_items_<name>(...)` in `assemble.py` returning an **ordered list of items**,
   most important first. One item per independently-droppable unit — a layer
   built as a single blob is all-or-nothing, which is how a long protagonist
   power description used to vanish entirely rather than lose its tail.
2. Add `("<name>", floor, ceiling)` to `config.BUDGET_LAYERS` at the right
   priority position.
3. Add it to `layers` in `build()`, to `_RENDER_ORDER`, and give it a heading in
   `_SECTION_TITLES` (or `None` if the items carry their own).
4. Check `settings._coerce_layers` tolerates it — it already splices unknown-to-
   an-old-override layers in from defaults, so a stored `settings.json` will not
   break.
5. Verify with `GET /api/prompt`.

### Add a field to the story schema

1. Parse and validate it in `story._validate`, collecting problems rather than
   raising.
2. Add it to `story._ordered` so saves round-trip it, and to `story.blank()` if
   new stories should start with it.
3. Consume it — usually a new `_items_*` in `assemble.py`, or a line in
   `memory._digest_prompt`.
4. If it should be checked against the budget, extend `assemble.story_fit`.
5. Editor UI is optional: an unhandled key still round-trips (see `edPayload`),
   so YAML-only fields are a legitimate choice.
6. Document it in [`stories/README.md`](stories/README.md).

### Add a field to the turn delta

1. Extend `memory.DELTA_SCHEMA` — **and its `required` array**, which lists every
   property. Structured output enforces it.
2. Describe it in `_digest_prompt` under "Your task", concretely, with the
   failure mode spelled out. The existing entries are worth copying in style:
   they say what counts, what does not, and which way to guess when unsure.
3. Apply it in `apply_delta`, with a filter if the model can plausibly get it
   wrong.
4. Surface it in `server.session_state` if a panel needs it.

### Add a route

`server.do_GET` / `do_POST` are `elif` chains on `path`. Add a branch, return
via `self._json()`. `StoryError` → 400, `ValueError`/`KeyError` → 400, anything
else → 500 with a traceback to stdout. Prefix-matched routes (`/api/story/<id>`)
must come *after* their exact-match siblings (`/api/story/new`).

### Add a model provider

1. A `_prose_<provider>` generator in `brain.py` yielding `("content"|"thinking",
   text)` tuples, wired into `prose()`'s `_stream()`.
2. A branch in `utility()` — provider-native structured output, or the strict
   `json_schema` path.
3. A branch in `models_list()` and `probe()`.
4. Add it to the `choices` of the `PROSE.provider` / `UTILITY.provider` knobs.

---

## Invariants worth not breaking

1. **One utility call per turn.** Not two. The whole memory pipeline is one
   `digest()`. Adding a second call for a new feature is the SillyTavern failure
   mode returning.
2. **Nothing blocks the reply.** Images, compaction and lorebook suggestions all
   run after the prose is on screen, and all swallow their own exceptions —
   a failed suggestion must never fail a played turn.
3. **stdlib only** (plus `pyyaml`). `urllib`, `sqlite3`, `http.server`,
   `queue`, `threading`. The frontend has no dependencies at all.
4. **Constants live in `config.py`.**
5. **`config.X` is read at call time**, or live settings silently stop working.
6. **Layers are lists of items, not blobs.**
7. **Degrade, don't break.** Ollama down → recency ordering. Image server down → no
   pictures. Browser gone → the turn still completes.
8. **Validation collects every problem**, never raises on the first.
9. **Writes are atomic** — temp file plus rename, for both `story.yaml` and
   `settings.json`.
10. **Git is not the safety net here.** This tree is not a git repo. Before
    deleting or restructuring anything: tarball to `~/backups/<name>-<date>.tar.gz`
    with a `.sha256`, verify it, and copy any `.env` to `~/backups/secrets-<date>/`.

---

## Known gaps

- **No tests.** `apply_delta` is deliberately separated from `digest` so a delta
  can be replayed without a model call, `assemble.allocate` is a pure function over
  a dict of lists, and `rulebuilder.build` is pure. Those three are where a first
  test suite would pay for itself immediately.
- **There is no path from `git clone` to a working loom.** No model is configured
  out of the box and nothing says what to do about it.
- **The action-pacing templates are story-specific** and hardcoded in `memory.py`.
  See [Pacing](#pacing-goals-and-action).
- **One checkpoint per image server.** A story's `checkpoint` field parses and is
  ignored; `/sdapi/v1` cannot switch models per request.
- **No expression sets.** One portrait per character. A mood set needs a stored
  per-character seed, or the faces drift into different people between expressions.
- **`_ADDED_COLUMNS` is the entire migration story.** Fine so far; it only handles
  added columns, not changed or dropped ones.
- **The editor cannot edit `pacing` or `arc`.** Both round-trip safely through a
  save; neither has a UI.
- **`raw` and `narrative` modes are not offered.** Both still parse and run, so old
  story files keep working, but neither was developed past a sketch and the editor
  only offers `play`.
- **Mobile is partly done.** The structural faults are fixed; screens that were
  never opened on a phone have not been checked. See [`TODO.md`](TODO.md) §10.
