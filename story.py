"""Story files: load, validate, hot-reload.

A story is a directory under stories/ containing story.yaml and an optional
media/ folder. YAML rather than JSON because story content is mostly long prose
blocks, and `|` block scalars beat escaped newlines.

Validation collects every problem rather than raising on the first, so a broken
file tells you everything wrong with it in one pass.
"""
from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from config import STATE_DIR, STORIES_DIR

_cache: dict[str, tuple[float, dict]] = {}


class StoryError(Exception):
    """Raised with every validation problem found, not just the first."""

    def __init__(self, story_id: str, problems: list[str]):
        self.story_id = story_id
        self.problems = problems
        super().__init__(f"{story_id}: {len(problems)} problem(s)\n  - " + "\n  - ".join(problems))


# ---------------------------------------------------------------- validation


def _req_str(d: dict, key: str, where: str, out: list[str], *, allow_empty: bool = False) -> str:
    v = d.get(key)
    if v is None:
        out.append(f"{where}: missing required field '{key}'")
        return ""
    if not isinstance(v, str):
        out.append(f"{where}: '{key}' must be text, got {type(v).__name__}")
        return ""
    if not allow_empty and not v.strip():
        out.append(f"{where}: '{key}' is empty")
    return v


def _validate(raw: Any, story_id: str) -> tuple[dict, list[str]]:
    p: list[str] = []
    if not isinstance(raw, dict):
        return {}, [f"{story_id}: top level must be a mapping"]

    story: dict[str, Any] = {
        "id": story_id,
        "name": _req_str(raw, "name", "story", p),
        "tagline": raw.get("tagline", "") or "",
        "title_image": raw.get("title_image", "") or "",
        "rules": _req_str(raw, "rules", "story", p),
        "details": raw.get("details", "") or "",
    }

    # ---- intros
    intros_raw = raw.get("intros")
    intros: list[dict] = []
    if not isinstance(intros_raw, list) or not intros_raw:
        p.append("story: 'intros' must be a non-empty list")
    else:
        seen: set[str] = set()
        for i, it in enumerate(intros_raw):
            where = f"intro[{i}]"
            if not isinstance(it, dict):
                p.append(f"{where}: must be a mapping")
                continue
            iid = _req_str(it, "id", where, p)
            if iid in seen:
                p.append(f"{where}: duplicate intro id '{iid}'")
            seen.add(iid)
            intros.append(
                {
                    "id": iid,
                    "name": _req_str(it, "name", where, p),
                    "prologue": _req_str(it, "prologue", where, p),
                    "opening_scene": it.get("opening_scene", "") or "",
                    "play_guide": it.get("play_guide", "") or "",
                    "suggestions": [s for s in (it.get("suggestions") or []) if isinstance(s, str)],
                }
            )
    story["intros"] = intros

    # ---- stats
    stats: list[dict] = []
    for i, st in enumerate(raw.get("stats") or []):
        where = f"stat[{i}]"
        if not isinstance(st, dict):
            p.append(f"{where}: must be a mapping")
            continue
        key = _req_str(st, "key", where, p)
        name = _req_str(st, "name", where, p)
        desc = _req_str(st, "description", where, p)
        lo, hi, dflt = st.get("min"), st.get("max"), st.get("default")
        nums = {"min": lo, "max": hi, "default": dflt}
        bad = [k for k, v in nums.items() if not isinstance(v, (int, float))]
        if bad:
            p.append(f"{where} ('{key}'): {', '.join(bad)} must be numeric")
        elif not (lo <= dflt <= hi):  # type: ignore[operator]
            p.append(f"{where} ('{key}'): default {dflt} outside range {lo}..{hi}")
        stats.append(
            {
                "key": key,
                "name": name,
                "unit": st.get("unit", "") or "",
                "min": lo,
                "max": hi,
                "default": dflt,
                "description": desc,
            }
        )
    story["stats"] = stats

    # ---- keyword notes
    notes: list[dict] = []
    for i, kn in enumerate(raw.get("keywords") or []):
        where = f"keyword[{i}]"
        if not isinstance(kn, dict):
            p.append(f"{where}: must be a mapping")
            continue
        title = _req_str(kn, "title", where, p)
        body = _req_str(kn, "body", where, p)
        kws = kn.get("keywords")
        if not isinstance(kws, list) or not kws:
            p.append(f"{where} ('{title}'): 'keywords' must be a non-empty list")
            kws = []
        elif not all(isinstance(k, str) and k.strip() for k in kws):
            p.append(f"{where} ('{title}'): every keyword must be non-empty text")
            kws = [k for k in kws if isinstance(k, str) and k.strip()]
        notes.append(
            {
                "title": title,
                "keywords": [k.strip().lower() for k in kws],
                "body": body,
                "always": bool(kn.get("always", False)),
            }
        )
    story["keywords"] = notes

    # ---- cast (image prompts; optional)
    cast: list[dict] = []
    for i, ch in enumerate(raw.get("cast") or []):
        where = f"cast[{i}]"
        if not isinstance(ch, dict):
            p.append(f"{where}: must be a mapping")
            continue
        cast.append(
            {
                "name": _req_str(ch, "name", where, p),
                "prompt": _req_str(ch, "prompt", where, p),
                "aliases": [a for a in (ch.get("aliases") or []) if isinstance(a, str)],
                # How the narration is likely to refer to them before the player
                # learns their name. Without this the state extractor has to guess
                # which "the half-elf" is, and it guesses wrong.
                "short": ch.get("short", "") or "",
            }
        )
    story["cast"] = cast

    # ---- protagonist (optional)
    #
    # A story that declares one becomes character-creatable: the block is the
    # DEFAULT, and each session keeps its own editable copy. Stories without it
    # behave exactly as before, with the player character written into the prose.
    pro_raw = raw.get("protagonist")
    if pro_raw is not None:
        if not isinstance(pro_raw, dict):
            p.append("protagonist: must be a mapping")
        else:
            story["protagonist"] = {
                "name": _req_str(pro_raw, "name", "protagonist", p),
                "short": pro_raw.get("short", "") or "",
                "pronouns": pro_raw.get("pronouns", "they/them") or "they/them",
                "description": _req_str(pro_raw, "description", "protagonist", p),
                "power_label": pro_raw.get("power_label", "") or "",
                "power_name": pro_raw.get("power_name", "") or "",
                "power": pro_raw.get("power", "") or "",
                "prompt": pro_raw.get("prompt", "") or "",
            }

    # ---- pacing (optional)
    #
    # Cadence the harness enforces by counting, because the model cannot. The
    # transcript it sees is the current chapter plus a short overlap — often
    # under ten messages — and carries no turn numbers, so "make fights common"
    # is an instruction to manage a ratio it has no way to measure.
    pacing_raw = raw.get("pacing") or {}
    pacing: dict[str, Any] = {"action_every": 0, "action_kind": "a fight"}
    if not isinstance(pacing_raw, dict):
        p.append("pacing: must be a mapping")
    else:
        n = pacing_raw.get("action_every", 0) or 0
        if not isinstance(n, int) or n < 0:
            p.append("pacing: 'action_every' must be a whole number of turns (0 = off)")
        else:
            pacing["action_every"] = n
        pacing["action_kind"] = pacing_raw.get("action_kind") or "a fight"
    story["pacing"] = pacing

    # ---- mode (optional)
    #
    # "play" is a story with a player in it: the model never writes the player's
    # actions and the composer is that character's voice. "narrative" is a story
    # the reader watches: the model writes everyone, and the composer becomes the
    # director's channel. The difference is mostly authoring, but the harness has
    # to know which one it is — the state extractor asks about "the player", and
    # in a narrative story there isn't one.
    mode = raw.get("mode", "play") or "play"
    if mode not in ("play", "narrative", "raw"):
        p.append("story: 'mode' must be 'play', 'narrative' or 'raw', "
                 f"got '{mode}'")
        mode = "play"
    story["mode"] = mode

    # ---- arc (optional)
    #
    # The only thing in the prompt that knows a story can be over. Loom otherwise
    # has no concept of position: goals are tactical, chapters are compaction, and
    # the pacing counter is cadence. Without an arc a story wanders pleasantly
    # forever and never arrives, which is fatal for one whose point is arrival.
    arc_raw = raw.get("arc")
    if arc_raw is not None:
        if not isinstance(arc_raw, dict):
            p.append("arc: must be a mapping")
        else:
            advance = arc_raw.get("advance", "chapter") or "chapter"
            if advance not in ("chapter", "manual"):
                p.append(f"arc: 'advance' must be 'chapter' or 'manual', got '{advance}'")
                advance = "chapter"
            acts: list[dict] = []
            acts_raw = arc_raw.get("acts")
            if not isinstance(acts_raw, list) or not acts_raw:
                p.append("arc: 'acts' must be a non-empty list")
            else:
                seen_a: set[str] = set()
                for i, a in enumerate(acts_raw):
                    where = f"arc.acts[{i}]"
                    if not isinstance(a, dict):
                        p.append(f"{where}: must be a mapping")
                        continue
                    aid = _req_str(a, "id", where, p)
                    if aid in seen_a:
                        p.append(f"{where}: duplicate act id '{aid}'")
                    seen_a.add(aid)
                    span = a.get("chapters", 1)
                    if not isinstance(span, int) or span < 1:
                        p.append(f"{where} ('{aid}'): 'chapters' must be a whole number of "
                                 "chapters, at least 1")
                        span = 1
                    acts.append({
                        "id": aid,
                        "name": _req_str(a, "name", where, p),
                        "chapters": span,
                        "shape": _req_str(a, "shape", where, p),
                    })
            story["arc"] = {"advance": advance, "acts": acts}

    story["style_prompt"] = raw.get("style_prompt", "") or ""
    # Which ComfyUI checkpoint renders this story. Empty falls back to
    # config.COMFY_CHECKPOINT. A booru-tag anime model and a photoreal one
    # cannot both serve every story here, and the setting was global — so a
    # grounded modern story rendered in the same anime checkpoint as the
    # fantasy ones and there was no way to say otherwise short of changing it
    # for everybody.
    story["checkpoint"] = raw.get("checkpoint", "") or ""
    # The two prompt fragments that ride along with the checkpoint. The defaults
    # in config are booru quality boosters ("masterpiece, best quality, very
    # aesthetic") — they are not neutral, they pull hard toward anime, so a story
    # that picks a photoreal checkpoint and cannot also replace these has only
    # done half the job. Empty means fall back to config.
    story["quality_prompt"] = raw.get("quality_prompt", "") or ""
    story["negative_prompt"] = raw.get("negative_prompt", "") or ""
    return story, p


