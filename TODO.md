# TODO

Ordered by what blocks a public release. Context for most of these is in
`MEASUREMENTS.md`.

loom went local-only on 2026-10-05: the `anthropic` and `xai` providers, the
`PROSE.thinking` knob, `API_KEYS` and the entire refusal-fallback subsystem were
removed. Providers are now `ollama` and `openai_compat`.

---

Order matters here. The two documentation items are **deliberately last**: there
is no point writing a setup guide, or a final README pass, for a v1 that is still
changing shape underneath them.

---

## What is left

Two items.

### 1. Mobile

Two passes done, both from the stylesheet rather than from a phone.

**2026-10-05, the structural faults:** the settings nav ate half the screen, fixed
widths overflowed, dialogs were letterboxed, tap targets were mouse-sized, and a
dialog body scrolled the page behind it instead of itself.

**2026-10-06, an audit for layouts that assume horizontal space:** the portrait
builder was two columns of selects, which at 390px is two 180px columns of truncated
option text; the probe table is a real `<table>` with a nowrap status column and a
monospace model name, so it now scrolls sideways in its own box rather than widening
the page; a lorebook key was ellipsised at 55% of its row, which on a phone is about
thirty characters of a key that is usually longer, so it wraps. Every selector in the
phone block was checked to exist in the markup — one did not and was an invented
class.

**What is actually left needs a phone.** The audit finds rules that cannot work; it
cannot find a screen that is merely unpleasant — spacing, reachability, whether a
dialog's footer is above the keyboard, whether the composer is usable one-handed.
That is a pass screen by screen with a device in hand, and it is the only honest way
to close this.

### 2. First-install setup guide

**There is no path from `git clone` to a working loom.** `PROSE.model` and
`UTILITY.model` default to `""` on purpose — no model name is right for every host
— so a fresh clone starts with nothing configured and nothing telling the user
what to do about it.

Needs to cover, in order:

- prerequisites: ollama running, and at least one chat model pulled
- how to pick a prose model, and what size is realistic for their hardware
- the embedding model (`nomic-embed-text`) — long-term memory degrades to recency
  ordering without it
- the utility model, and that pointing it at the **same** model as prose avoids
  ollama swapping a multi-GB model in and out between lanes
- `PROSE.num_ctx`, which is the single knob the whole character budget derives from
- `LOOM_OLLAMA_URL` / `LOOM_EMBED_URL` / `LOOM_IMAGE_URL` / `LOOM_STATE` /
  `LOOM_STORIES`, and `PYTHONUNBUFFERED=1` for a systemd unit
- images are optional, and what you lose without them

Open question worth deciding first: should first run **prompt** rather than
document? An unconfigured loom fails at the first turn with a provider error, which
is a poor first impression. A setup screen listing what ollama actually holds would
beat any README section.

---

## Done

The numbering below is historical — these were §§1-9 when they were open.

### ~~Repo hygiene~~ — 2026-10-06

`.gitignore` and the git remote were done earlier. The last of it was references to
the author's own machine: a docstring citing a rules file under their home
directory, a settings help string pointing at a ledger on a named host, a LAN
address as the example URL in the LOCAL provider spec, and a bench tool carrying
both a host name and a ~/docker path for a file sitting beside it in the repo.
Found by sweeping rather than by remembering. MEASUREMENTS.md keeps its host names
deliberately — it records what was measured on which machine.

### ~~The shipped defaults did not fit their own layer floors~~ — 2026-10-06

`num_ctx` 8192 with a 4000-token reply ceiling left 12,544 characters of budget
against layer floors needing 15,600, and `settings.update()` validates the whole
state — so a fresh clone could not save any setting at all. The first thing a new
user does, failing in a way that reads as "the app is broken".

The reply ceiling was NOT the thing to lower, despite an earlier note here claiming
4000 was wrong regardless. It was set deliberately after a measured turn of a
long-form story was cut off mid-sentence at 2000, because dialogue-heavy prose
tokenises at about 2.7 characters per token. The measurement that said replies
average 700 tokens came from stories asking for three to five paragraphs, and did
not generalise.

The window was simply too small for an engine with thirteen layers. 16384 leaves
37,059 characters, about 21k above the floors, and costs roughly 2.6GB of KV cache
on a 12B. Verified against an empty state directory: defaults now validate and a
setting saves.

