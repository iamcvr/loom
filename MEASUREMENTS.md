# Measured findings

Why loom is built the way it is. Every entry here was *measured* on a real
prompt, not reasoned about, and each one is the evidence behind a decision still
standing in the code.

loom became local-only on 2026-10-05 — the providers are `ollama` and
`openai_compat`. Several findings below cite Anthropic, xAI or Grok. They are
kept deliberately: loom no longer talks to those APIs, but the *behaviour* they
revealed is still what the code guards against, and a guard whose reason has
been deleted is a guard somebody removes next year.

---

## Streaming

### Reasoning models go silent before they say anything

Measured on grok-4.6 with a full loom prompt: **78 seconds of `reasoning_content`
before the first visible character, with 52-second silent gaps.** Dropping those
deltas on the floor made the SSE connection look dead to the browser and left the
reader staring at an empty message.

→ `_prose_openai_compat()` forwards them as `thinking` events so the turn can show
progress and the socket keeps moving. Local reasoning models behave identically,
which is why this still matters.

### "Unfinished" has three different names

A reply that ran out of budget is **not** a finished reply, but the stream ends
identically to one that is — so a beat cut off mid-sentence was stored as
complete and the digest ran on half a scene. Each API names it differently:

| API | field | value |
|---|---|---|
| OpenAI-shaped | `finish_reason` | `"length"` |
| ollama | `done_reason` | `"length"` |
| Anthropic | `stop_reason` | `"max_tokens"` |

→ all normalised to `max_tokens`, which the turn loop keys the resume button off.

### A transient fault can arrive inside a 200

Anthropic reported overload as a mid-stream `error` SSE event rather than an HTTP
status. Ignoring it turned a provider fault into a silent empty stream and a turn
that appeared to vanish.

→ `PROSE_RETRIES` with doubling backoff, and retries fire **only** before any text
has reached the screen, so a partly-streamed reply is never duplicated.

---

## Structured output

### `json_object` is not a schema

Measured against xAI: `json_object` honours "give me JSON" and nothing else —
asked for objects carrying a salience field, it returned **bare strings, silently
dropping half the delta.** `json_schema` returns exactly the shape requested.

→ strict `json_schema` first, falling back to `json_object` only when the server
rejects the parameter outright. Local llama.cpp and KoboldCpp servers commonly
implement only the latter.

### Provider-native structured output removes a whole failure class

Letting the provider constrain generation means the memory pipeline never parses
JSON out of prose and never retries on malformed output. That was a real failure
mode in the SillyTavern build and it is designed out here.

→ ollama constrains generation with `format`. Measured on Cydonia 24B IQ3_M:
**10/10 valid and schema-conforming, ~8s a call.**

---

## Prompt shape

### Somebody has to be holding the conversation open

Anthropic rejected a packet that did not end with a user message — *"This model
does not support assistant message prefill"* — and a narrative-mode story has
nobody typing, so every single turn would 400.

The deeper reason is loom's own, and it outlives the API that exposed it: the
depth channel attaches to the last user message. With no user turn at all, the
director's note silently degrades into the system prompt, **where it has been
measured not to work**, and an overdue pacing beat is dropped entirely.

→ `CONTINUE_TURN_TEXT`, appended whenever the transcript does not end on a user
turn.

### 2,000 characters was a guess, and it was wrong

A real player wrote a 2,500-character power and the whole `protagonist` block was
dropped from every single turn. Separately, a measured turn of *The Mercy* stopped
at 5,372 characters exactly mid-sentence, because dialogue-heavy prose tokenises
at roughly 2.7 characters per token.

→ layer ceilings are sized from real content, not estimates. See the budget table
in `config.py`.

---

## Model lists

### A listing is not an authority on what the API will accept

`claude-haiku-4-5` was a working alias that the Anthropic `/v1/models` listing
never returned — it listed only the dated `claude-haiku-4-5-20251001` — while
that alias served this project's entire memory pipeline. ollama tags drift from
what a Modelfile registers in the same way.

