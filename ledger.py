"""The Ledger — durable facts you have approved.

Deliberately a separate system from the other two memories, because they answer
different questions and mixing them is how the old pipeline ended up with 1,003
long-term rows of which 416 were referenced exactly once:

    memories (memory.py)   automatic, heat-decayed, similarity-retrieved, never
                           reviewed by anyone. Good at "what happened recently".
    lorebook (lorebook.py) keyword-fired, author-written, silent until a trigger
                           word appears. Good at "what this place is".
    ledger   (this file)   proposed by the model when the context window starts
                           filling, ACCEPTED BY YOU, and then in every prompt
                           until you remove it. Good at "what is still true".

Nothing here reaches the model until a human sets its status to 'active'. That
is the whole point, and it is why proposals are cheap to be wrong about.

Rendered at DEPTH, not in the system block. Two reasons, both measured:
loom already established that text near the generation point outranks system
text, and llama.cpp only reprocesses a prompt from the first changed byte — so
a list that is edited every few turns belongs after the transcript, where a
rewrite costs ~0.6s instead of a full reprocess.
"""
import json
from typing import Any, Optional

import brain
import config
import store

KINDS = ("place", "person", "object", "commitment", "state")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger(
  id         INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL,
  text       TEXT    NOT NULL,
  kind       TEXT    NOT NULL DEFAULT 'state',
  status     TEXT    NOT NULL DEFAULT 'proposed',
  source     TEXT    NOT NULL DEFAULT 'model',
  turn       INTEGER NOT NULL DEFAULT 0,
  ts         TEXT    DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ledger_session ON ledger(session_id, status);
CREATE TABLE IF NOT EXISTS ledger_state(
  session_id INTEGER PRIMARY KEY,
  floor_id   INTEGER NOT NULL DEFAULT 0,
  last_turn  INTEGER NOT NULL DEFAULT 0
);
"""


def _c():
    c = store._conn()
    c.executescript(_SCHEMA)
    return c


# ---------------------------------------------------------------- reads

def rows(session_id: int, statuses: tuple = ("active",)) -> list[dict]:
    q = ",".join("?" * len(statuses))
    with _c() as c:
        return [dict(r) for r in c.execute(
            f"SELECT * FROM ledger WHERE session_id=? AND status IN ({q}) "
            "ORDER BY kind, id", (session_id, *statuses))]


def counts(session_id: int) -> dict:
    with _c() as c:
        got = dict(c.execute(
            "SELECT status, COUNT(*) FROM ledger WHERE session_id=? GROUP BY status",
            (session_id,)).fetchall())
    return {k: got.get(k, 0) for k in ("proposed", "active", "dismissed")}


def floor(session_id: int) -> int:
    """Messages at or below this id are carried by the ledger, not by transcript."""
    with _c() as c:
        r = c.execute("SELECT floor_id FROM ledger_state WHERE session_id=?",
                      (session_id,)).fetchone()
    return int(r["floor_id"]) if r else 0


def render(session_id: int) -> str:
    """The block that goes into the prompt. Empty string when there is nothing."""
    got = rows(session_id)
    if not got:
        return ""
    by: dict[str, list[str]] = {}
    for r in got:
        by.setdefault(r["kind"], []).append(r["text"].strip())
    parts = []
    for k in KINDS:
        if by.get(k):
            parts.append(f"{k.upper()}\n" + "\n".join(f"- {t}" for t in by[k]))
    return "\n".join(parts)


# ---------------------------------------------------------------- writes

def add(session_id: int, text: str, kind: str = "state", *,
        status: str = "active", source: str = "you", turn: int = 0) -> int:
    kind = kind if kind in KINDS else "state"
    with _c() as c:
        cur = c.execute(
            "INSERT INTO ledger(session_id,text,kind,status,source,turn) "
            "VALUES(?,?,?,?,?,?)",
            (session_id, text.strip(), kind, status, source, turn))
        c.commit()
        return cur.lastrowid


def update(fact_id: int, *, text: Optional[str] = None,
           kind: Optional[str] = None, status: Optional[str] = None) -> None:
    sets, vals = [], []
    if text is not None:
        sets.append("text=?"); vals.append(text.strip())
    if kind is not None:
        sets.append("kind=?"); vals.append(kind if kind in KINDS else "state")
    if status is not None:
        sets.append("status=?"); vals.append(status)
    if not sets:
        return
    with _c() as c:
        c.execute(f"UPDATE ledger SET {','.join(sets)} WHERE id=?", (*vals, fact_id))
        c.commit()


def delete(fact_id: int) -> None:
    with _c() as c:
        c.execute("DELETE FROM ledger WHERE id=?", (fact_id,))
        c.commit()


def set_floor(session_id: int, msg_id: int, turn: int = 0) -> None:
    with _c() as c:
        c.execute("INSERT INTO ledger_state(session_id,floor_id,last_turn) "
                  "VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET "
                  "floor_id=excluded.floor_id, last_turn=excluded.last_turn",
                  (session_id, msg_id, turn))
        c.commit()


# ---------------------------------------------------------------- proposing

DELTA_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["facts"],
    "properties": {"facts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["text", "kind"],
        "properties": {"text": {"type": "string"},
                       "kind": {"type": "string", "enum": list(KINDS)}}}}}}

_PROMPT = """Extract durable facts from these scenes — things that constrain what can
happen LATER. A fact must still matter ten turns from now.

RECORD: where things are and what they connect to; who someone is and how they
behave; what someone now owns, owes or has lost; what someone has committed to;
lasting changes of state.

DO NOT RECORD: what happened in this scene, how someone momentarily felt, weather,
or scenery with no consequence. "The door is green" is not a fact. "The door leads
to the armoury" is.

These are already known — do not repeat them:
{known}

Return at most {cap} new facts. SCENES:
---
{text}
---"""


def due(session_id: int, fill: float) -> bool:
    """Fire before the window is full, not after.

    Reactive is too late: past the model's context ollama silently drops the
    FRONT of the prompt, so the first thing lost is the oldest history and the
    story simply appears to forget. The threshold leaves room for one more turn.
    """
    if not config.LEDGER_ENABLED:
        return False
    if counts(session_id)["proposed"]:
        return False              # something is already waiting on you
    return fill >= config.LEDGER_TRIGGER_FILL


def propose(session_id: int, messages: list[dict], turn: int) -> list[dict]:
    """One model call. Inserts proposals; returns them. Never auto-activates."""
    if not messages:
        return []
    known = "\n".join(f"- {r['text']}" for r in rows(session_id)) or "(nothing yet)"
    text = "\n\n".join(f"{m['role']}: {m['content']}" for m in messages)
    prompt = _PROMPT.format(known=known[:4000], cap=config.LEDGER_PROPOSE_MAX,
                            text=text[:config.LEDGER_SOURCE_CHARS])
    spec = dict(config.LEDGER_SPEC or config.PROSE)
    out = brain.utility(prompt, DELTA_SCHEMA, spec=spec)
    made = []
    seen = {r["text"].lower() for r in rows(session_id, ("active", "proposed", "dismissed"))}
    for f in (out.get("facts") or [])[:config.LEDGER_PROPOSE_MAX]:
        t = (f.get("text") or "").strip()
        if not t or t.lower() in seen:
            continue
        seen.add(t.lower())
        fid = add(session_id, t, f.get("kind", "state"),
                  status="proposed", source="model", turn=turn)
        made.append({"id": fid, "text": t, "kind": f.get("kind", "state")})
    return made
