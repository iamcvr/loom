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

### 3. The arbiter is optimising the wrong resource

`BUDGET_TOTAL` rations prompt characters because frontier APIs bill per input
token. Measured locally (`MEASUREMENTS.md`), **generation costs ~13× more per
token than prefill**, and prefill is KV-cached between turns. So the scarce
resource is `PROSE.max_tokens`, not `BUDGET_TOTAL` — close to an inversion of the
current design.

Worth considering: express the budget as a **latency target** ("turns under 90
seconds") and derive the character budget from a measured prefill/generation rate
at startup. That is also a far better knob for a stranger on unknown hardware
than a character count.

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