→ `check_models()` warns and **never blocks**, and the model picker is free text
rather than a closed dropdown. Treating "absent from the list" as "invalid" would
reject a model that demonstrably works.

### A listing says what exists, not what is answering

During the Opus 5 incident every model was still listed and every call to it
returned 529.

→ `probe()` sends one real minimal request instead of trusting the listing.

---

## Local inference

Measured 2026-10-05 on `solos` (Ryzen AI MAX+ 395, Strix Halo, 120 GiB
GPU-addressable) running Cydonia 24B Q6_K.

### Characters per token: 3.15, not 3.6

Measured against the tokenizer on a real dialogue-heavy turn. The old 3.6 figure
ran **~14% light**, so every budget estimate derived from it understated the token
count. `BUDGET_TOTAL` is in characters, so this ratio is what converts it:
48,000 characters ≈ 15,200 tokens.

### Prefill is cheap; generation is not

| phase | rate |
|---|---|
| prefill @ 5k tokens | 144 t/s |
| prefill @ 11k tokens | 151 t/s |
| prefill @ 22k tokens | 162 t/s |
| **generation** | **11.4 t/s** |

Two consequences, and they invert the assumption loom was designed under:

1. **Generation costs ~13× more per token than prefill.** 15,000 tokens of prompt
   costs about the same wall-clock as 800 tokens of output. Locally the scarce
   resource is `max_tokens`, *not* `BUDGET_TOTAL` — the opposite of a
   cost-per-input-token API where the context budget is the bill.
2. **Prefill throughput improves with depth** (144 → 162 t/s), so a large prompt
   is marginally cheaper per token than a small one. Cost scales sub-linearly.

### llama-server caches prompts by default

Sending the same 20,000-character prompt twice without passing `cache_prompt`:
`cache_n=4441, prompt_n=1, prefill 0.1s`. The KV cache is reused on a matching
prefix with no client-side flag.

→ The cache only helps on an **unchanged prefix**. loom's arbiter re-allocates
every layer each turn, and the volatile layers (`ledger`, `chapters`, `state`,
`temp_memory`) sit *early*, ahead of the transcript — so when any of them changes,
everything after it re-prefills. Priority order and cache order are not the same
thing. Not yet addressed; see `TODO.md`.

### ollama truncates silently

ollama does not error when the prompt exceeds the context it loaded the model
with — it drops the front, so the story reads as having developed amnesia and the
model takes the blame. loom sends `num_ctx` on every request so the budget and the
context window cannot drift apart, which also means loom **overrides** whatever
`OLLAMA_CONTEXT_LENGTH` the server was started with.

→ Keep `num_ctx` above `(BUDGET_TOTAL / 3.15) + max_tokens`. On 2026-10-05 the
live config sat 7 tokens inside that limit.

### Prefix caching is worth 113x, and one token destroys it

Measured against ollama, 7,011-token prompt, Cydonia 24B Q6_K:

| request | prompt tokens | prefill |
|---|---|---|
| cold prefix | 7,011 | **13.51 s** |
| same prefix, different user message | 7,011 | **0.12 s** |
| same prefix again | 7,011 | 0.11 s |
| **one token changed at the very front** | 7,013 | **13.48 s** |
| back to the original prefix | 7,011 | 0.11 s |

Three things follow:

1. **An unchanged prefix is effectively free** — 113x faster. No client flag is
   needed; it is on by default.
2. **A single changed token at the front costs the entire prefill back.** Position
   is not a micro-optimisation: anything that changes *ahead* of the transcript
   drags the whole transcript through reprocessing with it, every turn.
3. The cache survives switching away and back, so it is not one slot.

Note: ollama's `prompt_eval_count` reports how many prompt tokens there *were*,
not how many were computed — it reads 7,011 on every row above. Only
`prompt_eval_duration` reveals a cache hit. Measuring the count will tell you
caching is broken when it is working perfectly.