### ~~Final README pass~~ — 2026-10-05

Rewritten against what actually shipped: the module map had drifted by a third and
was missing three modules, images documented a ComfyUI node graph that no longer
exists, and the budget section described a `BUDGET_TOTAL` that was replaced by the
single `num_ctx` knob. The author's host names, home-directory paths and LAN
addresses are out of it, which is what made it unpublishable rather than merely
stale.


Numbers are kept as they were so older cross-references still resolve.

### 1. ~~Guard the `num_ctx` / `BUDGET_TOTAL` relationship~~ — 2026-10-05

`config.CHARS_PER_TOKEN = 3.15` (measured; the stale 3.6 comment is gone) and
`settings.context_headroom()` put the arithmetic in one place. `apply_saved()`
warns at boot, `update()` blocks a hard overflow and names both fixes, `current()`
exposes the headroom to the UI. A thin margin (<10% of the window) warns rather
than blocks. `openai_compat` opts out — that server owns its own window.

Found while verifying: the systemd unit did not set `PYTHONUNBUFFERED`, so every
`print()` diagnostic was invisible in journalctl. Fixed in the unit (outside this
repo) — **§8 must document it for native installs.**

### 2. ~~`OLLAMA_URL` defaults to a Docker service name~~ — 2026-10-05

`OLLAMA_URL`, `EMBED_URL` and `COMFY_URL` now default to `127.0.0.1`, which is
right for a native install. The Dockerfile overrides the first two with the
compose service name. `COMFY_URL` deliberately has no Dockerfile override: a
container reaches ComfyUI on the host via the bridge gateway, whose address is
deployment-specific and belongs in compose.

Verified by running with no `LOOM_*_URL` set at all — prose and embeddings both
work out of the box, which is what §8 depends on.

### 5. ~~Re-evaluate `BUDGET_TOTAL`~~ — 2026-10-05

Became a bigger change than re-picking a number. `BUDGET_TOTAL` is no longer a
knob at all: the **context window is the single knob** and the character budget is
derived from it (`settings.derive_budget`). Three numbers that had to be kept in
agreement by hand are now one that cannot disagree with itself.

`brain.model_limits()` asks ollama what the model actually is, so the window has a
visible price (`layers x kv_heads x (key+value) x 2` bytes a token — exact, not a
guess) and a checkable ceiling (`trained_ctx`).

`BUDGET_LAYERS` was also re-sized by tier rather than by feel — concrete 12,500 /
intermediate 37,000 / transcript 60,000 — after the realisation that **the
transcript is the input the utility model reads to update the intermediate tier**,
so starving it degrades both and the symptom looks like a stupid model.

Live result: window 8,192 -> 32,768, budget 20,923 -> 94,467 characters,
transcript ~5 exchanges -> ~26, `chapters` 4,000 -> 14,000, KV cache 5.00 GiB.

Retired knobs are now migrated rather than reported as "unknown" — `RETIRED` in
settings.py drops them from storage once with the reason, which matters for anyone
upgrading across the frontier strip.

### 4. ~~Priority order is not cache order~~ — 2026-10-05

Measured first: an unchanged prefix prefills 113x faster, and one changed token at
the front costs the whole prefill back (`MEASUREMENTS.md`). So position ahead of
the transcript is expensive, not cosmetic.

`config.DEPTH_LAYERS` now moves the seven layers that change on most turns after
the transcript, leaving only never-changing content in the system block:

    system block   rules, protagonist, chapters, arc        stable, cached
    messages       transcript                               appends
    depth          keyword_notes, long_memory, temp_memory,
                   state, goals, nudge, ledger, pacing,
                   director                                 small, recomputed

Position only — the text and its headings are unchanged, and layers are still
budgeted at their place in `BUDGET_LAYERS`. `_RENDER_ORDER` already existed to
separate position from priority; this uses it rather than reordering priorities.
Set `DEPTH_LAYERS = ()` to restore the old layout and A/B the prose.

Verified: a relationship upsert leaves the system block byte-identical while the
change still reaches the model at depth.

### 9. ~~Compaction should trigger on fill, not only on turn count~~ — 2026-10-05

