"""Image generation, entirely off the turn loop.

A single background worker drains a queue. Nothing here is ever awaited by a
reply: prose streams immediately and images arrive when they arrive. A render
takes 20-40 s on a 4070 and 43 s on a Strix Halo iGPU, and must never be in the
critical path.

Portraits are cached per character name forever. Scene art is debounced by
config.SCENE_MIN_TURN_GAP so a chatty stretch doesn't queue twenty renders.
"""
from __future__ import annotations

import base64
import json
import queue
import random
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import config
import store
from story import cast_member

_q: "queue.Queue[dict]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_lock = threading.Lock()
_state: dict[str, Any] = {"queued": 0, "rendering": None, "last_error": None, "done": 0}
_last_scene_turn: dict[int, int] = {}

# Portrait candidates offered during character creation, before a session exists to
# own them. Deliberately in memory and not in the media table: they are proposals,
# most of them are discarded, and a restart mid-creation losing them is a cheaper
# outcome than rows nobody ever cleans up. The chosen one is copied into the
# session's media when the session is created.
_cands: dict[str, dict] = {}


# ---------------------------------------------------------------- image server


def _render(prompt: str, size: dict, seed: int, prefix: str = "", checkpoint: str = "",
            negative: str = "", timeout: int = 600) -> bytes:
    """One image, synchronously, as PNG bytes.

    Speaks the AUTOMATIC1111 `/sdapi/v1/txt2img` shape, which stable-diffusion.cpp's
    server implements along with A1111 itself, Forge and reForge. That matters more
    than elegance: loom is not tied to one backend, and anyone who already runs a
    local image server probably already speaks this.

    It replaced a ComfyUI client that submitted a hardcoded graph, polled /history
    until the job appeared and then fetched the file over /view. This is one request
    and one response, and the whole thing is shorter than the polling loop was.

    `prefix` is vestigial -- ComfyUI needed an output filename prefix and nothing
    here does. Kept so the caller does not have to change.

    `checkpoint` is accepted and ignored: an sd-server process loads one model at
    startup and this API cannot switch it per request, so a story's own checkpoint
    field has no effect until there is a way to honour it. Silently rendering in the
    wrong model would be worse than this comment.
    """
    body = {
        "prompt": prompt,
        "negative_prompt": negative,
        "width": int(size.get("width", 768)),
        "height": int(size.get("height", 1024)),
        "steps": int(config.IMG_STEPS),
        "cfg_scale": float(config.IMG_CFG),
        "sampler_name": config.IMG_SAMPLER,
        "clip_skip": int(config.IMG_CLIP_SKIP),
        "seed": int(seed),
        "batch_size": 1,
    }
    url = config.IMAGE_URL.rstrip("/") + "/sdapi/v1/txt2img"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    # Generous: 20 s on a 4070, measured 43 s on a Strix Halo iGPU, and a machine
    # slower than either should fail because it is broken rather than because the
    # client gave up on it.
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read().decode())
    images = out.get("images") or []
    if not images:
        raise RuntimeError("image server returned no image")
    return base64.b64decode(images[0])


# ---------------------------------------------------------------- worker


def _slug(subject: str) -> str:
    """ASCII-only filename fragment.

    str.isalnum() is true for accented letters, so "Ōbayashi" used to survive
    into the filename verbatim. That is legal on disk and legal over HTTP, but it
    put a percent-encoded path in the browser's request and only worked if every
    hop decoded it identically — which one of them did not. Folding to ASCII
    removes the whole question. Existing files keep their names and are served
    correctly by the decode in server._media.
    """
    # NFKD splits "Ō" into "O" + a combining macron; drop the mark rather than
    # letting it become an underscore, so the name stays readable.
    flat = "".join(c for c in unicodedata.normalize("NFKD", subject or "")
                   if not unicodedata.combining(c))
    keep = [c if (c.isalnum() and c.isascii()) or c in "-_" else "_" for c in flat]
    out = "_".join(filter(None, "".join(keep).split("_")))
    return (out or "x")[:60]