# ---------------------------------------------------------------- protagonist


PRONOUN_SETS: dict[str, dict[str, str]] = {
    "he/him": {"they": "he", "them": "him", "their": "his",
               "theirs": "his", "themself": "himself"},
    "she/her": {"they": "she", "them": "her", "their": "her",
                "theirs": "hers", "themself": "herself"},
    "they/them": {"they": "they", "them": "them", "their": "their",
                  "theirs": "theirs", "themself": "themself"},
    "it/its": {"they": "it", "them": "it", "their": "its",
               "theirs": "its", "themself": "itself"},
}


def pronouns(spec: str) -> dict[str, str]:
    """Full pronoun set from a "he/him"-style spec.

    Anything unrecognised is honoured rather than corrected: the first two parts
    become subject and object and the possessive forms are guessed from the
    object. A neopronoun set the player typed in should still substitute, even
    if the guess is imperfect — silently falling back to they/them would rename
    someone against their explicit instruction.
    """
    key = " ".join((spec or "").split()).lower()
    if key in PRONOUN_SETS:
        return dict(PRONOUN_SETS[key], pronouns=key)
    parts = [x for x in re.split(r"[/,]", key) if x.strip()]
    if len(parts) >= 2:
        sub, obj = parts[0].strip(), parts[1].strip()
        return {"they": sub, "them": obj, "their": obj + "s", "theirs": obj + "s",
                "themself": obj + "self", "pronouns": key}
    return dict(PRONOUN_SETS["they/them"], pronouns="they/them")


