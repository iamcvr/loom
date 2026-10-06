# Writing a story for loom

A story is a directory in here containing `story.yaml`. That is the whole
contract — no registration, no code, no restart. `story.load()` caches on the
file's mtime and `stories/` is bind-mounted into the container, so **saving the
file is deploying it**. Reload the browser and the new story is on the boot
screen.

The directory name is the story id. It is validated, never sanitised
(`story.check_id`): lowercase letters, digits, hyphen and underscore, starting
with a letter or digit, at most 64 characters. `stories/my-story/story.yaml`
gives you a story with id `my-story`.

**Do not want to write one by hand?** [`INTERVIEW.md`](INTERVIEW.md) is a prompt you
paste into any capable model. It interviews you — sixteen questions about tone, cast,
pacing and stakes — and writes the `story.yaml` for you. The schema below is still
worth reading afterwards, because you will want to edit what it produces.

**`seiran` ships with loom as a worked example.** It is a finished story rather than
a template — 5,207 characters of rules, fifteen cast members, a lorebook, a stat and
an opening — and the fastest way to understand any section below is to open it in the
editor and read what it actually does. Everything in this guide is illustrated from
it. Duplicate it if you want to start from something that works rather than from
nothing.

---

## Three ways to make one

### 1. Ask Claude to write it