→ `config.DEPTH_LAYERS` moves the seven layers that change on most turns
(`keyword_notes`, `long_memory`, `temp_memory`, `state`, `goals`, `nudge`, and
`ledger` which was already there) *after* the transcript, leaving only
never-changing content in the system block. Verified: a relationship upsert
leaves the system block byte-identical while the change still reaches the model.

### The transcript is already chapter-anchored — do not "fix" it

`build()` takes the transcript from `store.chapter_transcript_floor(...)`, i.e.
everything since the last closed chapter plus `CHAPTER_OVERLAP_MSGS`. So it grows
by appending within a chapter and resets when one closes — it does **not** slide
by two messages a turn. The `transcript[-kept:]` trim further down is a safety
valve for a chapter that outgrows its allowance, not the normal path.

Open question (see TODO): at `CHAPTER_EVERY_TURNS = 20` and a 1,200-token reply
ceiling, a chapter can produce more transcript than the allowance holds, so the
safety valve fires for the back half of each chapter and the prefix slides after
all. Either compaction should trigger on fill — as `LEDGER_TRIGGER_FILL = 0.85`
already does for the ledger — or the two numbers should be derived from each other.

### SQLite reuses freed row ids, so orphaned child rows are not inert

`INTEGER PRIMARY KEY` is `max(rowid) + 1`, not a counter. Delete the
highest-numbered sessions and the next session created **takes one of their ids** —
and inherits any child rows that outlived them.

Observed 2026-10-05: a brand-new story, created minutes earlier at turn 0, opened
carrying twelve ledger facts from a different story entirely, written eighteen days
before the session existed. The cause was a hand-written delete cascade that listed
twelve tables while fourteen carried a `session_id`; `ledger` and `ledger_state`
were added later and never joined it.

Two fixes, because one was not enough:

- the cascade is now **derived from the schema** (`_session_tables()` scans for a
  `session_id` column), so a table added tomorrow is covered the day it is created
  rather than whenever somebody remembers
- `create_session()` clears child rows for the id it was just handed, which makes
  id reuse harmless even if a cascade is incomplete again or a delete dies half-way
- `purge_orphans()` runs at startup and reports what it removed, so existing
  databases self-heal

Incidental trap found while diagnosing: `ledger.ts` is declared `TEXT`, so
`WHERE ts < ?` against a float silently matches nothing. It returns zero rows and
looks like an answer. Cast both sides.

### Layer ceilings are measurements. Do not re-derive them from reasoning.

The ceilings in `BUDGET_LAYERS` were sized from real stories, and the comments say
so — *"2,000 was a guess and it was wrong: a real player wrote a 2,500-character
power and the whole block was dropped from every single turn."*

On 2026-10-05 the table was reorganised by tier, which was the right framing, and
the numbers were re-derived from that framing, which was not. `rules` went
11,000 -> 8,000 and `keyword_notes` 12,000 -> 4,000 on tidiness grounds.

Seiran wants 10,012 for `rules` and 7,819 for `keyword_notes` on an ordinary turn.
So every single turn silently dropped the world details, the opening scene and
three lore entries — and the symptom was **prose that read as unimaginative**, not
an error. The model had the tone instructions and nothing specific to be vivid
about.

`rules` is the trap because it is not just the rules: it carries the story rules
AND the world details AND the opening scene in one layer.

Two lessons:

1. The arbiter already reports this. `packet["advice"]` said *"Dropping content
   from: rules, keyword_notes. This is a ceiling, not a squeeze... It will be cut
   on every turn until you do."* Nobody read it, because the symptom looked like a
   model problem. **Check the allocation before blaming the model.**
2. A ceiling that was derived from content is data. Reorganising a table is not a
   licence to re-guess its values.

### Ceilings scale with the budget, or raising the window does nothing