`chapters.due()`'s own docstring already described a transcript-shedding safety
valve "underneath" the turn count. The code did not have one — it was just
`turns_in >= CHAPTER_EVERY_TURNS`. Now it has one:

    if turns_in < CHAPTER_MIN_TURNS:      return False   # narrative floor
    if turns_in >= CHAPTER_EVERY_TURNS:   return True    # the author's trigger
    return fill >= CHAPTER_TRIGGER_FILL                  # the valve

Turn count stays primary on purpose — a chapter should end because twenty turns of
story happened, not because a buffer filled — and `CHAPTER_MIN_TURNS` means the
valve can shorten a chapter but never make one trivially short.

`fill` is the whole-budget fill from the packet, the same signal
`ledger.due()` already takes, fired at the same 0.85 and for the same written-down
reason. A per-layer measure was tried first and is worse: `allocate()` grows each
layer only as far as it asks for, so a layer that fits reads 100% full and the
trigger would fire every turn.

### 3. ~~The arbiter is optimising the wrong resource~~ — 2026-10-05

Half of this was answered by §5: the budget is no longer a number set blind, it is
derived from a window whose memory cost is shown.

The other half — expressing the budget as a **latency target** — was considered and
**rejected**. A target needs a model of the latency/budget relationship, and the
projections offered during this work were wrong repeatedly and in both directions
(a prefill rate off by nearly 3x, a cache gap predicted to widen that narrowed, a
per-layer fill metric that read 100% on a story with five messages). A target built
on that would be confidently wrong on the author's hardware and worse on a
stranger's.

Built an instrument instead. Every turn records two measurements on the reply:

    ttft_ms   request -> first streamed token   tracks what the PROMPT costs
    gen_ms    first token -> last token         tracks what the REPLY costs

Two numbers, two knobs: a slow ttft means the context budget is large, a slow gen
means max_tokens is large. Reported as last turn / median of 10 / mean of 50, in
the Context panel next to the fill they relate to.

It does nothing automatically. No threshold, no auto-shrink, no advice carrying a
number nobody measured. The reader decides.

Honest caveat, carried in the tooltip: ttft also includes a model reload and queue
wait, so a single outlier can be a load rather than a budget problem — which is why
the median and mean sit beside it.

---

# Roadmap

The goal is a locally hosted equivalent of OOC.ai, so parity with its features
comes before anything new. The thesis is a **lived-in world**: somewhere a player
returns to for weeks, rather than a character they try for two days and replace.
That is what justifies a long setup — a cast with one portrait each does not sell
"these are real people"; a cast with a full expression set does.

## v1.1 — the settings assistant — DONE

Natural-language intent to settings changes: "I want more creative prose", "longer
replies". The parts already exist — `settings.current()` returns all 55 knobs with
label, help, bounds and current value, which is a tool schema; `brain.utility()`
already does provider-native structured JSON; `settings.update()` validates
atomically and raises, so a bad proposal fails safely.

**It must diagnose before it adjusts, and propose rather than apply.** The worked
example is real: asked for "more creative prose" on 2026-10-05, a knob-twiddler
would have raised temperature from 0.7, declared success, and left `rules` dropping
the story's world details on every turn — masking the actual fault. loom already
knew (`advice` said so plainly); nobody was looking at that panel. So the assistant
reads the live allocation, `dropped` counts, fill and the ttft/gen timings, explains
what it found, and offers the change as a diff to confirm.

Scope it hard: settings only. Read-only on stories and the database.

Uses the configured prose model — a GGUF cannot ship in a repo, and the model is
already resident, so this costs no extra memory and no extra download.

## v1.2 — character sprites and per-line faces — LARGELY DONE

Shipped: identity proven without a face embedding; stable-diffusion.cpp behind
/sdapi/v1; portraits chosen at story-save and at character creation and managed from
the cast card; attributed dialogue in CORE_RULES with the speaker's face beside the
line; the cast on screen before the first turn.

Still open: expression SETS, deferred on purpose — a mood set needs a stored
per-character seed so the faces stay the same person between expressions, and for now
a face you dislike is re-rolled on the spot. And a story's `checkpoint` field is
accepted and ignored, because an sd-server process loads one model at startup and
/sdapi/v1 cannot switch it per request.

