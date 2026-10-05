"""ComfyUI image generation, entirely off the turn loop.

A single background worker drains a queue. Nothing here is ever awaited by a
reply: prose streams immediately and images arrive when they arrive. A render
takes 20-40s on the 4070 and must never be in the critical path.

Portraits are cached per character name forever. Scene art is debounced by
config.SCENE_MIN_TURN_GAP so a chatty stretch doesn't queue twenty renders.
"""
from __future__ import annotations

import json
import queue
import random
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
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


# ---------------------------------------------------------------- comfy client


def _post(path: str, payload: dict) -> dict:
    url = config.COMFY_URL.rstrip("/") + path
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _get(path: str, timeout: int = 30) -> dict:
    url = config.COMFY_URL.rstrip("/") + path
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _fetch_image(filename: str, subfolder: str, kind: str) -> bytes:
    q = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": kind})
    url = f"{config.COMFY_URL.rstrip('/')}/view?{q}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def _workflow(prompt: str, size: dict, seed: int, prefix: str, checkpoint: str = "",
              negative: str = "") -> dict:
    return {
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint or config.COMFY_CHECKPOINT}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": size["width"], "height": size["height"], "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative or config.IMG_NEGATIVE, "clip": ["4", 1]}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": size["steps"], "cfg": size["cfg"],
                         "sampler_name": "euler_ancestral", "scheduler": "normal",
                         "denoise": 1.0, "model": ["4", 0], "positive": ["6", 0],
                         "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": prefix, "images": ["8", 0]}},
    }


def _render(prompt: str, size: dict, seed: int, prefix: str, checkpoint: str = "",
            negative: str = "", timeout: int = 300) -> bytes:
    pid = _post("/prompt", {"prompt": _workflow(prompt, size, seed, prefix,
                                                checkpoint, negative)})["prompt_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            hist = _get(f"/history/{pid}")
        except urllib.error.URLError:
            time.sleep(2)
            continue
        if pid in hist:
            for node in hist[pid].get("outputs", {}).values():
                for img in node.get("images", []):
                    return _fetch_image(img["filename"], img.get("subfolder", ""), img.get("type", "output"))
            raise RuntimeError("comfy finished with no image")
        time.sleep(2)
    raise TimeoutError(f"render timed out after {timeout}s")


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
    size = config.IMG_PORTRAIT if kind == "portrait" else config.IMG_SCENE

    data = _render(job["prompt"], size, job["seed"], f"loom/{kind}",
                   job.get("checkpoint", ""), job.get("negative", ""))

    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slug(subject)
    name = f"{sid}_{kind}_{slug}_{int(time.time())}.png"
    path = config.MEDIA_DIR / name
    path.write_bytes(data)

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
    """Fast probe for the UI. Must never block a page load — a firewalled or
    absent ComfyUI should answer 'no' in a couple of seconds, not thirty."""
    if not config.IMAGES_ENABLED:
        return False
    try:
        _get("/system_stats", timeout=3)
        return True
    except Exception:
        return False
