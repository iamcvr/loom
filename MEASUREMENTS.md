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