### What already exists

`images.py` runs a single background worker draining a queue, entirely off the turn
loop (20-40s a render on a 4070, never in the critical path). Portraits are cached
per character name forever. `portrait_prompt()` / `set_portrait_prompt()` let a
player describe a character the story does not define. `_render()` already takes a
seed and a checkpoint.

### Everything must run on one machine

The author's own deployment renders on a second box's RTX 4070 over the network.
**That is not the shipped architecture and must not be designed around.** A released
loom runs on whatever single machine the user has, so image generation shares a GPU
with the language model.

Consequences, none of them fatal but all of them real:

- **The GPU is contended.** A render and a turn want the same device. The queue is
  already off the critical path, which helps, but a render during generation will
  slow both. Renders probably want to pause while a turn is streaming.
- **`IMG_MAX_CONCURRENT = 1` is justified by a comment reading "the 4070 has 12GB".**
  That assumption no longer holds and the number needs rederiving per machine.
- **The 20-40s figure is a 4070 number.** An iGPU with no tensor cores and a third
  the bandwidth will be slower, which multiplies through a 28-image expression set.

### The image backend is a packaging decision, not a detail

Two paths, and this choice matters more for shipping than anything else here:

**ComfyUI** — what loom speaks today. Rich: arbitrary graphs, IP-Adapter, ControlNet,
LoRA, which is exactly what the identity problem below needs. But it is **not
packaged** on Arch (git clone plus a venv, since PEP 668 blocks pip), carries a large
PyTorch dependency tree, and **`python-pytorch-rocm` supporting gfx1151 is
unverified**. That last point is a hard prerequisite nobody has tested.

**stable-diffusion.cpp** — same ggml lineage as llama.cpp, a single binary, no Python
at all. `aur/stable-diffusion.cpp-vulkan-git` is actively maintained and most-voted,
and the Vulkan backend is **already proven on this hardware** (see MEASUREMENTS.md:
RADV STRIX_HALO, within 7% of ROCm for LLM inference). Note the ROCm variant,
`-hipblas-git`, is orphaned and a year out of date — Vulkan is the live path. Far
~~narrower features though: identity preservation would mean PhotoMaker rather than
IP-Adapter.~~ **Checked 2026-10-05 and this was wrong:** `sd-server --help` lists
`--ip-adapter` (with `--clip_vision`) and `--control-net`. The feature gap that made
this choice feel risky is largely not there, so needing a real face embedding later
would not force a move to ComfyUI.

Cheapest next step is to verify the backends rather than argue: does PyTorch-ROCm
see gfx1151 at all, and can stable-diffusion.cpp-vulkan hold a face across a dozen
renders. Both are small experiments and they decide the whole feature's shape.

### Character identity — probably easier than it looks, because anime

28 expressions at 20-40s is 9-19 minutes a character, an hour for a party of four.
That cost is acceptable and is the point. The risk is whether the 28 images read as
the same person.

**For anime it probably does, and that changes the architecture.** Photoreal
identity lives in subtle bone structure and skin texture, which is why preserving
it needs a face embedding. An anime character *is* a short list of discrete,
nameable, promptable attributes — hair colour and style, eye colour, accessories,
outfit. Fixed seed plus those tags plus a varied expression tag is how SillyTavern
sprite packs are generated in practice, and the checkpoint here is already anime
(`waiIllustrious`), with stories styled to match.

So the likely answer is the cheap one: a consistent character tag block, a fixed
seed, a fixed head-and-shoulders framing, and only the expression term varying.

What still drifts and wants watching: outfit and accessory detail, art-style wobble
between seeds, and framing or head angle. A fixed framing in the prompt constrains
most of it.

**This makes the backend choice much easier.** If no face embedding is needed, then
stable-diffusion.cpp's narrower feature set stops being a drawback — and the
single-binary, no-Python, Vulkan-already-proven option wins on the thing that
actually matters for shipping. Verify before committing, but expect this.

The fix matches the flow already described, and is backend-independent: the player
picks one option, and **that image becomes the identity anchor** — fed back as a
reference when rendering the expression set, so the expressions are derived from the
chosen face rather than re-rolled from the same words.

