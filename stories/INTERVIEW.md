# Simple mode — have an AI interview you and write the story file

Paste everything below the line into a capable model (Claude, GPT, whatever you
have). It will ask you a short series of questions and then produce a complete
`story.yaml`. Save that to `stories/<your-id>/story.yaml` and reload loom.

This exists because the hand-written path asks you to produce 5,000 characters of
specific, opinionated craft before you have played a single turn. Most people
cannot, and a vague ruleset produces vague stories — so the model takes the blame
for a prompt that never told it anything. An interview gets the same information
out of someone who has never written one.

---

You are going to interview me and then write a complete `story.yaml` for **loom**,
a local interactive-fiction engine. I am the author and the player; I will be
playing in the world you help me describe.

## What loom already handles — do not write any of this into the story

loom ships a `CORE_RULES` block, shared by every story, which already instructs the
narrator on all of the following. **Restating any of it in the story's `rules` wastes
budget and risks contradicting the engine.** It already covers:

- that the narrator is the world and every character in it, in second person
- that my character is mine alone — it never writes my dialogue, decides my
  actions, or narrates my thoughts or feelings
- that it must accept any input format I use without remarking on it
- that its narration is clean prose, with no asterisk actions or stage directions
- that only one thing happens at a time, and that I end scenes rather than it
- that a weighty spoken line gets its own attributed paragraph as
  `**Name** | "what they say."`
- how long a turn should run

So the story file carries **only what is true of this story**: tone, world, cast,
and the specific doctrines that make it feel like itself.

## The interview

Ask me these in small groups — three or four at a time, conversationally, not as a
form. Infer freely from what I give you and tell me what you inferred so I can
correct it. If an answer implies another, do not ask the implied one.

**The world**
1. Where and when is this? One or two sentences is plenty.
2. What is my character doing in it when we start?
3. Who are the two to five people who matter? Names, and one line each on who they
   are to me.

**The feel** — these map to specific instructions you will write
4. What does it feel like? (warm and funny / grounded and understated / grim and
   heavy / big and adventurous)
5. Are these people adults, young adults, teenagers, or a mix?
6. When violence happens, what shape is it? (one person solves it / a trained team /
   rare and clumsy and frightening / there is none)
7. How capable are they? (any of them could end a fight alone / good but mortal /
   ordinary people)
8. Is this about the people or the plot, or both in turn?
9. Romance: slow and mine to steer / present but rarely the subject / none?
10. If intimacy happens, how far does the camera follow? (fade at the door / on the
    page but not graphic / explicit)
11. Is there a mystery to solve, a little, or none?
12. Do events connect into one story, or are they mostly unrelated?
13. **What is a typical session made of?** Ordinary life first with incident second,
    incident first with downtime second, or an even mix? *Ask this one explicitly —
    nobody volunteers it, and it is what decides whether the story feels like a life
    or a highlight reel.*
14. What is the opposition? (individual people / organised factions / circumstance
    itself)
15. How bad am I allowed to let things get? (real consequences but I survive /
    mortal danger is real but only I decide my character dies / nothing truly
    terrible happens)
16. Does this world have named powers? If so, what does it call them?

Then show me a two-paragraph summary of what you understood, and let me correct it
before you write anything.

## How to write the rules

The `rules` block is the heart of it. Write it as a series of short, titled,
imperative paragraphs — the way a showrunner briefs a writers' room, not the way a
spec describes a feature.

**Be specific and opinionated.** "No conspiracies" is weak. This is strong:

> DO NOT CONNECT THINGS. Events are mostly unrelated to each other. A robbery on
> Thursday, a fire on Saturday and a rude letter are three ordinary things, not one
> plot. Coincidence is realistic. If you notice yourself arranging details into a
> pattern, throw the pattern away.

The difference is that the second one tells the model what to do when it catches
itself. Every rule should be actionable mid-sentence.

**Write a doctrine for whatever the story does most.** If there is fighting, say
exactly what a fight looks like. If it is a workplace, say what a meeting looks
like. Models default to the most generic version of any scene; the rule is where you
take that away.

**Say what NOT to do, specifically.** "No coordinated team tactics: no plans, no
assigned roles, no 'you distract it while I get behind'" works. "Keep fights simple"
does not.

## Hard limits

- `rules` + `details` + the opening scene must total **under 11,000 characters**.
  Aim for 4,000–8,000. If it overruns, loom silently drops the end of it from every
  turn and the story reads as though the model ignored you.
- Lorebook notes marked `always: true` must total **under 12,000 characters**, and
  every one of them is in every prompt forever. Two or three at most.
- `protagonist` must be **under 4,500 characters**.
- Each act's `shape` must be **under 1,500 characters**.

## The output

One fenced `yaml` block, nothing else around it. This is the complete schema;
everything not marked required can be omitted entirely.

```yaml
name: The Story Name                 # REQUIRED
tagline: One line, shown on the card
power_label: Quirk                   # omit unless this world has named powers

rules: |                             # REQUIRED — the craft, per above
  TONE: ...

  A FIGHT IS ONE PERSON SOLVING IT. ...

details: |                           # the world. Facts, not instruction.
  ...

protagonist:                         # the DEFAULT character; the player edits it
  name: Full Name
  short: What people call them
  pronouns: he/him
  description: |
    Age, background, temperament, why they are here.
  power_label: Quirk                 # only if the world has powers
  power_name: Understudy
  power: |
    What it does and — more importantly — its limits.
  prompt: >                          # image tags, not prose
    1boy, solo, black hair, grey hoodie, modern anime style

intros:                              # REQUIRED — at least one
  - id: start                        # REQUIRED, lowercase-hyphen
    name: Start                      # REQUIRED, shown on the card
    prologue: |                      # REQUIRED — the first thing shown.
      Second person, present or past, ending on an open beat with nobody
      resolved. Use two or three attributed lines to show the format:

      **Name** | "Something that lands."
    opening_scene: |                 # optional: situation given to the narrator
      Where everyone is and what is already in motion.
    suggestions:                     # optional: starter prompts for the player
      - Ask her what she meant.
    play_guide: One line on how to play this.

cast:                                # everyone the story knows about
  - name: Full Name                  # REQUIRED
    short: the tall one with the ledger   # how narration refers to them
    aliases: [Nickname]
    prompt: 1girl, dark bob, guild coat   # REQUIRED — image tags

keywords:                            # lorebook: fires when a keyword appears
  - title: The Guild                 # REQUIRED
    keywords: [guild, guildhall]     # REQUIRED, non-empty
    body: |                          # REQUIRED
      What the narrator should know when this comes up.
    always: false                    # true = in every prompt, forever

stats:                               # optional, usually zero or one
  - key: money                       # REQUIRED
    name: Coin                       # REQUIRED, shown in the UI
    description: What is in their pocket.   # REQUIRED, read by the narrator
    unit: gp                         # optional
    min: 0                           # REQUIRED, numeric
    max: 999                         # REQUIRED, numeric
    default: 12                      # REQUIRED, numeric, within min..max

arc:                                 # optional. The only thing that knows a story
  advance: chapter                   # can be over.
  acts:
    - id: act-one
      name: What It Is Called
      chapters: 3                    # how many chapters it spans
      shape: |
        What this act is for.
```

Write the `prologue` last, once everything else exists, and make it do real work:
establish the place, put two named people on the page disagreeing about something
small, and stop without resolving it.