def protagonist_defaults(story: dict) -> Optional[dict]:
    """The story's declared protagonist, or None if it does not have one."""
    pro = story.get("protagonist")
    if not pro:
        return None
    out = dict(pro)
    out["short"] = out.get("short") or out["name"].split()[0]
    return out


def tokens(pro: Optional[dict]) -> dict[str, str]:
    """Substitution values for story prose."""
    if not pro:
        return {}
    pn = pronouns(pro.get("pronouns", "they/them"))
    return {
        "name": pro.get("name", ""),
        "short": pro.get("short") or (pro.get("name", "").split() or [""])[0],
        "power": pro.get("power_name", ""),
        "powers": pro.get("power_label", ""),
        "pronouns": pn["pronouns"],
        **{k: v for k, v in pn.items() if k != "pronouns"},
    }


_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z_]+)\s*\}\}")


def subst(text: str, values: dict[str, str]) -> str:
    """Replace {{token}} in story prose, matching the token's capitalisation.

    `{{they}}` gives "he", `{{They}}` gives "He" — so a token can open a sentence
    without the author writing two of everything. An unknown token is left
    exactly as written rather than blanked, so a typo is visible in the prompt
    instead of silently deleting a word.
    """
    if not text or not values:
        return text

    def one(m: "re.Match[str]") -> str:
        raw = m.group(1)
        val = values.get(raw.lower())
        if val is None:
            return m.group(0)
        return val[:1].upper() + val[1:] if raw[:1].isupper() and val else val

    return _TOKEN_RE.sub(one, text)


