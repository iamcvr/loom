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

### 4. Priority order is not cache order

llama-server (and ollama) reuse the KV cache on an **unchanged prefix**. The
arbiter re-allocates every layer each turn, and the volatile layers — `ledger`,
`chapters`, `state`, `temp_memory` — sit *early*, ahead of the transcript. When
any of them changes, everything after it re-prefills.

The `ledger` already renders at DEPTH rather than in the system block, so the
mechanism for separating priority from position exists. Worth measuring the real
cache hit rate across turns before changing anything.

### 5. Re-evaluate `BUDGET_TOTAL`

It is 22,000 characters only because `num_ctx` is 8192. ollama already holds
Cydonia at **ctx=32768**, so raising `num_ctx` lifts the ceiling to roughly
99,000 characters. Do that first, measure, then choose a number deliberately —
the current value is a constraint artefact, not a decision.

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

---

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
