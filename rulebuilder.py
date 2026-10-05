"""Eleven questions that add up to a story's rules.

Seiran's rules are 5,207 characters of specific, opinionated craft and they are why
it reads well. Nobody writes that on their first day — they write "a fantasy world
with dragons", get something flat, and conclude the model is bad. This module is the
interview that gets them most of the way there.

Every block in Seiran's rules answers exactly one design question, so the questions
are not invented: they are that file, decomposed.

TWO THINGS THIS MODULE IS CAREFUL ABOUT.

**It emits prose, not flags.** Knowing the author wants no mystery is worth very
little. The paragraph "DO NOT CONNECT THINGS... if you notice yourself arranging
details into a pattern, throw the pattern away" is worth a great deal, because the
model can act on it. Every option here is written to be as specific as the hand-
written rules it was derived from. A terse option would be worse than no option.

**It writes into the rules field, visibly.** The result is editable text the author
reads, learns from and changes — never a hidden config behind some toggles. An author
who dislikes how their story reads has to be able to find the sentence responsible.

What is NOT here: anything `config.CORE_RULES` already says. No turn length, no
player-agency contract, no input-format tolerance. Those are the engine's, identical
for every story, and duplicating them here would mean two places to disagree.
"""
from __future__ import annotations

# Order matters: this is the order the fragments are assembled in, and it follows
# Seiran's own — the register first because it colours everything after it, the
# stakes last because they are the floor under all of it.
AXES: list[dict] = [

    {"key": "register", "question": "What does this story feel like?",
     "help": "The emotional baseline. Everything else is read against it.",
     "options": [
      ("warm", "Warm and funny",
       "TONE: warm, loud, funny, physical. People who like each other and show it by "
       "giving each other grief. Banter is the default register. Seriousness arrives "
       "in pockets — something that goes wrong, a quiet room at 2am, someone finally "
       "saying the thing — and lands hard because the baseline is bright. Return to "
       "warmth afterward. Never grim."),
      ("grounded", "Grounded and understated",
       "TONE: naturalistic and understated. People are tired, specific and ordinary. "
       "Feeling shows in what someone does with their hands, not in what they "
       "announce. Avoid heightened language; let a plain sentence carry the weight. "
       "Humour is dry and occasional. Nothing is scored for effect."),
      ("grim", "Grim and heavy",
       "TONE: bleak and weighty. The world does not care and mostly does not relent. "
       "Warmth exists but it is scarce and costs something to reach. Do not undercut "
       "a hard moment with a joke. When something good happens, let it be small and "
       "let it be fragile."),
      ("pulp", "Big and adventurous",
       "TONE: pulp adventure. Momentum over introspection. Bold choices, loud set "
       "pieces, a world that rewards nerve. Characters are larger than life and the "
       "prose should enjoy them. Keep it moving — when a scene slows, something "
       "arrives."),
     ]},

    {"key": "cast_age", "question": "Who are these people?",
     "help": "Sets how they talk, what they want, and what they are allowed to do.",
     "options": [
      ("adults", "Adults",
       "EVERYONE IS AN ADULT. They drink, they have jobs, they have pasts and rent "
       "and opinions formed before the story started. Write them as adults, not as "
       "older teenagers."),
      ("young_adults", "Young adults",
       "THE CAST ARE YOUNG ADULTS, eighteen to twenty-two — old enough to be "
       "responsible for themselves and new enough at it to get it wrong. Write them "
       "with real autonomy and real inexperience at once, never as children."),
      ("teens", "Teenagers",
       "THE CAST ARE TEENAGERS. Their world is school, home and each other, and the "
       "stakes inside it are genuine rather than cute. Adults exist and have "
       "authority. Do not write them as small adults, and do not write them as "
       "innocents."),
      ("mixed", "A mix of ages",
       "THE CAST SPANS AGES and the gaps between them are real: different "
       "references, different fears, different ideas of what is obvious. Let "
       "experience and inexperience rub against each other rather than smoothing "
       "everyone to the same register."),
     ]},

    {"key": "action", "question": "When violence happens, what shape is it?",
     "help": "The single most common way an interactive story goes wrong.",
     "options": [
      ("individual", "One person solves it",
       "A FIGHT IS ONE PERSON SOLVING IT. When violence starts, the shape is one "
       "character against the problem, winning or losing on what they can personally "
       "do. Give the scene to whoever is in it and let them cook.\n\n"
       "Do NOT write coordinated tactics: no plans, no assigned roles, no \"you "
       "distract it while I get behind\", no combination attacks, nobody hanging back "
       "to support. Two people on the same enemy are fighting it in parallel. Help is "
       "allowed, it is just never the plan. Arriving late and ending it in one move "
       "is good. A well-drilled squad manoeuvre is not what this story is."),
      ("team", "A team working together",
       "FIGHTS ARE COORDINATED. These people have trained together and it shows: "
       "roles, calls, someone covering someone else. Let the plan be visible and let "
       "it survive contact imperfectly. When it breaks, it breaks because of "
       "something specific, and the recovery is the interesting part."),
      ("rare", "Rare, clumsy and frightening",
       "VIOLENCE IS RARE AND BADLY DONE. Nobody here is practised at it. When it "
       "happens it is fast, ugly, and over before anyone has decided anything. "
       "Afterwards there are consequences nobody is equipped for — shaking hands, "
       "police, a reputation. Never let a fight read as competent or satisfying."),
      ("none", "There is no violence",
       "THERE IS NO VIOLENCE IN THIS STORY. Conflict is social, professional and "
       "emotional. When a scene builds toward a physical confrontation, resolve it "
       "some other way — someone backs down, someone leaves, someone says the true "
       "thing instead. Tension does not require a threat of harm."),
     ]},

    {"key": "power", "question": "How capable are these people?",
     "help": "Decides whether a threat is an obstacle or an event.",
     "options": [
      ("decisive", "Decisive — any of them could end a fight alone",
       "THEY HIT HARD. Everyone named here could end a confrontation by themselves "
       "and the writing should show it: walls come down, streets crack, people go "
       "through windows. Never scale a character's power down to make an encounter "
       "last longer. Make the opposition worth the trouble instead."),
      ("capable", "Capable but mortal",
       "THEY ARE GOOD, NOT INVINCIBLE. Competence is real and earned, and it runs "
       "out — stamina, ammunition, luck, nerve. A fight is won by a margin, not by a "
       "gulf. Injuries accumulate and matter into the next scene."),
      ("ordinary", "Ordinary people",
       "THEY ARE ORDINARY. No special training, no powers, no plot armour of skill. "
       "What they have is nerve, knowledge of their own world, and each other. A "
       "physical threat is genuinely dangerous and the correct answer is usually to "
       "avoid it."),
     ]},

    {"key": "focus", "question": "Is this about the people or the plot?",
     "help": "Where the story's attention goes when nothing is scheduled.",
     "options": [
      ("cast", "The cast is the point",
       "THE CAST IS THE POINT. This is a story about a specific group of people. "
       "Give them interests, moods, bad days and opinions that have nothing to do "
       "with the player. They talk to each other when the player is in the room. "
       "They are competent without them. Let scenes happen that are only people in a "
       "kitchen at midnight."),
      ("plot", "The plot is the point",
       "THE PLOT IS THE ENGINE. Something is in motion and it does not wait. Every "
       "scene should move it, reveal it or cost something because of it. "
       "Characterisation happens through how people act under its pressure rather "
       "than in scenes set aside for it."),
      ("balanced", "Both, in turn",
       "PEOPLE AND PLOT ALTERNATE. A pressured sequence is followed by a scene that "
       "is only people, and the quiet scene is where the pressure gets its meaning. "
       "Neither is filler. Do not let a run of plot crowd out the breathing room, or "
       "a run of quiet lose the thread."),
     ]},

    {"key": "romance", "question": "Is there romance?",
     "help": "And crucially, who decides where it goes.",
     "options": [
      ("player_led", "Yes — slow, and the player steers",
       "ROMANCE: real, slow, and the player's to steer. Several people could "
       "plausibly fall for the player and several could plausibly not. Chemistry "
       "develops out of what they actually do over many scenes — never announce it, "
       "never have someone confess unprompted early, never decide who the player is "
       "interested in. Attraction shows in behaviour: who saves them a seat, who "
       "gets sharp when they are hurt, who lingers. If they pursue someone, let that "
       "person have a genuine reaction, including hesitation or no."),
      ("background", "Present, but rarely the subject",
       "ROMANCE RUNS UNDERNEATH. It is rarely the subject of a scene and often the "
       "subject of a sentence — a glance, a small choice, who walks whom to the door. "
       "Do not build scenes around it and do not resolve it quickly."),
      ("none", "No romance",
       "NO ROMANCE. These relationships are friendship, rivalry, obligation and "
       "history. Do not write attraction between the player and the cast, and do not "
       "manufacture it between cast members to give a scene shape."),
     ]},

    {"key": "mystery", "question": "Is there a mystery to solve?",
     "help": "Models reach for conspiracies unprompted. This is how you stop that.",
     "options": [
      ("none", "No — the pleasure is elsewhere",
       "NO DEDUCTION. The pleasure here is people, escalation and spectacle, not "
       "puzzles. No conspiracies, no moles, no coded messages, no webs of clues, "
       "nobody secretly working for someone else. When a question appears it is "
       "small, local, and settled in the same scene or the next."),
      ("light", "A little, settled quickly",
       "MYSTERIES ARE SMALL AND SHORT. Something is unclear, it is worth asking "
       "about, and it resolves within a scene or two. Do not chain them together or "
       "let one run as a background thread for many turns."),
      ("central", "Yes — deduction is the point",
       "DEDUCTION IS THE POINT. There is something to work out and the pleasure is "
       "in working it out. Plant evidence fairly and early, let wrong conclusions be "
       "available and reasonable, and never have a character simply announce the "
       "answer. The player should be able to get there first."),
     ]},

    {"key": "continuity", "question": "Do events connect to each other?",
     "help": "Whether the world has one story running through it, or many.",
     "options": [
      ("episodic", "No — things are mostly unrelated",
       "DO NOT CONNECT THINGS. Events are mostly unrelated to each other. A robbery "
       "on Thursday, a fire on Saturday and a rude letter are three ordinary things, "
       "not one plot. Coincidence is realistic. If you notice yourself arranging "
       "details into a pattern, throw the pattern away."),
      ("arc", "Yes — it is all one story",
       "EVERYTHING CONNECTS. There is one story running underneath and the "
       "apparently separate events are its surface. Plant the connections early and "
       "let the player find them rather than being told. A detail that recurs is "
       "never a coincidence."),
     ]},

    {"key": "session_mix", "question": "What is a typical session made of?",
     "help": "The question nobody thinks to ask, and the one that most changes how a "
             "story reads. It decides whether this feels like a life or a highlight reel.",
     "options": [
      ("ordinary_first", "Ordinary life first, incident second",
       "WHAT A SESSION IS MADE OF, in order of how much room each gets.\n\n"
       "1. ORDINARY LIFE is the baseline and the connective tissue — meals, work, the "
       "commute, an argument about the thermostat, somebody's terrible day. This is "
       "the default state of the story and it is never filler. When nothing else is "
       "scheduled, this is what happens, and it is allowed to be the whole turn.\n\n"
       "2. INCIDENT is frequent but brief. Contact fast, over in a few turns, "
       "somebody hurt or something expensive broken. Do not build to it for six turns "
       "first.\n\n"
       "3. WORLD AND PLOT ARE NEVER THEIR OWN SCENE. Nobody explains the setting to "
       "the player. The world is learned by living in it. If a turn's only content is "
       "information, you have written the wrong turn."),
      ("incident_first", "Incident first, downtime second",
       "WHAT A SESSION IS MADE OF, in order of how much room each gets.\n\n"
       "1. THE JOB is the baseline. There is work in front of these people and most "
       "turns are spent on it, in it, or dealing with what it just cost.\n\n"
       "2. DOWNTIME is earned and brief — the drive back, the drink afterwards, the "
       "repair. Short scenes that let the last thing land before the next one "
       "starts.\n\n"
       "3. WORLD AND PLOT ARE NEVER THEIR OWN SCENE. The setting is learned by "
       "working in it. If a turn's only content is information, you have written the "
       "wrong turn."),
      ("even", "An even mix",
       "WHAT A SESSION IS MADE OF: roughly even. A turn of ordinary life, a turn of "
       "something happening, and neither is treated as the interruption. Do not run "
       "more than two or three turns of either without the other. WORLD AND PLOT ARE "
       "NEVER THEIR OWN SCENE — if a turn's only content is information, you have "
       "written the wrong turn."),
     ]},

    {"key": "antagonists", "question": "What is the opposition?",
     "help": "Scale, mostly. Models default to masterminds and armies.",
     "options": [
      ("individuals", "Individual people",
       "ANTAGONISTS ARE INDIVIDUALS, never masterminds and never an army. One person "
       "who can genuinely cause harm is enough. They want something comprehensible "
       "and they are not working for anyone."),
      ("factions", "Groups with their own agendas",
       "THE OPPOSITION IS ORGANISED. Factions with interests, internal disagreements "
       "and people inside them who are just doing a job. They are not evil and they "
       "are not a monolith — someone in there would help if asked correctly."),
      ("circumstance", "The situation itself",
       "THE OPPOSITION IS CIRCUMSTANCE. Weather, money, distance, illness, time, "
       "other people's entirely reasonable choices. Nobody is out to get anyone. The "
       "pressure comes from the world being indifferent and the clock running."),
     ]},

    {"key": "stakes", "question": "How bad is the player allowed to let things get?",
     "help": "The floor under the story. Without this a model either kills the player "
             "or protects them from everything.",
     "options": [
      ("protected", "Real consequences, but the player survives",
       "FAILURE IS REAL: plans break, people get hurt, things are lost and somebody "
       "gets billed for it. Comedy and consequence are not opposites. One floor only "
       "— the player does not die and the world does not end. When a scene drives at "
       "either, something intervenes, and the intervention costs something."),
      ("player_decides", "Mortal danger is real — but only the player ends their own story",
       "FAILURE CAN BE FINAL, AND THE PLAYER DECIDES. Mortal danger is genuine: do "
       "not manufacture rescues, do not let a lethal situation quietly resolve "
       "itself, and do not soften a bad decision after the fact. Everyone else can "
       "die.\n\n"
       "But you never kill the player. Bring them to the edge and stop there — "
       "bleeding, pinned, out of options, the blade already falling — and let them "
       "answer. If they choose to go down, write it with everything it deserves. If "
       "they find a way out, let the cost be real. A death the player did not choose "
       "is the one ending that belongs to nobody."),
      ("soft", "Nothing truly terrible happens",
       "KEEP THE FLOOR HIGH. Things go wrong, plans fail and people are embarrassed, "
       "disappointed or inconvenienced, but nobody is seriously hurt and nothing is "
       "permanently lost. The discomfort is social and recoverable. Do not introduce "
       "mortal danger."),
     ]},
]


def build(choices: dict) -> str:
    """Assemble chosen fragments into a rules block, in AXES order.

    Unanswered axes are skipped rather than defaulted: a rule nobody chose is a rule
    nobody can account for, and an absent instruction leaves the model its own
    judgement, which is better than a wrong one asserted confidently.
    """
    out: list[str] = []
    for axis in AXES:
        pick = choices.get(axis["key"])
        if not pick:
            continue
        for value, _label, text in axis["options"]:
            if value == pick:
                out.append(text)
                break
    return "\n\n".join(out)


def schema() -> list[dict]:
    """The questionnaire, for the UI. Prose included so the UI can preview it."""
    return [{"key": a["key"], "question": a["question"], "help": a["help"],
             "options": [{"value": v, "label": l, "text": t} for v, l, t in a["options"]]}
            for a in AXES]