The ceilings in `BUDGET_LAYERS` are absolute character counts. On their own that
makes the budget knob nearly inert: raise the context window and the extra budget
flows only to `transcript`, the one layer that absorbs slack. Every other layer
stays pinned at a number sized for a smaller machine.

That is how a hand-squeezed table outlived its reason. The table was tightened to
fit a 20,923-character budget — correct at the time, when an 11,000 ceiling for
`rules` would have been more than half of everything available — and then the
window went to 32,768 and nothing connected the two. Seiran kept losing its world
details and three lore entries on every turn, and the symptom read as a dull model.

    scale = max(1.0, BUDGET_TOTAL / BUDGET_REFERENCE)
    effective_ceiling = written_ceiling * scale

`BUDGET_REFERENCE = 96_000` is the budget the written ceilings were sized against.
Above it everything loosens together. At or below it the written values hold and
**priority order does the rationing**, which is what priority order is for and is
strictly better than editing the table by hand.

    window  32,768 -> budget  94,467 -> x1.00 -> rules ceiling 11,000
    window  65,536 -> budget 192,525 -> x2.01 -> rules ceiling 22,060
    window 131,072 -> budget 388,641 -> x4.05 -> rules ceiling 44,531

The allocation report carries the *effective* ceiling, and the settings panel says
when the written numbers are being stretched — otherwise the layers table and the
context panel disagree with each other and both look wrong.

## Image generation on gfx1151 — stable-diffusion.cpp, Vulkan (2026-10-05)

`stable-diffusion.cpp-vulkan-git r929`, `waiIllustriousSDXL_v170.safetensors`
(6,938,040,682 bytes, 2,515 tensors, SDXL), Strix Halo iGPU, RADV.

One 1024x1024 render, 28 steps, euler_a, cfg 5.0, clip-skip 2:

| stage | time |
|---|---|
| model load (warm page cache) | 0.8 s |
| sampling, 28 steps | 98.6 s — **3.5 s/iteration** |
| VAE decode | 6.2 s |
| **total** | **105.7 s** |

**This is 2.6-5x slower than the figure the v1.2 roadmap was written against**, which
was 20-40 s on an RTX 4070. The consequence is arithmetic: a 28-expression set is
**49 minutes per character** and a party of four is **3.3 hours**, where the roadmap
assumed 9-19 minutes and an hour and called that acceptable. It needs optimisation
before the expression set is viable, not after.

Untested levers, in rough order of expected value on this hardware:

- **A quantised GGUF checkpoint.** Strix Halo is bandwidth-starved — shared LPDDR5X,
  no dedicated VRAM — and SDXL sampling at f16 is memory-bound. `calcuis/illustrious`
  publishes q4_k_m at 1.5 GB against this checkpoint's 6.9 GB, roughly 4x less weight
  traffic per step. Largest expected win, and it is the same ggml lineage.
- **Fewer steps.** 28 -> 20 is a 29% cut and Illustrious is usable at 20-24.
- **Lower resolution.** Head-and-shoulders sprites do not need 1024x1024.
- **`--diffusion-fa`** (flash attention in the diffusion model only), unmeasured.

One warning appears during VAE decode and is survivable — the image decodes
correctly:

    ggml_vulkan: Failed to allocate pinned memory
    (Requested buffer size exceeds device buffer size limit: ErrorOutOfDeviceMemory)

That is the known Strix Halo Vulkan buffer-size ceiling. It costs a fallback path
rather than a failure, and may be worth tuning.

### Making it fast enough — what actually worked (2026-10-05)

Same character, same seed 42, same tag block, same negative prompt, 768x1024, euler_a,
cfg 5.0, clip-skip 2. Only the stated variable changes each row.

| config | per render | 28-expression set | quality |
|---|---|---|---|
| f16, 24 steps | 49 s | 23 min | baseline |
| q4_K, 24 steps | 43 s | 20 min | equivalent |
| q4_K + `--diffusion-fa`, 24 steps | **27 s** | **12.8 min** | equivalent |
| q4_K + `--diffusion-fa`, 16 steps | **19.4 s** | **9.1 min** | equivalent |