def cast_with(story: dict, pro: Optional[dict]) -> list[dict]:
    """The story's cast, with the session's protagonist at the front.

    The player character is deliberately NOT in the story file's `cast:` list
    when the story is character-creatable — their name is chosen per session, and
    a hard-coded cast entry would go stale the moment somebody renamed
    themselves. It is synthesised here instead, from the one record that is
    always current.
    """
    base = list(story.get("cast", []))
    if not pro:
        return base
    return [{
        "name": pro.get("name", ""),
        "short": "the player character",
        "aliases": [pro["short"]] if pro.get("short") else [],
        "prompt": pro.get("prompt", ""),
    }] + base


# ---------------------------------------------------------------- loading


def _story_file(story_id: str) -> Optional[Path]:
    d = STORIES_DIR / story_id
    for name in ("story.yaml", "story.yml"):
        f = d / name
        if f.is_file():
            return f
    return None


def load(story_id: str, *, force: bool = False) -> dict:
    """Load and validate. Cached on file mtime, so editing the file hot-reloads."""
    f = _story_file(story_id)
    if f is None:
        raise StoryError(story_id, [f"no story.yaml in {STORIES_DIR / story_id}"])

    mtime = f.stat().st_mtime
    if not force:
        hit = _cache.get(story_id)
        if hit and hit[0] == mtime:
            return hit[1]

    try:
        raw = yaml.safe_load(f.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise StoryError(story_id, [f"YAML parse error: {e}"]) from e

    story, problems = _validate(raw, story_id)
    if problems:
        raise StoryError(story_id, problems)

    story["_path"] = str(f.parent)
    story["_loaded"] = time.time()
    _cache[story_id] = (mtime, story)
    return story


def available() -> list[dict]:
    """Every story dir, with a load error attached rather than raised."""
    out: list[dict] = []
    if not STORIES_DIR.is_dir():
        return out
    for d in sorted(STORIES_DIR.iterdir()):
        if not d.is_dir() or _story_file(d.name) is None:
            continue
        try:
            s = load(d.name)
            out.append({"id": s["id"], "name": s["name"], "tagline": s["tagline"], "error": None})
        except StoryError as e:
            out.append({"id": d.name, "name": d.name, "tagline": "", "error": str(e)})
    return out


def intro(story: dict, intro_id: str) -> dict:
    for it in story["intros"]:
        if it["id"] == intro_id:
            return it
    raise StoryError(story["id"], [f"no intro '{intro_id}'"])


# ---------------------------------------------------------------- authoring


ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s or "story")[:64]


def check_id(story_id: str) -> str:
    """A story id becomes a directory name, so it is validated, never sanitised.

    Quietly rewriting a bad id would let '../../etc' become 'etc' and succeed,
    which is the wrong shape of helpful.
    """
    sid = (story_id or "").strip()
    if not ID_RE.match(sid):
        raise StoryError(sid or "(blank)", [
            "id must be lowercase letters, digits, hyphen or underscore, "
            "start with a letter or digit, and be at most 64 characters"
        ])
    return sid