For anime that anchor may be nothing more than *the seed and tag block that produced
the chosen option*, reused for every expression — no reference image mechanism at
all. If it turns out a stronger anchor is needed, **both backends have IP-Adapter** —
ComfyUI's requires the workflow to stop being one hardcoded graph (already a listed
known gap), and stable-diffusion.cpp exposes `--ip-adapter` with `--clip_vision`, plus
img2img against the chosen image.

**RESOLVED 2026-10-05 — the cheap approach works.** Two twelve-image sets on
`waiIllustriousSDXL_v170`, fixed seed 42, fixed tag block, fixed framing, only the
expression term varying, 768x1024 at 24 steps.

Identity holds without any face-embedding mechanism. Across twelve renders the face,
hair colour, hairstyle, eye colour and art style are consistently the same character.
**No IP-Adapter, no PhotoMaker, no reference image, no anchor plumbing.** The identity
anchor really is just the seed plus the tag block, as predicted.

What drifts is **sporadic and per-frame, not systematic**, and that is the finding
that shapes the feature:

- outfit detail drifts in proportion to how much detail there is to drift. A
  decorated military coat changed its insignia in all twelve frames; a plain gym
  shirt with red trim held across all twelve. **Simple outfits are a real mitigation.**
- one frame in twelve tied the hair up; two or three carried a shirt logo the others
  lacked; one zoomed in and added manga emphasis lines.

**So the fix is a review-and-reroll step, not a better pipeline.** Show the player the
contact sheet, let them click the two or three frames that came out wrong, re-roll
those with a bumped seed. At 49 s a render that is about 100 seconds to repair a
28-image set. Everything img2img anchoring would have bought is achieved by
regenerating the handful of outliers, and it needs no new backend capability.

Judged by the author, who called the result "almost perfect" and the core concept
"sound".

**The remaining constraint is time, not quality.** 49 s a render means 23 minutes for
a 28-expression set and 1.5 hours for a party of four. See MEASUREMENTS.md for the
ranked levers; a quantised GGUF checkpoint is the one to try first.

**One shipping hazard found by accident.** The first character block — "black
high-collared military coat" with white hair — produced unmistakable SS iconography in
all twelve frames, because anime checkpoints carry strong unintended priors on certain
tag combinations. A setup flow that accepts free-text character descriptions *will*
hit these. `military, uniform, insignia, medal, armband` are now in the test's negative
prompt and something like that belongs in the shipped default.

### The flow

1. At setup, describe a character; generate several options in parallel.
2. The player picks one. It is stored as that character's reference.
3. The expression set renders in the background off the selected reference.
4. Dialogue lines display the matching expression.

### Per-line faces

OOC.ai puts the portrait **above** its dialogue rather than inline, which is why it
reads as a cast speaking rather than a novel describing. That needs speaker-attributed
output, and loom has no mechanism for it today: every prose instruction lives in a
story's own `rules`, and there is no shipped global format directive.

Two things to resolve:

- **Where the directive lives.** A format every story inherits is new — probably a
  shipped layer ahead of `rules`, not something each story restates.
- **It pulls against existing story rules.** Seiran's say "Your narration is clean
  prose — never asterisks around actions". A per-speaker block format is structure
  imposed on prose that was explicitly told to be unstructured. Those have to be
  reconciled deliberately, not by having the format quietly win.

### Mode consolidation

Settle on `play` (CYOA) and hide `raw` and `narrative` in the editor until they are
developed further. Hiding the option does not break existing stories — the field
still parses, so goblin-road, laundry-and-taxes and reverse-isekai (`raw`) and
kindling (`narrative`) keep working. They simply stop being creatable, and the
sprite work has one mode to target instead of three.

## v1.3 — the memory system is overbuilt

Two observations from OOC.ai, the thing loom is a local replacement for. Both are
simplifications, and the second one is uncomfortable.

### Observation 1: state belongs in the transcript, not in a pipeline

OOC.ai ends every reply with a structured block — identity, date, time, location,
inventory, money, one line per character with mood and whereabouts, and a one-line
recap of the beat. Four mechanisms are doing work there, and only the first is
obvious:

1. **It rides in context for free.** It is part of an assistant message the
   transcript already pays for. loom's `state` layer competes for budget as its own
   line item.