**The prediction above was wrong and the error is instructive.** A quantised GGUF was
ranked the largest expected win, on the reasoning that Strix Halo is bandwidth-starved
and SDXL sampling at f16 is memory-bound. Quantising the same checkpoint to q4_K cut
weight traffic 2.5x (6.94 GB -> 2.80 GB) and bought **12%**. Weight bandwidth was not
the constraint.

`--diffusion-fa` then cut 37% on its own. Taken together these say the workload is
**attention- and compute-bound, not weight-bandwidth-bound**, on an iGPU with no
tensor cores. The levers that cut compute paid off; the lever that cut weight traffic
did not. Step count is strictly linear and behaves exactly as arithmetic predicts.

Net: **23 minutes to 9 for a 28-expression set**, 1.5 hours to 36 minutes for a party
of four. This is no longer the blocker it was.

Quality notes from the A/B sheets:

- **q4_K is visually equivalent to f16.** Same linework, faces, colour and shading.
  Images *diverge* — perturbed weights take a different denoising path, so the same
  seed lands on a different valid sample — but neither is better. RMSE and PSNR
  between them are therefore meaningless here; they measure divergence, not quality.
- **16 steps shows no degradation** against 24 in faces, hands or linework.
- **Two finer claims were made here and both were wrong.** An earlier revision said
  f16 rendered crisper small logo text and that q4_K showed faint background banding.
  Both were judged from montage tiles about 300 px wide, downscaled 3.4x from the
  1024 px originals — exactly the scale at which such detail is destroyed. Re-checked
  on native-resolution crops: all four configs render the chest logo with equivalent
  stroke weight and edge definition (the glyphs differ because the samples differ,
  not the quality), and contrast-stretched flat background regions show no banding in
  either, with q4_K actually the lower-variance of the two.

  **Method note for anyone repeating this:** compare quality on native-resolution
  crops of the region in question. A contact sheet is for spotting composition and
  identity drift, and it cannot support a claim about fine detail.
- **Unresolved:** one of three frames at 16 steps rendered its shirt trim navy instead
  of red. That is the same sporadic outfit drift already documented, and a different
  step count is a different draw — but whether *fewer steps raises the drift rate* is
  not answerable from three frames. If a 28-set at 16 steps needs noticeably more
  rerolls than at 24, that erases the saving. Worth measuring before 16 becomes the
  shipped default; 20-24 steps is the conservative choice until then.

### The chosen config, and what the speed table got wrong (2026-10-05)

**Decision: q4_K at 24 steps, no `--diffusion-fa`.** 43 s a render, 20 minutes for a
28-expression set, 80 minutes for a party of four. Chosen by the author after viewing
all four configs at native resolution.

The table above is still correct about *time* and was badly wrong about *quality*. Both
speed levers it recommended cost image quality:

- **`--diffusion-fa` renders flat.** Compared side by side against the same seed without
  it, the flash-attention output loses depth and dimensionality. The 37% it saves is not
  free, which retires it as a default.
- **16 steps produces anatomical failure.** The laughing frame came out with missing
  fingers. An earlier revision of this file asserted 16 steps showed "no degradation in
  faces, hands or linework" — hands are exactly where it broke.

**So the honest speed story is 49 s -> 43 s, a 12% gain from quantisation alone**, not
the 49 s -> 19.4 s the stacked configs implied. Sprite rendering is roughly 20 minutes a
character and that is the number to plan against.

**The process failure is worth more than the result.** Three times in one session a
quality judgement was made from contact-sheet tiles downscaled 3.4x from the originals,
and all three were wrong — logo crispness, background banding, and now flatness and
hands. The method note above was written after the first two and then not followed.

Quality is judged at native resolution, by the person whose product it is. Measured
numbers — timings, file sizes, render counts — are the useful contribution here.
Verdicts on how images look are not.