def _handle(job: dict) -> None:
    kind, sid, subject = job["kind"], job["session_id"], job["subject"]
    size = config.IMG_SCENE if kind == "scene" else config.IMG_PORTRAIT

    data = _render(job["prompt"], size, job["seed"], f"loom/{kind}",
                   job.get("checkpoint", ""), job.get("negative", ""))

    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slug(subject)
    name = f"{sid}_{kind}_{slug}_{int(time.time())}_{job.get('seed', 0)}.png"
    path = config.MEDIA_DIR / name
    path.write_bytes(data)

    if kind == "candidate":
        with _lock:
            c = _cands.get(job["token"])
            if c is not None:
                c["images"].append(name)
        return

    store.add_media(sid, kind, subject, name, job["prompt"], job["turn"])


def _loop() -> None:
    while True:
        job = _q.get()
        with _lock:
            _state["rendering"] = f"{job['kind']}:{job['subject']}"
            _state["queued"] = _q.qsize()
        try:
            _handle(job)
            with _lock:
                _state["done"] += 1
                _state["last_error"] = None
        except Exception as e:  # never let a render kill the worker
            with _lock:
                _state["last_error"] = f"{job['kind']}:{job['subject']}: {e}"
        finally:
            with _lock:
                _state["rendering"] = None
            _q.task_done()


def start() -> None:
    global _worker
    if not config.IMAGES_ENABLED:
        return
    with _lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, name="loom-images", daemon=True)
        _worker.start()


def _enqueue(job: dict) -> None:
    if not config.IMAGES_ENABLED:
        return
    start()
    _q.put(job)
    with _lock:
        _state["queued"] = _q.qsize()


# ---------------------------------------------------------------- requests


def portrait_prompt(session_id: int, name: str) -> str:
    """The player's own description of a character the story does not define."""
    return str(store.get_meta(f"portrait:{session_id}:{name.strip().lower()}", "") or "")


def set_portrait_prompt(session_id: int, name: str, text: str) -> None:
    store.set_meta(f"portrait:{session_id}:{name.strip().lower()}", text.strip())


def request_portrait(session_id: int, story_obj: dict, name: str, turn: int,
                     *, manual: bool = False, replace: bool = False) -> bool:
    """Queue a portrait unless one already exists. Returns True if queued.

    Automatic requests are limited to characters the story actually defines.
    Without that, a two-hander accumulated eighteen portraits — one for every
    innkeeper and carter the narration happened to name — each rendered from a
    prompt with no description in it, so they came out looking like nobody in
    particular. Anyone else is available on demand from the cast panel.
    """
    name = (name or "").strip()
    if not name:
        return False

    # Resolve to the canonical name BEFORE checking the cache — media is stored
    # under the canonical name, so checking an alias first would miss and render
    # the same character twice. Resolving first also means a per-session
    # description written against an alias is found under the canonical name.
    member = cast_member(story_obj, name)
    if member:
        subject = member["name"]
    elif not manual and config.PORTRAIT_CAST_ONLY:
        return False
    else:
        subject = name

    # The session's own description wins over the story file, for ANYONE.
    # It used to apply only to people the story did not define, which made the
    # cast panel's description box silently inert for every character that
    # actually had an entry — exactly the ones whose portrait you most want to
    # correct without editing the story for every other session.
    described = portrait_prompt(session_id, subject)
    if described:
        subject_prompt = described if member else f"solo, upper body, {described}"
    elif member:
        subject_prompt = member["prompt"]
    else:
        # Anyone the story does not define has no description anywhere, so the
        # checkpoint invents one — a Guild official came back as a horned elf girl.
        # A per-session description fixes that properly; without one the result is
        # explicitly a dice roll and regenerating only rolls again.
        subject_prompt = f"solo, upper body, fantasy character portrait, {name}"

    existing = store.find_media(session_id, "portrait", subject)
    if existing and not replace:
        return False
    if existing:
        # "Regenerate" must actually replace. The cache check that stops a
        # character being rendered twice was also silently swallowing every
        # explicit request to redo one.
        store.delete_media(existing["id"])
        try:
            (config.MEDIA_DIR / existing["path"]).unlink()
        except OSError:
            pass

    style = story_obj.get("style_prompt", "")
    quality = story_obj.get("quality_prompt") or config.IMG_QUALITY_TAGS
    prompt = ", ".join(p for p in [quality, subject_prompt, style,
                                   "upper body portrait, simple background"] if p)
    _enqueue({"kind": "portrait", "session_id": session_id, "subject": subject,
              "checkpoint": story_obj.get("checkpoint", ""),
              "negative": story_obj.get("negative_prompt", ""),
              # Random on a replace, stable otherwise. A seed derived from the
              # name is right for the first render and useless for a redo — it
              # would return the identical picture and look like nothing happened.
              "prompt": prompt,
              "seed": random.randrange(2**31) if replace else abs(hash(subject)) % 2**31,
              "turn": turn})
    return True