2. **It is immutable, so it is cache-safe.** Once written, that text never changes;
   it is frozen in a historical message. loom's `state`, `goals` and `ledger` sit at
   DEPTH and are recomputed and re-rendered every turn, invalidating the prompt
   tail. The footer is append-only, which is what prefix caching wants.
3. **The model authors it, so it cannot disagree with the prose.** loom runs a
   separate utility call to parse narrative into fields — a lossy translation that
   can drift from what the story actually said. The footer is written in the same
   breath as the prose, by the same model.
4. **It self-corrects by induction.** Each turn the model reads the previous block
   and writes the next, so persistence becomes "copy forward and amend" — a task
   models are reliably good at, unlike re-extraction from scratch.

**The synthesis, better than either approach:** have the model emit the footer, then
**parse it into SQLite** rather than extracting from prose. That inverts the fragile
step — instead of asking a utility model to read prose and guess the state, ask the
prose model to emit a rigid format that parses deterministically. Same rows, one
fewer model call per turn, no drift, the UI panels keep working, and state becomes
append-only transcript instead of a recomputed depth layer. A parse failure degrades
gracefully: that turn's row does not update, but the block is still in context doing
its primary job.

Costs, not fatal but real: every reply pays 150-250 tokens of footer forever, and
measured replies already stop naturally around 2,200 characters, so it competes with
prose. And Seiran's rules say "clean prose — never asterisks around actions"; a rigid
block is structure imposed on prose explicitly told to be unstructured. Same tension
as the per-line sprite format in v1.2, and it must be reconciled deliberately rather
than letting the format quietly win.

### Observation 2: long-term memory is just a lorebook

The first read of OOC.ai was "4 long-term, 4 temporary, a relationships blurb, one
goal — far simpler". That was wrong and the correction matters: **one story has 91
long-term memories.** It is doing heavy summarization. The difference is not that it
skips the work, it is that the work produces a flatter result.

What it actually appears to be:

- story content authored at creation, in the prompt
- cast and keywords for static recall when relevant
- **long-term memories: an unbounded, auto-written lorebook**, entries created at its
  own chapter boundaries
- **short-term: a fixed small window**, around four threads, that may or may not be
  pulled on
- relationship status
- **one** core goal the narrative steers toward

The structural point: **that is one mechanism where loom has four.** A growing
lorebook with mixed authorship — some entries written by the player, some written
automatically at chapter closes — retrieved through a single path. loom has
`keyword_notes` (static lorebook), `long_memory` (embedded retrieval), `chapters`
(compaction) and `temp_memory` (heat and decay) as four subsystems, with four budget
lines and four code paths, for arguably one thing.

And a fixed cap of four short-term threads means **no heat-and-decay arithmetic is
needed at all.** Decay exists to rank a large pool. Ranking four items is just
recency.

### Why theirs is fast, honestly

Not architecture cleverness — **prompt size.** A few thousand tokens of lorebook
hits plus four threads plus a goal, against the roughly 30,000 characters loom
assembles for Seiran. Fixed caps also make the total bounded and predictable, where
loom's layers are unbounded and must be arbitrated.

Which leads somewhere uncomfortable: **the context-budget arbiter exists to solve a
problem created by having many unbounded competing layers.** Fixed small caps avoid
the problem rather than solving it.

### What earns its keep anyway

The arbiter is still right **for loom specifically**, and for a reason that does not
apply to OOC.ai: loom ships to whatever single machine someone has, with budgets
from 8k to 96k, where OOC.ai tunes for one known deployment it controls. Fixed caps
cannot spend a large context window well, and cannot shrink to fit a small one. The
arbiter is solving a harder problem, not an imaginary one.

Also keep:

- **the dropped/`advice` reporting**, which caught a real bug — Seiran silently
  losing world details and lore on every turn, invisible until the panel said so
- **DEPTH ordering**, which keeps the volatile layers behind the stable ones so the
  system block stays byte-identical across turns (verified: it does)
- **the chapter-anchored transcript floor**

### Shared infrastructure with v1.2

The footer and v1.2's per-line speaker attribution need the identical missing piece:
**a shipped global format directive**, a layer every story inherits rather than each
story restating. Build it once and the memory simplification comes nearly free.

