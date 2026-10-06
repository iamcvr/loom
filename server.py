"""HTTP server and the turn loop.

One stdlib ThreadingHTTPServer. The only interesting route is POST /api/send,
which is the whole product in about forty lines:

    store user turn
      -> gather (keywords, retrieval, hot temp)
      -> assemble within budget
      -> stream prose to the browser
      -> store reply
      -> ONE utility call to digest the exchange
      -> queue images off-loop
      -> push updated panels to the browser

Everything before "stream prose" is arithmetic. Everything after it happens with
the reply already on screen.
"""
from __future__ import annotations

import json
import mimetypes
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

import advisor
import assemble
import brain
import chapters as chapters_mod
import config
import images
import ledger as ledger_mod
import memory
import rulebuilder
import settings
import store
import story as story_mod

STATIC = Path(__file__).resolve().parent / "static"


# ---------------------------------------------------------------- state view


def story_for(session_id: int, s: Optional[dict] = None) -> tuple[dict, Optional[dict]]:
    """The story as this session sees it, plus who the player is.

    One choke point for merging the session's protagonist into the cast. A
    character-creatable story leaves the player out of its `cast:` list — their
    name is chosen at session start — so everything downstream that reasons about
    who exists (the state extractor, the portrait queue, the cast panel) has to
    be handed the merged version rather than the file's version.
    """
    s = s or store.get_session(session_id)
    if not s:
        raise story_mod.StoryError("?", ["no such session"])
    st = story_mod.load(s["story_id"])
    pro = assemble.protagonist(session_id, st)
    if pro:
        st = {**st, "cast": story_mod.cast_with(st, pro)}
    return st, pro


def session_state(session_id: int) -> dict[str, Any]:
    s = store.get_session(session_id)
    if not s:
        return {}
    turn = int(s["turn"])
    st, pro = story_for(session_id, s)
    tok = story_mod.tokens(pro)
    defs = {d["key"]: d for d in st.get("stats", [])}
    vals = store.stats(session_id)
    try:
        intro = story_mod.intro(st, s["intro_id"])
    except story_mod.StoryError:
        intro = {"suggestions": [], "play_guide": ""}
    return {
        "session": s,
        "story": {"id": st["id"], "name": st["name"], "tagline": st["tagline"],
                  "mode": st.get("mode", "play")},
        "arc": assemble.current_act(session_id, st),
        "acts": [{"id": a["id"], "name": a["name"]}
                 for a in (st.get("arc") or {}).get("acts", [])],
        # Name, descriptor and aliases only — the image prompt is tag soup and
        # would just be noise in the UI.
        "cast": [
            {"name": c["name"], "short": c.get("short", ""), "aliases": c.get("aliases", [])}
            for c in st.get("cast", [])
        ],
        "portrait_prompts": {
            m["subject"]: images.portrait_prompt(session_id, m["subject"])
            for m in store.media(session_id) if m["kind"] == "portrait"
        },
        "suggestions": [story_mod.subst(x, tok) for x in intro.get("suggestions", [])],
        "play_guide": story_mod.subst(intro.get("play_guide", ""), tok),
        "protagonist": pro,
        "messages": store.messages(session_id),
        "stats": [
            {**defs[k], "value": v} for k, v in vals.items() if k in defs
        ],
        "relationships": store.relationships(session_id),
        "goals": store.goals(session_id, ["active", "pending"]),
        # Same clock the nudge uses — a panel that disagrees with the prompt about
        # how long something has been quiet is worse than no panel.
        "goal_quiet": (memory.goal_quiet(g, turn)
                       if (g := store.active_goal(session_id)) else None),
        "goals_done": store.goals(session_id, ["complete"]),
        # Memory is deliberately not sent. It is machinery, not an interface: a
        # 132-turn session held 537 rows and there was nothing useful to do with
        # the list. What the author curates is the lorebook.
        "memory_counts": {
            "long": len(store.memories(session_id, "long")),
            "temp": len(store.memories(session_id, "temp")),
        },
        "lorebook": store.lore(session_id, ["active"]),
        "lore_suggestions": store.lore(session_id, ["suggested"]),
        "notes": store.notes(session_id),
        "media": store.media(session_id),
        "images": images.status(),
        "chapter": chapters_mod.status(session_id),
        "chapters": store.chapters(session_id, closed_only=True),
        "synopsis": store.synopsis(session_id).get("text", ""),
    }