The intended path for a story built from scratch. See
[Briefing Claude](#briefing-claude) below for what to say and what you get back.

### 2. The built-in editor

The boot screen → **＋**. Sections: Story / Intros / Stats /
Keywords / Cast. **Check** runs full validation *and* the context-budget fit
report without writing anything. **Save** writes `story.yaml` atomically and
keeps the previous version as `story.yaml.bak`.

Two limitations worth knowing:

- The editor has **no UI for `protagonist:` or `pacing:`**. It round-trips them
  safely — they survive a save untouched — it just won't show them. Those blocks
  are edited in the file.
- Saving rewrites the file from parsed data, which **drops YAML comments**. If
  you hand-wrote a commented file, an editor save flattens it. The `.bak` is
  your undo.

There is also **Duplicate** (`/api/story/duplicate`), which copies a story and
its `media/` under a new id. Fastest start when the new story is a variant of an
existing one — that is how the three Veyras relate.

### 3. Write the YAML by hand

Fully supported and often better for prose-heavy work. The loader was designed
around hand-editing: `|` block scalars everywhere, hot reload, and validation
that reports every problem at once instead of stopping at the first.

---

## Briefing Claude

When you say *"write me a new loom story about X"*, this is the working
agreement.

### What Claude needs from you

Enough to write from. Missing pieces get invented and flagged, not asked about
one at a time:

| Ask | Why it matters |
|---|---|
| The premise, in a sentence or two | Becomes `tagline` and drives everything else |
| Tone and register | The single biggest lever in `rules`. "Warm anime ensemble" and "grim survival" produce completely different files |
| Is the player character fixed or built at session start? | Decides whether the story gets a `protagonist:` block. Fixed → written into the prose; built → character creation screen |
| What the story tracks as a number, if anything | `stats`. Zero is a valid answer; Seiran tracks exactly one |
| Whether it should force action on a cadence | `pacing.action_every`. Off by default — see the caveat below |
| Named characters you already have in mind | `cast` entries with image prompts |
| Anything that must never drift | `keywords` notes, `always: true` for the handful that must be in every prompt |

### What Claude produces

A complete `stories/<id>/story.yaml`, written straight to disk, that:

- passes validation with zero problems,
- fits the budget (`rules` layer under 11,000 characters for **every** intro),
- follows the conventions in [The rules block](#the-rules-block) below,
- uses `{{token}}` substitution correctly if it declares a protagonist.

Then Claude runs the validation command below and reports the actual numbers —
character counts against ceilings, not a claim that it "should fit".

### What Claude will not do without asking

- Overwrite an existing story id. New story, new directory.
- Edit a story you are mid-session on without saying so — a live session reloads
  the file on its next turn, so a `rules` change lands immediately and mid-scene.
- Delete anything. Destructive work gets a tarball to
  `~/backups/` first.

### Reviewing what came back

The three things worth eyeballing, in order:

1. **Read the prologue aloud.** It is the only text a player sees before they
   type anything, and it is stored as a real message at session start — editing
   the file later does not change a session that has already begun.
2. **Check the `rules` fit number.** Under the ceiling but close (Seiran runs
   10,012/11,000) means there is no room to add world details later without
   something silently falling out of every turn.
3. **Check the `always: true` notes.** Every one of them is permanent prompt
   cost on every turn, forever. Two or three is normal; eight is a mistake.

---

## Field reference

Required: `name`, `rules`, and a non-empty `intros` list whose entries each have
`id`, `name` and `prologue`. Everything else is optional.

```yaml
name: Seiran                       # shown on the boot card
tagline: One in a thousand has a Quirk.   # one line under the name
title_image: ""                    # optional, path under this story's media/

rules: |                           # THE system prompt. See below.
  ...

details: |                         # world reference, rendered as "## The world"
  ...

style_prompt: modern anime illustration, vibrant, superhero
                                   # appended to every ComfyUI image prompt

checkpoint: ""                     # optional; which ComfyUI model renders this
                                   #   story. Empty = config.COMFY_CHECKPOINT
quality_prompt: ""                 # optional; replaces config.IMG_QUALITY_TAGS
negative_prompt: ""                # optional; replaces config.IMG_NEGATIVE

mode: play                         # play (default) | narrative

pacing:                            # optional; harness-enforced action cadence
  action_every: 0                  # turns without violence before a nudge; 0 = off
  action_kind: a fight             # what to bring in

arc:                               # optional; the only thing that knows the
  advance: chapter                 #   story can END. chapter | manual
  acts:
    - id: convenience
      name: Two Men Walking
      chapters: 1                  # how many chapters this act spans
      shape: |                     # what this act is FOR — injected every turn
        ...                        #   while the act is current

protagonist:                       # optional; presence makes the story
  name: Arata Shindo               #   character-creatable
  short: Arata
  pronouns: he/him
  description: |
    ...
  power_label: Quirk               # the noun this world uses for abilities
  power_name: Understudy
  power: |
    ...
  prompt: 1boy, solo, 19 years old, ...   # booru tags for the portrait

intros:                            # one or more entry points
  - id: move_in_day                # unique within the story
    name: Move-in day              # shown on the intro picker
    prologue: |                    # PLAYER-FACING. Stored as message 0.
      ...
    opening_scene: |               # MODEL-FACING. "## How this began"
      ...
    play_guide: |                  # PLAYER-FACING. How to play this story.
      ...
    suggestions:                   # starter buttons under the composer
      - Side with Mika. The wall stays up.

stats:
  - key: yen                       # stable identifier; the delta extractor uses it
    name: Yen                      # display label
    unit: "¥"                      # optional suffix
    min: 0
    max: 9999999
    default: 180000
    description: |                 # read by the extractor every turn — this is
      ...                          #   where you write the rules for movement

keywords:                          # the lorebook, authored up front
  - title: The fourth floor
    keywords: [fourth floor, the others, kirigaya]   # lowercased triggers
    always: true                   # optional; pin into every prompt
    body: |
      ...

cast:
  - name: Mika Onoe                # canonical name
    short: the short girl with steaming hands       # how narration refers to
    aliases: [Mika, Onoe, Kilnhand]                 #   her before you learn it
    prompt: 1girl, solo, 19 years old, ...          # booru tags for the portrait
```

### What the runtime actually does with each field

Understanding this is the difference between a story that behaves and one that
drifts.

**`rules`** — goes into the prompt on every single turn, first, at highest
priority after the director's note. Cost is permanent. This is where voice
lives.

**`details`** — same layer as `rules`, immediately after it, headed
`## The world`. Same permanent cost. Anything that is not needed every turn
belongs in `keywords` instead, where it fires only when relevant.

**`opening_scene`** — third item in the same layer. It is the model's briefing
on the situation the prologue just described: who is present, what time it is,
what has and has not happened yet. Not player-facing.

**`prologue`** — written into the transcript as an assistant message at turn 0,
with `{{tokens}}` already substituted. It is a real message: it can be edited in
the UI afterwards, and changing the story file does not retroactively change a
session that already started.

**`keywords`** — matched case-insensitively against the last
`KEYWORD_SCAN_TURNS` (4) turns of transcript. Matches are ordered by *most
recently mentioned*, `always: true` entries lead, and at most
`KEYWORD_MAX_NOTES` (10) are injected. Story notes and lorebook entries the
session earned during play compete in the same pool, ranked together
(`memory.match_keywords`) — a note written up front does not automatically
outrank one the story earned.

**`cast`** — three separate jobs. `prompt` feeds ComfyUI. `aliases` resolve a
name to the canonical entry so a character is not rendered twice. `short` is
handed to the state extractor every turn so that "the half-elf" gets attributed
to the right person — without it, the extractor guesses, and it guesses wrong.
Only cast members get portraits automatically (`PORTRAIT_CAST_ONLY`); anyone
else is one click in the cast panel.

> **`cast[].prompt` is an image prompt and NOTHING ELSE.** The narrator model
> never sees it — `assemble.build` has no cast layer, and the only other reader
> is the extractor, which takes `name` and `short`. A story once put thirteen
> hundred words of characterisation in there, and every word of it was invisible
> to the model writing the prose while being fed verbatim to a booru-tag
> checkpoint, which picked out "sword", "cloak" and "king" and drew exactly
> that. **Characterisation belongs in `keywords`** — that layer has a 12,000
> character ceiling, an `always: true` flag for the leads, and it is in the
> prompt. Tags go in `cast`.

**A session's own portrait description beats the story file**, for cast members
too. Write it in the cast panel when one character comes out wrong and you do
not want to change the story for every other session. This used to apply only to
people the story did *not* define, which made the box silently inert for exactly
the characters you most wanted to correct.

**`checkpoint` / `quality_prompt` / `negative_prompt`** — the render side of a
story's look, and they move together. The defaults in `config.py` are an
Illustrious anime checkpoint plus booru quality boosters (`masterpiece, best
quality, very aesthetic`), which are not neutral: they pull hard toward anime, so
naming a photoreal checkpoint without also replacing the quality tags does half
a job. Set all three or none. Whatever you cannot say positively — a genre, a
costume, a body type the model defaults to and you do not want — goes in
`negative_prompt`; CLIP has no negation, so "no armour" in the positive prompt
draws armour.

**`stats`** — each `description` is shown to the utility model every turn,
alongside the *current* value and an explicit `[AT MAXIMUM]` / `[AT MINIMUM]`
marker. The model returns deltas, which are clamped to `min..max`. Write the
description as the rules for what moves the number and why, not as flavour text.
Fewer stats work better: Seiran tracks one.

**`protagonist`** — its presence is the switch. With it, the story shows a
character-creation screen at session start, the block becomes the *default*, and
each session stores its own editable copy. The player character is deliberately
**not** in `cast:` — they are synthesised into it per session
(`story.cast_with`), so a rename does not leave a stale entry. It also gets its
own prompt layer with its own 4,500-character allowance, so a long story bible
cannot push the player's own character description out of the prompt.

**`{{tokens}}`** — resolved once, at the end, on the finished system prompt
(`assemble.build`), so any layer can reference the player: `{{name}}`,
`{{short}}`, `{{power}}` (the power's name), `{{powers}}` (the world's word for
powers), plus the pronoun set `{{they}} {{them}} {{their}} {{theirs}}
{{themself}}` and `{{pronouns}}`. Capitalisation is matched — `{{They}}` renders
"He". An unknown token is left visible in the prompt rather than blanked, so a
typo shows up instead of silently deleting a word.

**`pacing`** — a counter in the harness, not an instruction in prose, because
the model sees the current chapter's transcript with no turn numbers on it and
cannot measure "how long since the last fight". After `action_every` quiet turns
a soft nudge enters the prompt; at `action_every + max(2, action_every//2)` it
escalates to a hard demand injected at *depth* — appended to the last user
message rather than the system prompt, because system text loses to a vivid
scene sitting right before the generation point.

> **Caveat before you use `pacing`:** no story currently turns it on, and the nudge
> templates in `memory.py` are still written in Seiran's voice — the firm one
> literally says *"This is a city with Quirks in it"*. Turning it on in a
> non-superhero story will put that sentence in your prompt. Fix the templates
> in `memory.py` (`_ACTION_NUDGE`, `_ACTION_NUDGE_FIRM`, `action_demand`) at the
> same time, or leave `action_every: 0`.

---

## Narrative mode

`mode: narrative` means there is no player: the model writes every character and
the reader watches. It still parses and still runs, but it was never developed past
a sketch and the editor does not offer it — `play` is the only mode on the menu.

What changes:

- **The rules block inverts.** Instead of *never write the player's actions*, it
  says *write everyone*. Second person is wrong; pick a third-person voice.
- **The composer becomes the director's channel.** What the reader types is still
  a real message, so the rules must say what it is: *"Occasionally a line will
  arrive that is not part of the story — the reader, directing. It is not a
  character speaking. Obey it and never acknowledge it in the prose."* The UI
  labels it *Direction* and styles it apart from the prose. Empty sends are
  normal — the Send button becomes **Continue ▸**.
- **The extractor stops asking about "the player."** Relationships become how the
  leads regard each other; goals become what the leads have committed to. Handled
  automatically, but it means your `cast` order matters: the first two entries
  are named to the extractor as the leads.
- **`suggestions` become director prompts**, not player choices. *"Give them a
  quiet passage"*, not *"Go north"*.

### The arc

Without one, a narrative story wanders forever and never arrives — goals are
tactical, chapters are just compaction, and nothing in the prompt knows how much
story is left. The arc is what fixes that.

Each act's `shape` is injected on every turn while that act is current, and
nothing else is. The act's number out of the total goes in too, and the final act
is explicitly labelled as final — that last part is what lets the model *land* an
ending rather than continue past it.

`advance: chapter` walks the acts by chapter number (chapters close every 20
turns, so `chapters: 2` ≈ 40 turns of story). The act chip in the header shows
where you are; clicking it pins the story to an act, or releases it back to the
clock. Pinning wins until cleared — the counter only knows time has passed, you
know whether the beat actually landed.

Writing an act `shape` well:

- Say what the act is **for**, not what happens in it. You are briefing a
  director, not writing a plot.
- Say how it should **end**, because that is the transition the model has to aim
  at.
- Explicitly permit slowness. *"Everything this act asks for does not have to
  happen in this passage"* — otherwise a 900-character act description reads as a
  checklist for the next four paragraphs.
- Keep each act under the 2,500-character ceiling; five acts in a real story ran
  930–1,020.

---

## The rules block

Every story here converges on the same skeleton, in roughly this order. It is
worth following.

1. **Who the narrator is.** *"You are Seiran — the world and everyone in it.
   Narrator, game master, and the voice of every character. Second person,
   addressed to {{short}} as 'you'."*
2. **TONE.** The longest paragraph, and the one that does the most work. Say
   what the baseline register is, where the exceptions live, and what to return
   to. "Never grim" is a more useful instruction than three adjectives.
3. **HARD RULE — the player's autonomy.** Never write their dialogue, never
   decide their actions, never narrate their thoughts or feelings or
   attractions. Describe what happens around them, then stop.
4. **NO INTERFACE.** No status windows, no numbers in the prose, nobody in the
   world knows it is a game. (A LitRPG story that *wants* visible mechanics
   inverts this — but say so explicitly, because the default is silence.)
5. **How the player writes, and how you do not.** Accept plain prose, asterisk
   actions, quoted dialogue, bare speech — all the same thing, never remark on
   the format. And do not mirror it back: narration is clean prose, no asterisk
   stage directions.
6. **Reply length.** Every story here asks for three to five paragraphs.
   `PROSE.max_tokens` is 2,000, and a full five-paragraph turn runs 2,500–2,800
   characters, so that is already close to the ceiling.

Things that do **not** belong in `rules`, because the harness handles them and
prose instructions cannot:

- *Frequency of anything.* "Combat should be common", "mention the goal
  occasionally". The model cannot count turns. Use `pacing` and the goal nudge.
- *Long-term memory instructions.* "Remember what the player told you." The
  memory pipeline does this; the instruction just costs tokens.
- *Reference material needed only sometimes.* That is what `keywords` is for.

---

## Budget limits

The prompt is a fixed 48,000-character budget split across layers in priority
order (`config.BUDGET_LAYERS`). Two ceilings constrain a story file:

| Layer | Ceiling | What counts |
|---|---|---|
| `rules` | 11,000 | `rules` + `details` + the intro's `opening_scene`, **per intro** |
| `keyword_notes` | 12,000 | notes marked `always: true`, before any keyword match adds more |

Overshooting is the nastiest failure mode in the system because it is **silent**
and **permanent**: the arbiter fills the layer in order and drops what does not
fit, so an over-long `rules` block means the opening scene vanishes from every
turn for the life of the story, with nothing in the prose to say it happened.
That is exactly what `assemble.story_fit` exists to catch.

Where a real story sits. `seiran` ships with loom, so these are numbers you can
check yourself in the editor:

| Story | `rules` layer | always-on notes | intros | stats | notes | cast |
|---|---|---|---|---|---|---|
| `seiran` | 8,382 / 11,000 | 3,105 | 1 | 1 | 8 | 15 |

Measured across five stories written for this engine, the `rules` layer ran between
4,800 and 8,400 characters and the always-on lorebook notes between 1,400 and 4,500.
A story is comfortable well under the ceiling; the ones that got close did so by
accumulating rules rather than by needing them.

Multiple intros share one `rules` and one `details`, so each intro's
`opening_scene` is measured against the same ceiling separately — a story with
four intros has four fit checks, and the longest one is the binding constraint.

---

## Validating

Both of these read the file from disk, so they check what you actually saved.

**Everything at once** — validation problems *and* the budget fit:

```bash
docker exec loom python3 -c "
import story, assemble, json
sid = 'my-story'
print('problems:', story.editable(sid)['_problems'])
print(json.dumps(assemble.story_fit(story.load(sid)), indent=2))
"
```

`problems: []` and `"fits": true` on every row is the pass condition.

**Just does it parse and load** (host, no container needed — pyyaml is
installed):

```bash
python3 -c "import yaml,sys; yaml.safe_load(open('my-story/story.yaml'))" && echo ok
```

**In the browser:** the editor's **Check** button is the same two checks, run
through `/api/story/validate`, rendered as a list.

Validation collects every problem rather than raising on the first, so a broken
file tells you everything wrong with it in one pass.

---

## Checklist before calling a story done

- [ ] `problems: []`
- [ ] `"fits": true` for every intro and for the always-on notes
- [ ] Prologue reads well cold, and names the player correctly if the story uses
      `{{tokens}}`
- [ ] `opening_scene` states who is present and what has *not* happened yet
- [ ] Every `cast` member has a `short` — the extractor needs it
- [ ] `cast[].prompt` is tags, not prose — characterisation lives in `keywords`
- [ ] Every stat `description` says what moves the number, in both directions
- [ ] `always: true` used on two or three notes, not eight
- [ ] No keyword so generic it fires every turn (`man`, `city`, `door`)
- [ ] `suggestions` are things a player would actually type, not menu options
- [ ] Story appears on the boot screen with no error badge

---

## Gotchas

- **`id:` inside `story.yaml` is ignored.** Some stories carry one; it does
  nothing. The directory name wins.
- **`story.yml` shadows `story.yaml`** in the lookup order. A save renames any
  stray `.yml` to `.yml.bak` to prevent exactly this.
- **Editing a story mid-session takes effect on the next turn.** Hot reload has
  no session boundary. Handy for fixing a tone problem; surprising if you forgot
  a session was open.
- **Stats are not rolled back by a rewind.** Only the current value is stored,
  never the deltas, so there is nothing to reverse. Adjust by hand if it matters
  (`store.rewind_to`).
- **The `.bak` file is one deep.** Two editor saves in a row and the original is
  gone. The story library is gitignored, so git is not the safety net here.
- **`media/` is copied by Duplicate but is otherwise unused by the schema** —
  only `title_image` references it. Generated portraits and scenes live in the
  state volume (`/state/media`), not here.

---

For how any of this is implemented, and how to change it, see
[`../README.md`](../README.md).