### Proposed, not decided

1. Collapse `keyword_notes` + `long_memory` + `chapters` into one lorebook with
   mixed authorship and a single retrieval path.
2. Replace `temp_memory` heat/decay with a fixed window of N recent threads.
3. Replace `state` + `goals` extraction with the parsed footer.
4. Keep the arbiter and the dropped reporting.
5. Measure prompt size and ttft before and after, on the same story and seed.

### Open questions, measurable before committing

- **How many of those 91 entries enter the prompt per turn?** If retrieval pulls
  most of them, the prompt is not small and the speed explanation is wrong. This is
  the single number that decides whether observation 2 holds.
- Does a fixed window of four actually suffice, or is loom's heat ranking doing
  something a reader would miss?
- Changing memory architecture against existing stories risks regressions that only
  show up in prose quality, which is the hardest thing here to measure.

## v1.4 — help the user write the rules — DONE

The premise, from the author: *"the average user is probably going to be very, very
simplistic in what they denote for the world, the rules, the characters."* Seiran's
rules are 5,207 characters of specific, opinionated craft and they are why it reads
well. Nobody arriving at loom for the first time writes that. They write "a fantasy
world with dragons" and get something flat, and they will blame the model.

### Seiran decomposes into a questionnaire

Every block in it answers one design question. That list IS the interview:

| the block | the question |
|---|---|
| TONE: modern hero-school anime, warm, loud, funny | what emotional register? |
| EVERYONE IS AN ADULT | who is the cast? |
| A FIGHT IS ONE PERSON SOLVING IT | how does action work? |
| QUIRKS HIT HARD | how powerful is everyone? |
| THE CAST IS THE POINT | is this about people or plot? |
| ROMANCE: real, slow, the player's to steer | romance, and who drives it? |
| SHONEN, NOT SHERLOCK | mystery and deduction, or not? |
| DO NOT CONNECT THINGS | is there an overarching pattern? |
| WHAT A SESSION IS MADE OF | the content mix, in order of how much room each gets |
| VILLAINS ARE INDIVIDUALS | what scale are the antagonists? |
| FAILURE IS REAL, one floor only | what are the stakes, and what is protected? |

Eleven axes. Ask those eleven questions and you have specified a story's rules.

### What makes this work or fail

**Emit prose, not flags.** The value is not in knowing the user wants no mystery, it
is in the paragraph *"DO NOT CONNECT THINGS... If you notice yourself arranging details
into a pattern, throw the pattern away."* A toggle that emits "no conspiracies" is worth
a fraction of that. Each option has to map to writing as specific as Seiran's, which
means **someone has to author the fragments well.** That is the real cost here and it is
writing work, not engineering work.

**Generate into the rules field, visibly.** The output is editable text the user can
read, learn from and change. Never a hidden config behind the toggles — if it is a
black box they cannot fix what they dislike and they learn nothing about why their
story reads the way it does.

**The highest-value question is the one nobody would think to ask.** WHAT A SESSION IS
MADE OF — ordinary life as the baseline, violence frequent but brief, romance underneath
both — is what makes Seiran feel like a life instead of a highlight reel. A new author
will not specify a content ratio unprompted. Asking is the whole point of asking.

### Two routes to the same place

The questionnaire suits people who want to be guided. For people who can already
describe what they want, **v1.1's settings advisor is the same shape** — natural
language in, a validated proposal out, shown as a diff before it applies. "A grim
detective story in 1970s Chicago, no supernatural, the player can die" should produce a
proposed ruleset the same way "more creative prose" produces proposed settings. The
machinery exists; it needs a second prompt and a different target field.

### Templates

Named, importable rulesets, with Seiran's as the first one. Start from a template,
answer the questions to adjust it, edit the prose directly, and export your own.
`CORE_RULES` is already the universal half; this is the per-story half.

### Risks

- **Generic fragments produce generic stories.** If the authored prose is bland the
  questionnaire actively harms — it gives users confidence in a weak ruleset. Better to
  ship five excellent options per axis than twenty mediocre ones.
- **Scope.** This is the story-creation experience, which also carries v1.2's character
  sprites and v1.3's format directive. Creation is becoming loom's main product surface
  and should probably be designed once, as a whole, rather than three times.