# ---------------------------------------------------------------- turn


def run_turn(session_id: int, user_text: str, emit: Callable[[str, Any], None],
             *, store_user: bool = True, resume_msg_id: Optional[int] = None) -> None:
    """One turn. With `resume_msg_id`, finish that reply instead of starting one.

    A resume is deliberately the same turn, not the next one: it bumps no
    counter, stores no user message, and appends its output to the message it is
    finishing. Treating it as a new turn would advance the pacing and chapter
    clocks by one for a beat that had not even ended.
    """
    s = store.get_session(session_id)
    if not s:
        emit("error", {"message": "no such session"})
        return

    st, _pro = story_for(session_id, s)
    intro = story_mod.intro(st, s["intro_id"])

    if resume_msg_id is not None:
        turn = store.current_turn(session_id)
    else:
        turn = store.bump_turn(session_id)
    # A retry re-runs the turn against a user message that is already in the
    # transcript. The text is still needed for the memory digest, but storing it
    # again appended a duplicate of your own turn on every single retry.
    if store_user and user_text.strip():
        store.add_message(session_id, "user", user_text.strip(), turn)

    # --- gather (all local arithmetic)
    raw_mode = st.get("mode") == "raw"
    if raw_mode:
        notes, hot, retrieved = [], [], []
    else:
        notes = memory.match_keywords(st, session_id)
        hot = memory.hot_temp(session_id, turn, limit=25)
        retrieved = memory.retrieve_long(session_id, memory.retrieval_query(session_id))

    packet = assemble.build(
        session_id, st, intro,
        retrieved_long=retrieved, hot_temp=hot, keyword_notes=notes,
        closing_user=config.RESUME_TURN_TEXT if resume_msg_id is not None else None,
    )
    emit("context", {
        "layers": packet["layers"], "used": packet["used"], "budget": packet["budget"],
        "fill": packet["fill"], "advice": packet["advice"],
        "transcript_kept": packet["transcript_kept"],
        "transcript_total": packet["transcript_total"],
    })

    # A resume is only meaningful if the model can see what it is resuming.
    #
    # This is defence in depth behind assemble._fit's min_items, and it exists
    # because the failure it catches is silent and destructive rather than loud:
    # the budget squeezed the transcript to nothing, the packet went out as the
    # resume instruction alone, and the model — told to continue a reply that was
    # not in front of it — wrote a different scene, which was then appended to the
    # broken sentence and stored as one turn. Refusing costs a button press.
    if resume_msg_id is not None:
        target = next((m for m in store.messages(session_id) if m["id"] == resume_msg_id), None)
        anchor = (target or {}).get("content", "")[-120:]
        if not anchor or not any(m["role"] == "assistant" and m["content"].endswith(anchor)
                                 for m in packet["messages"]):
            emit("error", {"message": (
                "Cannot continue: the reply being resumed did not fit in the context "
                "budget, so the model would not be able to see it. Raise Settings \u2192 "
                "Context budget \u2192 Total budget, or compact the chapter first.")})
            return

    # --- prose
    stopped: list[str] = []

    # Measured, not projected. Two clocks, because they answer different
    # questions: time to the FIRST token tracks what the prompt costs, and the
    # span from first to last tracks what the reply costs. One is the budget
    # knob, the other is max_tokens.
    t_start = time.perf_counter()
    first_at: list[float] = []

    def _on_token(t: str) -> None:
        if not first_at:
            first_at.append(time.perf_counter())
        emit("token", t)

    try:
        reply = brain.prose(
            packet["system"], packet["messages"],
            on_token=_on_token,
            # The provider's own word for why it stopped. "max_tokens" means the
            # reply is unfinished, which the stream itself cannot express.
            on_stop=stopped.append,
            # Reasoning models can deliberate for over a minute before the first
            # visible character. These keep the browser informed and, just as
            # importantly, keep bytes moving over the SSE socket.
            on_thinking=lambda n: emit("thinking", {"chars": n}),
            on_retry=lambda n, d, why: emit("retrying", {
                "attempt": n, "of": config.PROSE_RETRIES, "delay": d, "reason": why}),
        )
    except brain.BrainError as e:
        # Log it too. These were previously emitted to the browser and nowhere
        # else, so a failed turn left no server-side record to diagnose from.
        print(f"[turn] prose failed (session {session_id}, "
              f"{config.PROSE['provider']}/{config.PROSE['model']}): {e}")
        emit("error", {"message": str(e)})
        return

    cut = "max_tokens" in stopped

    if resume_msg_id is not None:
        # Only the leading whitespace goes. The tail is stripped as usual, but a
        # resume starts mid-sentence by design, so rstrip-only here would leave
        # the model's own leading space doubled against text that already ended
        # in one — and lstrip would glue a new paragraph onto the old line.
        addition = reply.rstrip()
        full = store.append_message(resume_msg_id, addition, truncated=cut)
        mid, reply = resume_msg_id, full
        # The digest sees only what is new. The truncated half was already
        # digested when it was written, and re-extracting it would file every
        # fact in it a second time.
        digest_text = addition
        emit("message", {"id": mid, "role": "assistant", "content": full,
                         "turn": turn, "truncated": cut, "replaced": True})
    else:
        reply = reply.strip()
        mid = store.add_message(session_id, "assistant", reply, turn)
        if cut:
            store.set_truncated(mid, True)
        digest_text = reply
        emit("message", {"id": mid, "role": "assistant", "content": reply,
                         "turn": turn, "truncated": cut})

    # Latency, attached to the reply it describes. Both store paths above set
    # `mid`, so a resumed turn is measured too — its ttft is the resume's, which
    # is the honest number for the request that was actually made.
    _end = time.perf_counter()
    _first = first_at[0] if first_at else _end
    store.record_timing(mid, int((_first - t_start) * 1000),
                        int((_end - _first) * 1000))
    stats = store.timing_stats(session_id)
    if stats:
        emit("timing", stats)

    if cut:
        # Say so. A beat that stops mid-sentence with no explanation reads as the
        # model losing the thread, and the reader's instinct is to retry the
        # whole turn — throwing away a reply that was fine as far as it got.
        emit("warn", {"message": (
            f"The reply hit the {config.PROSE['max_tokens']}-token limit and stopped "
            f"mid-scene. Press continue on it to finish the beat, or raise "
            f"Settings \u2192 Models \u2192 Prose max tokens.")})

    # --- one utility call, after the reply is already on screen
    if raw_mode:
        applied = {}            # no digest, no memories, no relationships, no stats
    else:
        try:
            applied = memory.digest(st, session_id, turn, user_text, digest_text)
        except Exception as e:
            emit("warn", {"message": f"state update failed: {e}"})
            applied = {}

    # --- images, off-loop
    if applied:
        for name in applied.get("new_characters", []):
            images.request_portrait(session_id, st, name, turn)
        if applied.get("scene_changed"):
            images.request_scene(session_id, st, applied.get("scene_prompt", ""), turn)

    # --- compaction, last, after everything the player is waiting for is done
    #
    # This costs one or two utility calls and takes a few seconds. Running it here
    # means the reply is already on screen and the delay is invisible; running it
    # before the prose would put it directly in the player's way, every twentieth
    # turn, for no benefit they can perceive.
    if not raw_mode and chapters_mod.due(session_id, packet.get("fill", 0.0)):
        done = chapters_mod.compact(session_id, st)
        if done:
            emit("chapter", done)

    # --- the Ledger, last of all
    #
    # Fires on BUDGET, not on a turn count, and only after the reply is on
    # screen. The proposal call reuses the prose model deliberately: a second
    # model would evict the first from a 12GB card and cost an 11.8s reload on
    # every turn after. Once per review, that reload is free -- you are reading
    # a fact list while it happens.
    try:
        if ledger_mod.due(session_id, packet.get("fill", 0.0)):
            made = ledger_mod.propose(session_id, packet["messages"], turn)
            if made:
                emit("ledger", {"proposed": made, "fill": packet.get("fill", 0.0),
                                "counts": ledger_mod.counts(session_id)})
    except Exception as e:
        emit("warn", {"message": f"ledger proposal failed: {e}"})

    emit("state", {"applied": applied, **session_state(session_id)})
    emit("done", {})