def parse(raw: Any, story_id: str) -> tuple[dict, list[str]]:
    """Normalise a story mapping and collect every problem, without touching disk."""
    return _validate(raw, story_id)


def validate_raw(raw: Any, story_id: str) -> list[str]:
    """Every problem with a story mapping, without touching disk."""
    return _validate(raw, story_id)[1]


class _Dumper(yaml.SafeDumper):
    """Emits multi-line prose as `|` block scalars instead of escaped one-liners.

    Story files are meant to stay hand-editable. A rules block round-tripped
    through the editor should still look like the one that was written by hand.
    """


def _repr_str(dumper: yaml.SafeDumper, data: str) -> Any:
    if "\n" in data:
        # A block scalar cannot represent trailing whitespace, so PyYAML would
        # silently fall back to quoted style for the whole block. Strip it.
        clean = "\n".join(line.rstrip() for line in data.splitlines())
        return dumper.represent_scalar("tag:yaml.org,2002:str", clean + "\n", style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_Dumper.add_representer(str, _repr_str)


def _ordered(story: dict) -> dict:
    """Field order for the written file — the order an author reads them in."""
    out: dict[str, Any] = {
        "name": story["name"],
        "tagline": story.get("tagline", ""),
        "rules": story["rules"],
    }
    for key in ("details", "style_prompt", "checkpoint", "quality_prompt",
                "negative_prompt", "title_image"):
        if story.get(key):
            out[key] = story[key]
    if story.get("mode") and story["mode"] != "play":
        out["mode"] = story["mode"]
    if story.get("arc", {}).get("acts"):
        out["arc"] = {
            "advance": story["arc"]["advance"],
            "acts": [{k: a[k] for k in ("id", "name", "chapters", "shape")
                      if a.get(k) not in ("", None)} for a in story["arc"]["acts"]],
        }
    if (story.get("pacing") or {}).get("action_every"):
        out["pacing"] = {k: v for k, v in story["pacing"].items() if v}
    if story.get("protagonist"):
        out["protagonist"] = {
            k: story["protagonist"][k]
            for k in ("name", "short", "pronouns", "description",
                      "power_label", "power_name", "power", "prompt")
            if story["protagonist"].get(k) not in ("", None)
        }
    out["intros"] = [
        {k: it[k]
         for k in ("id", "name", "prologue", "opening_scene", "play_guide", "suggestions")
         if k in ("id", "name", "prologue") or it.get(k) not in ("", [], None)}
        for it in story["intros"]
    ]
    if story.get("stats"):
        out["stats"] = [
            {k: st[k] for k in ("key", "name", "unit", "min", "max", "default", "description")
             if st.get(k) not in ("", None)}
            for st in story["stats"]
        ]
    if story.get("keywords"):
        out["keywords"] = [
            {"title": kn["title"], "keywords": kn["keywords"],
             **({"always": True} if kn.get("always") else {}),
             "body": kn["body"]}
            for kn in story["keywords"]
        ]
    if story.get("cast"):
        out["cast"] = [
            {k: ch[k] for k in ("name", "short", "aliases", "prompt")
             if ch.get(k) not in ("", [], None)}
            for ch in story["cast"]
        ]
    return out


def save(story_id: str, raw: Any, *, create: bool = False) -> dict:
    """Validate then write stories/<id>/story.yaml. Returns the loaded story.

    Validation happens before anything touches disk, so a rejected save leaves
    the existing file exactly as it was.
    """
    sid = check_id(story_id)
    story, problems = _validate(raw, sid)
    if problems:
        raise StoryError(sid, problems)

    d = STORIES_DIR / sid
    f = d / "story.yaml"
    if create and _story_file(sid) is not None:
        raise StoryError(sid, [f"a story called '{sid}' already exists"])

    d.mkdir(parents=True, exist_ok=True)
    text = yaml.dump(_ordered(story), Dumper=_Dumper, sort_keys=False,
                     allow_unicode=True, width=100, default_flow_style=False)

    # Keep the previous version. A save rewrites the file from the parsed data,
    # which drops YAML comments — hand-written stories usually have some, and
    # losing them silently to a UI round-trip would be a nasty surprise.
    if f.is_file():
        shutil.copy2(f, d / "story.yaml.bak")

    # Write beside the target and rename, so an interrupted save can't leave a
    # half-written story.yaml that then fails to parse on the next load.
    tmp = d / "story.yaml.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(f)

    # An older story.yml would win the lookup order and shadow what we just wrote.
    alt = d / "story.yml"
    if alt.is_file():
        alt.replace(d / "story.yml.bak")

    _cache.pop(sid, None)
    return load(sid, force=True)


def duplicate(source_id: str, new_id: str, new_name: str = "") -> dict:
    """Copy a story, media and all, under a new id."""
    src = load(source_id)
    sid = check_id(new_id)
    if _story_file(sid) is not None:
        raise StoryError(sid, [f"a story called '{sid}' already exists"])
    raw = {k: v for k, v in src.items() if not k.startswith("_") and k != "id"}
    if new_name:
        raw["name"] = new_name
    out = save(sid, raw, create=True)
    media = Path(src["_path"]) / "media"
    if media.is_dir():
        shutil.copytree(media, STORIES_DIR / sid / "media", dirs_exist_ok=True)
    return out


def sessions_using(story_id: str) -> list[dict]:
    """Sessions that would be orphaned by deleting this story."""
    import store
    return [s for s in store.list_sessions() if s.get("story_id") == story_id]


def delete(story_id: str) -> dict:
    """Archive the story directory, then remove it.

    Never an unrecoverable rm. ~/Projects/CLAUDE.md requires destructive work to
    leave a tarball behind, and the archive goes under STATE_DIR because that is
    the only host-visible path the container can write to.

    Sessions are deliberately NOT deleted. A session holds its own transcript and
    is the thing with hours in it; the story file is a few kilobytes of setup.
    Orphaning one is recoverable by restoring the tarball, whereas deleting it is
    not recoverable by anything.
    """
    import tarfile
    import time as _t

    sid = check_id(story_id)
    path = STORIES_DIR / sid
    if not (path / "story.yaml").exists():
        raise StoryError(sid, [f"no story called '{sid}'"])

    out = STATE_DIR / "deleted-stories"
    out.mkdir(parents=True, exist_ok=True)
    archive = out / f"{sid}-{_t.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(path, arcname=sid)

    shutil.rmtree(path)
    _cache.pop(sid, None)
    return {"id": sid, "archive": str(archive),
            "orphaned": len(sessions_using(sid))}


def editable(story_id: str) -> dict:
    """The story as the editor wants it: validated shape, problems attached.

    Loads even when invalid — you cannot fix a broken story in an editor that
    refuses to open it.
    """
    f = _story_file(story_id)
    if f is None:
        raise StoryError(story_id, [f"no story.yaml in {STORIES_DIR / story_id}"])
    try:
        raw = yaml.safe_load(f.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise StoryError(story_id, [f"YAML parse error: {e}"]) from e
    story, problems = _validate(raw, story_id)
    # _validate bails early on a file that isn't a mapping at all, returning {}.
    # The editor still has to render something, so fill in the missing shape.
    story = {**blank(), **story, "id": story_id}
    story["_problems"] = problems
    story["_path"] = str(f.parent)
    return story


def blank(name: str = "") -> dict:
    """A new story with the required fields present and nothing invented."""
    return {
        "name": name,
        "tagline": "",
        "rules": "",
        "details": "",
        "style_prompt": "",
        "intros": [{
            "id": "start", "name": "Start", "prologue": "",
            "opening_scene": "", "play_guide": "", "suggestions": [],
        }],
        "stats": [],
        "keywords": [],
        "cast": [],
    }


def cast_member(story: dict, name: str) -> Optional[dict]:
    """Match a character by name or alias, case-insensitively."""
    n = name.strip().lower()
    for ch in story.get("cast", []):
        if ch["name"].strip().lower() == n:
            return ch
        if any(a.strip().lower() == n for a in ch.get("aliases", [])):
            return ch
    return None