def request_scene(session_id: int, story_obj: dict, scene_prompt: str, turn: int) -> bool:
    """Queue scene art if the debounce gap has passed."""
    scene_prompt = (scene_prompt or "").strip()
    if not scene_prompt:
        return False

    last = _last_scene_turn.get(session_id, -10_000)
    if turn - last < config.SCENE_MIN_TURN_GAP:
        return False
    _last_scene_turn[session_id] = turn

    style = story_obj.get("style_prompt", "")
    quality = story_obj.get("quality_prompt") or config.IMG_QUALITY_TAGS
    prompt = ", ".join(p for p in [quality, scene_prompt, style,
                                   "wide establishing shot, no people"] if p)
    subject = f"turn{turn}"
    _enqueue({"kind": "scene", "session_id": session_id, "subject": subject,
              "checkpoint": story_obj.get("checkpoint", ""),
              "negative": story_obj.get("negative_prompt", ""),
              "prompt": prompt, "seed": turn * 7919 % 2**31, "turn": turn})
    return True


def status() -> dict:
    with _lock:
        s = dict(_state)
    s["enabled"] = config.IMAGES_ENABLED
    s["queued"] = _q.qsize()
    return s


def reachable() -> bool:
    """Fast probe for the UI. Must never block a page load — a firewalled or absent
    image server should answer 'no' in a couple of seconds, not thirty.

    It called ComfyUI's /system_stats through a _get() helper that was deleted when
    this module moved to /sdapi/v1, so it raised NameError, the bare except swallowed
    it, and images reported unreachable no matter what was running. /sdapi/v1/samplers
    is the cheapest GET every server implementing this API answers.
    """
    if not config.IMAGES_ENABLED:
        return False
    try:
        url = config.IMAGE_URL.rstrip("/") + "/sdapi/v1/samplers"
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------- candidates


def request_candidates(prompt: str, n: int = 4, negative: str = "",
                       checkpoint: str = "") -> str:
    """Queue n portrait options and return a token to poll.

    One job each rather than one batched request, so they finish one at a time and
    the first option is on screen in about 45 seconds instead of all four arriving
    after three minutes. Waiting is more tolerable when something is happening.
    """
    token = f"c{int(time.time()*1000)}{random.randint(100, 999)}"
    with _lock:
        _cands[token] = {"images": [], "total": int(n), "error": None,
                         "started": time.time()}
    for i in range(int(n)):
        _enqueue({"kind": "candidate", "session_id": 0, "subject": "candidate",
                  "prompt": prompt, "negative": negative, "checkpoint": checkpoint,
                  "turn": 0, "seed": random.randint(1, 2_000_000_000),
                  "token": token})
    return token


def candidates(token: str) -> dict:
    """Progress so far. Images appear in this list as each render lands."""
    with _lock:
        c = _cands.get(token)
        if not c:
            return {"images": [], "total": 0, "done": 0, "error": "unknown token"}
        return {"images": list(c["images"]), "total": c["total"],
                "done": len(c["images"]), "error": c["error"],
                "elapsed": int(time.time() - c["started"])}


def claim_candidate(session_id: int, name: str, subject: str, prompt: str) -> None:
    """Attach a chosen candidate to a session as that character's portrait.

    The file is already on disk; this only records it. The unchosen ones are left
    where they are -- see sweep_candidates().
    """
    store.add_media(session_id, "portrait", subject, name, prompt, 0)


def sweep_candidates(max_age: int = 3600) -> int:
    """Delete candidate files nobody claimed. Called at startup.

    A character-creation screen that was abandoned leaves four 900 KB renders on
    disk with nothing referencing them, and nothing else would ever remove them.
    """
    claimed = set(store.all_media_paths())
    n = 0
    cutoff = time.time() - max_age
    for f in config.MEDIA_DIR.glob("0_candidate_*.png"):
        try:
            if f.name not in claimed and f.stat().st_mtime < cutoff:
                f.unlink()
                n += 1
        except OSError:
            pass
    return n
