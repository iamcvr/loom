# TODO

Ordered by what blocks a public release. Context for most of these is in
`MEASUREMENTS.md`.

loom went local-only on 2026-10-05: the `anthropic` and `xai` providers, the
`PROSE.thinking` knob, `API_KEYS` and the entire refusal-fallback subsystem were
removed. Providers are now `ollama` and `openai_compat`.

---

## Blocking a first release

### 1. First-install setup guide

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
- `num_ctx` vs `BUDGET_TOTAL`, and the arithmetic that relates them (see §3)
- `LOOM_OLLAMA_URL` / `LOOM_EMBED_URL` / `LOOM_STATE` / `LOOM_STORIES`

Open question: should first run **prompt** rather than document? An unconfigured
loom currently fails at the first turn with a provider error, which is a poor
first impression. A setup screen that lists what ollama actually holds would be
better than any README section.

### 2. README is stale

It documents things that no longer exist:

| line | claim | status |
|---|---|---|
| 133 | `fallback` in the SSE event list | event removed |
| 147 | "because Anthropic rejects one that…" | reason now provider-neutral |
| 408–427 | Anthropic `/v1/messages`, xAI, `output_config.format` | providers removed |
| 432–438 | "Refusals go to another provider", `PROSE_FALLBACK` | subsystem removed |
| 445 | `claude-haiku-4-5` alias as the worked example | model removed |
| 723 | "A refusal mid-stream is still fatal" | no longer applicable |

Also: **"Running it" is Docker-only** (`cd ~/docker && docker compose up`), but
loom now runs natively on solos as a systemd `--user` unit. Both deployments need
documenting, and the native one is the one a stranger will try first.

### 3. Guard the `num_ctx` / `BUDGET_TOTAL` relationship

ollama truncates the front of an oversized prompt **silently** — the story reads
as having developed amnesia and the model takes the blame. The constraint is:

```
(BUDGET_TOTAL / 3.15) + PROSE.max_tokens  <  PROSE.num_ctx
```

On 2026-10-05 the live config sat **8 tokens** inside it (22,000 chars ≈ 6,984
tokens, + 1,200 max_tokens = 8,184, against num_ctx 8192). Nothing warned.

This should be a startup check and a settings-save validation, not a sentence in
a help string. `settings.py` already validates the layer-floor table — same
treatment.

### 4. `OLLAMA_URL` defaults to a Docker service name

`config.py` defaults `LOOM_OLLAMA_URL` and `LOOM_EMBED_URL` to
`http://ollama:11434`. That hostname does not resolve outside the compose
network, so a native install fails until the env vars are set — and the failure
reads as "ollama is down" rather than "that hostname is wrong".
`http://127.0.0.1:11434` is the better default; Docker can override.

### 5. Repo hygiene

- `__pycache__/` is untracked but not ignored — needs a `.gitignore`
- `story.py:628` cites `~/Projects/CLAUDE.md` in a docstring: a local dev
  artifact that will mean nothing to anyone else
- **no git remote configured** — versioning is local-only

---

## Design work, now that it is local-first

### 6. The arbiter is optimising the wrong resource

`BUDGET_TOTAL` rations prompt characters because frontier APIs bill per input
token. Measured locally (`MEASUREMENTS.md`), **generation costs ~13× more per
token than prefill**, and prefill is KV-cached between turns. So the scarce
resource is `PROSE.max_tokens`, not `BUDGET_TOTAL` — close to an inversion of the
current design.

Worth considering: express the budget as a **latency target** ("turns under 90
seconds") and derive the character budget from a measured prefill/generation rate
at startup. That is also a far better knob for a stranger on unknown hardware
than a character count.

### 7. Priority order is not cache order

llama-server (and ollama) reuse the KV cache on an **unchanged prefix**. The
arbiter re-allocates every layer each turn, and the volatile layers — `ledger`,
`chapters`, `state`, `temp_memory` — sit *early*, ahead of the transcript. When
any of them changes, everything after it re-prefills.

The `ledger` already renders at DEPTH rather than in the system block, so the
mechanism for separating priority from position exists. Worth measuring the real
cache hit rate across turns before changing anything.

### 8. Re-evaluate `BUDGET_TOTAL` once §3 is fixed

It is 22,000 characters only because `num_ctx` is 8192. ollama already holds
Cydonia at **ctx=32768**, so raising `num_ctx` lifts the ceiling to roughly
99,000 characters. Do that first, measure, then choose a number deliberately —
the current value is a constraint artefact, not a decision.