# ---------------------------------------------------------------- routing


class Handler(BaseHTTPRequestHandler):
    server_version = "loom"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default log
        if config.__dict__.get("HTTP_LOG", False):
            super().log_message(fmt, *args)

    # -- helpers

    def _send(self, code: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except json.JSONDecodeError:
            return {}

    def _static(self, rel: str) -> None:
        p = (STATIC / unquote(rel)).resolve()
        if not str(p).startswith(str(STATIC.resolve())) or not p.is_file():
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        # No cache-busting in the URLs, so a rebuilt image would otherwise serve
        # new HTML against a browser-cached app.js. These files are a few KB on a
        # LAN; revalidating every load costs nothing and removes a whole class of
        # "the fix didn't deploy" confusion.
        self._send(200, p.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    def _media(self, name: str) -> None:
        # unquote first. urlparse() does not percent-decode, so a filename with a
        # non-ASCII character in it — "Kenji Ōbayashi" renders to
        # 9_portrait_Kenji_Ōbayashi_….png — arrived here as the literal text
        # "…Kenji_%C5%8Cbayashi…" and missed a file that was sitting right there.
        # The image generated fine; it was simply unreachable.
        #
        # Decoding is safe against traversal because the containment check below
        # happens after resolve(), so a decoded "../" still cannot escape.
        p = (config.MEDIA_DIR / unquote(name)).resolve()
        if not str(p).startswith(str(config.MEDIA_DIR.resolve())) or not p.is_file():
            self._json({"error": "not found"}, 404)
            return
        self._send(200, p.read_bytes(), "image/png", {"Cache-Control": "public, max-age=31536000"})

    # -- GET

    def do_GET(self) -> None:
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path == "/":
                self._static("index.html")
            elif path == "/ledger":
                self._static("ledger.html")
            elif path == "/api/ledger":
                sid = int((qs.get("session") or ["0"])[0])
                self._json({
                    "active": ledger_mod.rows(sid, ("active",)),
                    "proposed": ledger_mod.rows(sid, ("proposed",)),
                    "dismissed": ledger_mod.rows(sid, ("dismissed",)),
                    "counts": ledger_mod.counts(sid),
                    "kinds": list(ledger_mod.KINDS),
                    "max_active": config.LEDGER_MAX_ACTIVE,
                })
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path.startswith("/media/"):
                self._media(path[len("/media/"):])
            elif path == "/api/health":
                self._json({**brain.health(), "images": images.status(),
                            "comfy": images.reachable()})
            elif path == "/api/stories":
                self._json(story_mod.available())
            elif path == "/api/rulebuilder":
                # Static: the questionnaire never varies per story or session.
                self._json({"axes": rulebuilder.schema()})
            elif path == "/api/settings":
                self._json(settings.current())
            elif path.startswith("/api/chapters/"):
                sid = int(path.rsplit("/", 1)[1])
                self._json({
                    "status": chapters_mod.status(sid),
                    "chapters": store.chapters(sid, closed_only=True),
                    "synopsis": store.synopsis(sid).get("text", ""),
                })
            elif path == "/api/probe":
                prov = qs.get("provider", [config.PROSE["provider"]])[0]
                only = qs.get("model", [None])[0]
                rows = ([brain.probe(prov, only)] if only
                        else brain.probe_all(prov))
                self._json({
                    "provider": prov,
                    "results": rows,
                    "in_use": {"prose": config.PROSE["model"] if config.PROSE["provider"] == prov else None,
                               "utility": config.UTILITY["model"] if config.UTILITY["provider"] == prov else None},
                })
            elif path == "/api/models":
                self._json(brain.models(qs.get("provider", ["ollama"])[0],
                                        force=qs.get("force") == ["1"]))
            elif path == "/api/story/new":
                self._json(story_mod.blank())
            elif path.startswith("/api/story/"):
                self._json(story_mod.editable(path[len("/api/story/"):]))
            elif path == "/api/sessions":
                self._json(store.list_sessions())
            elif path.startswith("/api/session/"):
                self._json(session_state(int(path.rsplit("/", 1)[1])))
            elif path.startswith("/api/context/"):
                sid = int(path.rsplit("/", 1)[1])
                s = store.get_session(sid)
                st, _ = story_for(sid, s)
                intro = story_mod.intro(st, s["intro_id"])
                turn = int(s["turn"])
                self._json(assemble.context_report(
                    sid, st, intro,
                    retrieved_long=memory.retrieve_long(sid, memory.retrieval_query(sid)),
                    hot_temp=memory.hot_temp(sid, turn, limit=25),
                    keyword_notes=memory.match_keywords(st, sid),
                ))
            elif path == "/api/prompt":
                # full assembled packet, for inspection
                sid = int(qs.get("session", ["0"])[0])
                s = store.get_session(sid)
                st, _ = story_for(sid, s)
                intro = story_mod.intro(st, s["intro_id"])
                turn = int(s["turn"])
                p = assemble.build(
                    sid, st, intro,
                    retrieved_long=memory.retrieve_long(sid, memory.retrieval_query(sid)),
                    hot_temp=memory.hot_temp(sid, turn, limit=25),
                    keyword_notes=memory.match_keywords(st, sid),
                )
                self._json({"system": p["system"], "messages": p["messages"],
                            "layers": p["layers"]})
            else:
                self._json({"error": "not found"}, 404)
        except story_mod.StoryError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    # -- POST

    def do_POST(self) -> None:
        u = urlparse(self.path)
        path = u.path
        body = self._body()
        try:
            if path == "/api/session":
                st = story_mod.load(body["story"])
                intro_id = body.get("intro") or st["intros"][0]["id"]
                intro = story_mod.intro(st, intro_id)
                sid = store.create_session(st["id"], intro_id, body.get("title", st["name"]))
                for d in st.get("stats", []):
                    store.set_stat(sid, d["key"], d["default"])

                # Who the player chose to be, before the prologue is written —
                # the prologue names them, and it is stored as a real message, so
                # it has to be substituted once here rather than on every read.
                # The protagonist belongs to the SESSION, not the story. A story
                # describes a world and the people already in it; who the player is
                # this time is answered when they sit down, and answered again
                # differently on the next playthrough. So a story's protagonist
                # block, when it has one, is a suggested starting point and nothing
                # more — and a story without one still gets whatever the player
                # typed, which it previously discarded.
                defaults = story_mod.protagonist_defaults(st) or {}
                if st.get("power_label") and not defaults.get("power_label"):
                    defaults["power_label"] = st["power_label"]
                submitted = body.get("protagonist") or {}
                pro = None
                if defaults or submitted:
                    pro = store.set_protagonist(sid, {**defaults, **submitted})
                    if pro.get("name"):
                        images.set_portrait_prompt(sid, pro["name"], pro.get("prompt", ""))

                store.add_message(
                    sid, "assistant",
                    story_mod.subst(intro["prologue"].strip(), story_mod.tokens(pro)), 0)
                self._json({"session_id": sid, **session_state(sid)})

            elif path == "/api/protagonist":
                sid = int(body["session"])
                st = story_mod.load(store.get_session(sid)["story_id"])
                if body.get("reset"):
                    values = story_mod.protagonist_defaults(st) or {}
                else:
                    values = body.get("values") or {}
                was = store.protagonist(sid)
                pro = store.set_protagonist(sid, values)
                # A rename leaves the old portrait filed under the old name, so it
                # would vanish from the cast panel and re-render from scratch.
                if was and was["name"] and was["name"] != pro["name"]:
                    store.rename_media_subject(sid, was["name"], pro["name"])
                images.set_portrait_prompt(sid, pro["name"], pro.get("prompt", ""))
                self._json({"ok": True, **session_state(sid)})

            elif path == "/api/send":
                self._stream_turn(int(body["session"]), body.get("text", ""))

            elif path == "/api/retry":
                sid, mid = int(body["session"]), int(body["message"])
                msgs = store.messages(sid)
                idx = next((i for i, m in enumerate(msgs) if m["id"] == mid), None)
                prior_user = ""
                if idx is not None:
                    for m in reversed(msgs[:idx]):
                        if m["role"] == "user":
                            prior_user = m["content"]
                            break
                    # A full rewind, not just the message: the turn being retried
                    # already ran its digest, so its memories, goals and
                    # relationship updates exist and would otherwise survive into
                    # the replacement turn as a duplicate of what never happened.
                    store.rewind_to(sid, mid)
                self._stream_turn(sid, prior_user, store_user=False)

            elif path == "/api/continue":
                # Finish a reply the provider cut off. Defaults to the last
                # assistant message, which is the only one you can meaningfully
                # resume — anything earlier has the rest of the story after it.
                sid = int(body["session"])
                mid = body.get("message")
                if mid is None:
                    last = store.last_message(sid, "assistant")
                    mid = last["id"] if last else None
                if mid is None:
                    self._json({"error": "nothing to continue"}, 400)
                else:
                    # The digest wants the prompt that produced the scene, not
                    # the resume instruction — that is machinery, and feeding it
                    # in would have the extractor writing memories about it.
                    msgs = store.messages(sid)
                    idx = next((i for i, m in enumerate(msgs) if m["id"] == int(mid)), None)
                    prior_user = ""
                    if idx is not None:
                        for m in reversed(msgs[:idx]):
                            if m["role"] == "user":
                                prior_user = m["content"]
                                break
                    self._stream_turn(sid, prior_user, store_user=False,
                                      resume_msg_id=int(mid))

            elif path == "/api/message":
                store.update_message(int(body["id"]), body.get("content", ""))
                self._json({"ok": True})

            elif path == "/api/message/delete":
                # `after` deletes this message and everything following it —
                # useful for rewinding a session to a branch point. Without it,
                # only the one message goes.
                sid = int(body["session"])
                removed = {}
                if body.get("after"):
                    removed = store.rewind_to(sid, int(body["id"]))
                else:
                    store.delete_message(int(body["id"]))
                self._json({"ok": True, "removed": removed, **session_state(sid)})

            elif path == "/api/arc":
                # The counter only knows that time has passed; the reader knows
                # whether a beat actually landed. This wins over the clock until
                # it is cleared.
                sid = int(body["session"])
                if body.get("clear"):
                    store.clear_meta(f"arc:{sid}")
                else:
                    store.set_meta(f"arc:{sid}", body["act"])
                self._json({"ok": True, **session_state(sid)})

            elif path == "/api/notes":
                store.set_notes(int(body["session"]), body.get("text", ""))
                self._json({"ok": True})

            elif path == "/api/goal":
                store.set_goal_status(int(body["id"]), body["status"],
                                      store.current_turn(int(body["session"])))
                self._json({"ok": True, **session_state(int(body["session"]))})

            elif path == "/api/memory/forget":
                store.drop_memories([int(body["id"])])
                self._json({"ok": True, **session_state(int(body["session"]))})

            elif path == "/api/portrait/describe":
                images.set_portrait_prompt(int(body["session"]), body["name"],
                                           body.get("text", ""))
                self._json({"ok": True})

            elif path == "/api/portrait":
                sid = int(body["session"])
                s = store.get_session(sid)
                # story_for, not load: the player character is not in the story
                # file's cast list, so plain load() would send them down the
                # "story does not define this person" path and render them from a
                # generic prompt instead of their own appearance tags.
                st, _ = story_for(sid, s)
                ok = images.request_portrait(sid, st, body["name"], int(s["turn"]),
                                             manual=True, replace=bool(body.get("replace")))
                self._json({"queued": ok, "images": images.status()})

            elif path == "/api/rulebuilder":
                # Generated server-side rather than in the browser, although the
                # browser already has every fragment. build() enforces the one
                # constraint that is not a matter of taste, and a second copy of
                # that rule in JavaScript is a second copy to drift.
                self._json({"text": rulebuilder.build(body.get("choices") or {})})
            elif path == "/api/advise":
                # Proposes only. Applying is POST /api/settings, which the reader
                # reaches by pressing a button on a diff they have read.
                self._json(advisor.propose(
                    str(body.get("request") or ""),
                    int(body.get("session") or 0) or None))
            elif path == "/api/settings":
                out = settings.update(body.get("changes") or {})
                # Model names are advisory-checked, not gated — see check_models.
                if any(k.startswith(("PROSE.", "UTILITY.")) for k in (body.get("changes") or {})):
                    out["warnings"] = settings.check_models()
                self._json(out)

            elif path == "/api/ledger":
                # do_POST already drained the request body at the top; reading it
                # a second time blocks forever on bytes that never arrive.
                b = body
                act, fid = b.get("action"), b.get("id")
                if act == "add":
                    ledger_mod.add(int(b["session"]), b.get("text", ""),
                                   b.get("kind", "state"), status="active",
                                   source="you")
                elif act in ("accept", "dismiss", "restore"):
                    ledger_mod.update(int(fid), status={"accept": "active",
                                                        "dismiss": "dismissed",
                                                        "restore": "active"}[act])
                elif act == "update":
                    ledger_mod.update(int(fid), text=b.get("text"),
                                      kind=b.get("kind"))
                elif act == "delete":
                    ledger_mod.delete(int(fid))
                elif act == "accept_all":
                    for r in ledger_mod.rows(int(b["session"]), ("proposed",)):
                        ledger_mod.update(r["id"], status="active")
                elif act == "dismiss_all":
                    for r in ledger_mod.rows(int(b["session"]), ("proposed",)):
                        ledger_mod.update(r["id"], status="dismissed")
                else:
                    self._json({"error": f"unknown action {act}"}, 400); return
                sid = int(b.get("session") or 0)
                self._json({"ok": True, "counts": ledger_mod.counts(sid)})

            elif path == "/api/settings/reset":
                self._json(settings.reset(body.get("keys")))

            elif path == "/api/story/validate":
                # Dry run: report every problem without writing anything, so the
                # editor can show them while the author is still typing.
                sid = (body.get("id") or "").strip()
                problems: list[str] = []
                try:
                    story_mod.check_id(sid)
                except story_mod.StoryError as e:
                    problems += e.problems
                parsed, found = story_mod.parse(body.get("story") or {}, sid or "story")
                problems += found
                # A story can be perfectly valid and still be too big for the
                # budget, which fails silently. Report it either way.
                fit = assemble.story_fit(parsed) if not found else []
                self._json({"ok": not problems, "problems": problems, "fit": fit})

            elif path == "/api/story/save":
                st = story_mod.save(body["id"], body.get("story") or {},
                                    create=bool(body.get("create")))
                self._json({"ok": True, "id": st["id"], "name": st["name"]})

            elif path == "/api/story/delete":
                target = body.get("id") or ""
                using = story_mod.sessions_using(target)
                # Two-step on purpose: the first call reports what would break,
                # the second one does it. The UI shows the count in between.
                if using and not body.get("confirm"):
                    self._json({"needs_confirm": True, "sessions": len(using),
                                "titles": [s["title"] for s in using[:5]]})
                else:
                    self._json(story_mod.delete(target))

            elif path == "/api/story/duplicate":
                st = story_mod.duplicate(body["from"], body["id"], body.get("name", ""))
                self._json({"ok": True, "id": st["id"], "name": st["name"]})

            elif path == "/api/story/slug":
                self._json({"id": story_mod.slugify(body.get("name", ""))})

            elif path == "/api/chapter/draft":
                sid = int(body["session"])
                st, _ = story_for(sid)
                d = chapters_mod.draft(sid, st)
                if chapters_mod.needs_refold(sid):
                    d["synopsis"] = chapters_mod.refold_synopsis(sid, st)
                    d["synopsis_current"] = store.synopsis(sid).get("text", "")
                self._json(d)

            elif path == "/api/chapter/close":
                sid = int(body["session"])
                chapters_mod.close(
                    sid,
                    title=body.get("title", ""),
                    summary=body.get("summary", ""),
                    synopsis_text=body.get("synopsis"),
                )
                self._json({"ok": True, **session_state(sid)})

            elif path == "/api/chapter/update":
                store.update_chapter(int(body["id"]), body.get("title", ""),
                                     body.get("summary", ""))
                self._json({"ok": True, **session_state(int(body["session"]))})

            elif path == "/api/lore":
                sid = int(body["session"])
                if body.get("delete"):
                    store.delete_lore(int(body["delete"]))
                elif body.get("id"):
                    store.update_lore(
                        int(body["id"]),
                        title=body.get("title"),
                        body=body.get("body"),
                        keywords=body.get("keywords"),
                        always=body.get("always"),
                        status=body.get("status"),
                    )
                else:
                    store.add_lore(
                        sid,
                        body.get("title", ""),
                        body.get("body", ""),
                        body.get("keywords") or [],
                        always=bool(body.get("always")),
                        chapter=chapters_mod.status(sid).get("number", 0),
                    )
                self._json({"ok": True, **session_state(sid)})

            elif path == "/api/synopsis":
                sid = int(body["session"])
                store.set_synopsis(sid, body.get("text", ""),
                                   store.synopsis(sid).get("upto_chapter", 0))
                self._json({"ok": True, **session_state(sid)})

            elif path == "/api/session/delete":
                store.delete_session(int(body["session"]))
                self._json({"ok": True})

            else:
                self._json({"error": "not found"}, 404)
        except story_mod.StoryError as e:
            self._json({"error": str(e)}, 400)
        except (ValueError, KeyError) as e:
            # Bad input, not a server fault — settings validation lands here.
            self._json({"error": str(e)}, 400)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    # -- SSE

    def _stream_turn(self, session_id: int, text: str, *, store_user: bool = True,
                     resume_msg_id: Optional[int] = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        # A dropped browser must not cost you the turn.
        #
        # This used to let the write error propagate, which unwound out of
        # brain.prose() and abandoned the reply — the model had already generated
        # it, we were already paying for it, and it was thrown away. The user
        # message stayed in the transcript with nothing answering it. Locking a
        # phone, switching networks or backgrounding the tab was enough to do it.
        #
        # Now a dead socket only stops the pushing. Generation runs to completion,
        # the reply and the memory delta are stored, and the turn is simply there
        # on the next page load.
        alive = True

        def emit(event: str, data: Any) -> None:
            nonlocal alive
            if not alive:
                return
            payload = json.dumps(data, default=str)
            try:
                self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                alive = False
                print(f"[turn] client disconnected mid-turn (session {session_id}); "
                      f"finishing server-side")

        try:
            run_turn(session_id, text, emit, store_user=store_user,
                     resume_msg_id=resume_msg_id)
        except Exception as e:
            # Provider failures used to reach the browser and leave no trace in
            # the logs, which made them undiagnosable after the fact.
            traceback.print_exc()
            emit("error", {"message": str(e)})
        self.close_connection = True


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Swallow client-disconnect tracebacks, print everything else.

        A browser closing a keep-alive socket raises inside the stdlib handler
        and prints a full traceback. With SSE and a phone that happens constantly,
        and the noise was burying the failures worth reading.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def serve() -> None:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for problem in settings.apply_saved():
        print(f"  settings: {problem}")
    # Child rows whose session is gone. SQLite reuses a freed session id, so an
    # orphan is not inert — it gets adopted by the next story created.
    orphans = store.purge_orphans()
    if orphans:
        print("  purged orphaned rows: "
              + ", ".join(f"{n} {t}" for t, n in sorted(orphans.items())))
    images.start()
    srv = Server((config.HOST, config.PORT), Handler)
    print(f"loom on http://{config.HOST}:{config.PORT}")
    print(f"  prose   {config.PROSE['provider']}/{config.PROSE['model']}")
    print(f"  utility {config.UTILITY['provider']}/{config.UTILITY['model']}")
    print(f"  stories {config.STORIES_DIR}")
    srv.serve_forever()
