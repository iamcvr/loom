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

## v1 — finish the engine

Nothing left. §§1-5 and 9 are in Done below; the engine work is complete and
the three remaining v1 items are the documentation ones, deliberately last.

---

## Anytime

### 6. Repo hygiene

- `__pycache__/` is untracked but not ignored — needs a `.gitignore`
- `story.py:628` cites `~/Projects/CLAUDE.md` in a docstring: a local dev
  artifact that will mean nothing to anyone else
- **no git remote configured** — versioning is local-only

---

## Release prep — only once v1 is settled

### 7. Final README pass

The README was de-staled on 2026-10-05 — the frontier providers, the
refusal-fallback section, the `fallback` SSE event and the `claude-haiku-4-5`
example are all gone, and "Running it" now documents the native systemd
deployment alongside Docker.

What is left is a **final pass once v1 is settled**, because §§1–5 will change
the engine underneath it. Specifically, re-check:

- the `## Model routing` section against whatever the provider story ends up being
- the `## The context budget arbiter` section if §3 changes what the budget means
  (characters of prompt vs a latency target)
- `## Settings` against the knob list, which lost six knobs in the strip and may
  gain some in §1
- the internal network details still in the examples — `192.168.5.249`,
  `192.168.5.234`, `172.19.0.1`, `atlas`, `solos` — which are fine in a private
  repo and map the LAN in a public one

### 8. First-install setup guide

**There is no path from `git clone` to a working loom.** `PROSE.model` and
`UTILITY.model` now default to `""` on purpose — no model name is right for every
host, and the old defaults (`claude-sonnet-5`, `claude-haiku-4-5`) would be
actively broken on a local-only build. So a fresh clone starts with no model
configured and nothing tells the user what to do about it.

Needs to cover, in order:

- prerequisites: ollama running, and at least one chat model pulled
- how to pick a prose model, and what size is realistic for their hardware
- the embedding model (`nomic-embed-text`) — long-term memory degrades to recency
  ordering without it
- the utility model, and that pointing it at the **same** model as prose avoids
  ollama swapping a multi-GB model in and out between lanes
- `num_ctx` vs `BUDGET_TOTAL`, and the arithmetic that relates them (see §1)
- `LOOM_OLLAMA_URL` / `LOOM_EMBED_URL` / `LOOM_STATE` / `LOOM_STORIES`

Open question: should first run **prompt** rather than document? An unconfigured
loom currently fails at the first turn with a provider error, which is a poor
first impression. A setup screen that lists what ollama actually holds would be
better than any README section.


## Done

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

## v1.1 — the settings assistant

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

## v1.2 — character sprites and per-line faces

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
narrower features though: identity preservation would mean PhotoMaker rather than
IP-Adapter, and **whether it supports either needs checking before committing.**

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
all. If it turns out a stronger anchor is needed, ComfyUI has IP-Adapter (requiring
the workflow to stop being one hardcoded graph, already a listed known gap) and
stable-diffusion.cpp has PhotoMaker or img2img against the chosen image.

**Prove this first, with one character, before any selection UI exists.** It is a
ten-minute experiment on the existing checkpoint: fix a seed, fix a tag block, fix
the framing, vary only the expression, and look at twelve of them. The answer
decides the backend, the packaging and the shape of the setup flow, and it is far
cheaper to learn now than afterwards.

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
