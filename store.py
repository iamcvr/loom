"""SQLite persistence.

One database, one file, no migrations framework — tables are created on demand
and new columns are added defensively so an older db keeps working.

Turn numbers come from the session row and increment once per completed
exchange. Memory decay is measured in turns rather than wall-clock because a
story's pace has nothing to do with how long you left the tab open.
"""
from __future__ import annotations

import array
import json
import sqlite3
import time
from typing import Any, Iterable, Optional

from config import DB_PATH, STATE_DIR

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id         INTEGER PRIMARY KEY,
    story_id   TEXT NOT NULL,
    intro_id   TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    turn       INTEGER NOT NULL DEFAULT 0,
    created    REAL NOT NULL,
    updated    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages(
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    role       TEXT NOT NULL,              -- user | assistant
    content    TEXT NOT NULL,
    turn       INTEGER NOT NULL DEFAULT 0,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS memories(
    id            INTEGER PRIMARY KEY,
    session_id    INTEGER NOT NULL,
    kind          TEXT NOT NULL,           -- long | temp
    text          TEXT NOT NULL,
    salience      REAL NOT NULL DEFAULT 50,
    reinforced    INTEGER NOT NULL DEFAULT 0,
    created_turn  INTEGER NOT NULL DEFAULT 0,
    seen_turn     INTEGER NOT NULL DEFAULT 0,
    embedding     BLOB,
    ts            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_memories_session ON memories(session_id, kind);

CREATE TABLE IF NOT EXISTS relationships(
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL,
    name         TEXT NOT NULL,
    summary      TEXT NOT NULL,
    updated_turn INTEGER NOT NULL DEFAULT 0,
    ts           REAL NOT NULL,
    UNIQUE(session_id, name)
);

CREATE TABLE IF NOT EXISTS goals(
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL,
    text         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | active | complete | dismissed
    created_turn INTEGER NOT NULL DEFAULT 0,
    touched_turn INTEGER NOT NULL DEFAULT 0,        -- last turn the story engaged with it
    ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_goals_session ON goals(session_id, status);

CREATE TABLE IF NOT EXISTS stats(
    session_id INTEGER NOT NULL,
    key        TEXT NOT NULL,
    value      REAL NOT NULL,
    PRIMARY KEY(session_id, key)
);

CREATE TABLE IF NOT EXISTS notes(
    session_id INTEGER PRIMARY KEY,
    text       TEXT NOT NULL DEFAULT '',
    ts         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS media(
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    kind       TEXT NOT NULL,              -- portrait | scene
    subject    TEXT NOT NULL,              -- character name, or a scene slug
    path       TEXT NOT NULL,
    prompt     TEXT NOT NULL DEFAULT '',
    turn       INTEGER NOT NULL DEFAULT 0,
    ts         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_media_session ON media(session_id, kind);

CREATE TABLE IF NOT EXISTS chapters(
    id           INTEGER PRIMARY KEY,
    session_id   INTEGER NOT NULL,
    number       INTEGER NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',   -- open | closed
    start_msg_id INTEGER NOT NULL DEFAULT 0,
    end_msg_id   INTEGER NOT NULL DEFAULT 0,
    start_turn   INTEGER NOT NULL DEFAULT 0,
    end_turn     INTEGER NOT NULL DEFAULT 0,
    ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chapters_session ON chapters(session_id, number);

-- Retired. Unresolved plot threads were a permanent, never-decaying prompt layer
-- meant to protect a Chekhov's gun from the heat scorer. In play they did the
-- opposite: a standing list of loose ends reads to the model as a set of things to
-- tie together, and the story turned into a conspiracy board. The chapter summary
-- covers what the recap needs and the lorebook covers what must not be forgotten.
-- The table is kept, unread, so the rows are not destroyed.
CREATE TABLE IF NOT EXISTS threads(
    id             INTEGER PRIMARY KEY,
    session_id     INTEGER NOT NULL,
    text           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    opened_chapter INTEGER NOT NULL DEFAULT 0,
    closed_chapter INTEGER,
    ts             REAL NOT NULL
);

-- Lore the story earned during play. Same shape and same trigger mechanism as the
-- notes a story.yaml authors up front, because it is the same thing: `keywords` is
-- a comma-joined list, matched against the recent transcript. `always` pins an
-- entry into every prompt. Suggestions live here too, at status 'suggested', and
-- contribute nothing until accepted.
CREATE TABLE IF NOT EXISTS lorebook(
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    keywords    TEXT NOT NULL DEFAULT '',
    always      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'active',   -- active | suggested | dismissed
    source      TEXT NOT NULL DEFAULT 'manual',   -- manual | auto
    chapter     INTEGER NOT NULL DEFAULT 0,
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lorebook_session ON lorebook(session_id, status);

CREATE TABLE IF NOT EXISTS synopsis(
    session_id   INTEGER PRIMARY KEY,
    text         TEXT NOT NULL DEFAULT '',
    upto_chapter INTEGER NOT NULL DEFAULT 0,
    ts           REAL NOT NULL
);

-- Who the player is, for stories that let you decide. Per session, not per
-- story: two people can play the same story as different characters, and the
-- story file only supplies the defaults.
CREATE TABLE IF NOT EXISTS protagonist(
    session_id  INTEGER PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    short       TEXT NOT NULL DEFAULT '',
    pronouns    TEXT NOT NULL DEFAULT 'they/them',
    description TEXT NOT NULL DEFAULT '',
    power_label TEXT NOT NULL DEFAULT '',
    power_name  TEXT NOT NULL DEFAULT '',
    power       TEXT NOT NULL DEFAULT '',
    prompt      TEXT NOT NULL DEFAULT '',
    ts          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts    REAL NOT NULL
);
"""


# Columns added to tables that already exist in deployed databases. CREATE TABLE
# IF NOT EXISTS is a no-op on those, so a new column needs its own ALTER. Applied
# once per process rather than per connection, which is every request.
_ADDED_COLUMNS = [
    ("goals", "touched_turn", "INTEGER NOT NULL DEFAULT 0"),
    # A reply that hit the provider's length limit is unfinished, not over. The
    # stream ends identically either way, so without this flag a beat cut off
    # mid-sentence was indistinguishable from a beat that ended.
    ("messages", "truncated", "INTEGER NOT NULL DEFAULT 0"),
]
_migrated = False


def _migrate(c: sqlite3.Connection) -> None:
    global _migrated
    if _migrated:
        return
    for table, column, decl in _ADDED_COLUMNS:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    _migrated = True


def _conn() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript(_SCHEMA)
    _migrate(c)
    return c


# ---------------------------------------------------------------- embeddings


def pack_embedding(vec: Iterable[float]) -> bytes:
    return array.array("f", vec).tobytes()


def unpack_embedding(blob: Optional[bytes]) -> list[float]:
    if not blob:
        return []
    a = array.array("f")
    a.frombytes(blob)
    return list(a)


# ---------------------------------------------------------------- sessions


def create_session(story_id: str, intro_id: str, title: str = "") -> int:
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO sessions(story_id, intro_id, title, turn, created, updated)"
            " VALUES(?,?,?,0,?,?)",
            (story_id, intro_id, title, now, now),
        )
        return int(cur.lastrowid)


def get_session(session_id: int) -> Optional[dict]:
    with _conn() as c:
        r = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return dict(r) if r else None


def list_sessions() -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS messages"
            " FROM sessions s ORDER BY s.updated DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def bump_turn(session_id: int) -> int:
    with _conn() as c:
        c.execute(
            "UPDATE sessions SET turn = turn + 1, updated = ? WHERE id = ?",
            (time.time(), session_id),
        )
        r = c.execute("SELECT turn FROM sessions WHERE id=?", (session_id,)).fetchone()
    return int(r["turn"]) if r else 0


def set_turn(session_id: int, turn: int) -> None:
    """Put the turn counter at an absolute value.

    Only importing a pre-existing story needs this — everything played walks the
    counter with bump_turn(). It exists because the chapter clock, the pacing
    cadence and the goal nudge all measure against this number, so a session
    whose transcript starts at turn 56 must say so or all three start over.
    """
    with _conn() as c:
        c.execute("UPDATE sessions SET turn=?, updated=? WHERE id=?",
                  (int(turn), time.time(), session_id))


def current_turn(session_id: int) -> int:
    s = get_session(session_id)
    return int(s["turn"]) if s else 0


def delete_session(session_id: int) -> None:
    with _conn() as c:
        for t in ("messages", "memories", "relationships", "goals", "stats", "notes",
                  "media", "chapters", "threads", "synopsis", "lorebook",
                  "protagonist"):
            c.execute(f"DELETE FROM {t} WHERE session_id=?", (session_id,))
        c.execute("DELETE FROM sessions WHERE id=?", (session_id,))


# ---------------------------------------------------------------- messages


def add_message(session_id: int, role: str, content: str, turn: int) -> int:
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO messages(session_id, role, content, turn, ts) VALUES(?,?,?,?,?)",
            (session_id, role, content, turn, now),
        )
        c.execute("UPDATE sessions SET updated=? WHERE id=?", (now, session_id))
        return int(cur.lastrowid)


def messages(
    session_id: int, limit: Optional[int] = None, *, since_id: Optional[int] = None
) -> list[dict]:
    """Oldest -> newest. `limit` takes the most recent N.

    `since_id` returns only messages after that id, which is how the transcript is
    scoped to the current chapter: once a chapter closes its messages are
    represented by its summary and no longer belong in the prompt.
    """
    where = "session_id=?"
    args: list[Any] = [session_id]
    if since_id is not None:
        where += " AND id > ?"
        args.append(since_id)
    with _conn() as c:
        if limit is None:
            rows = c.execute(
                f"SELECT * FROM messages WHERE {where} ORDER BY id ASC", args
            ).fetchall()
            return [dict(r) for r in rows]
        rows = c.execute(
            f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?",
            (*args, limit),
        ).fetchall()
    out = [dict(r) for r in rows]
    out.reverse()
    return out


def update_message(message_id: int, content: str) -> None:
    """Edit a message. Editing by hand also finishes it: whatever the provider
    stopped at, the text on disk is now the author's, so the resume flag goes."""
    with _conn() as c:
        c.execute("UPDATE messages SET content=?, truncated=0 WHERE id=?",
                  (content, message_id))


def set_truncated(message_id: int, flag: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE messages SET truncated=? WHERE id=?",
                  (1 if flag else 0, message_id))


def append_message(message_id: int, text: str, *, truncated: bool = False) -> str:
    """Extend a reply in place and return the whole thing.

    A resumed turn belongs to the message it is finishing, not to a new one:
    appended as a second assistant message it would read as two replies in the
    transcript, and the chapter, the digest and the budget arbiter would all
    count it twice.

    The join is a bare concatenation. The model is told to resume from the exact
    character it stopped at, so inserting a space or a newline here would put one
    inside a word the provider happened to cut in half.
    """
    with _conn() as c:
        row = c.execute("SELECT content FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise KeyError(f"no message {message_id}")
        merged = row["content"] + text
        c.execute("UPDATE messages SET content=?, truncated=? WHERE id=?",
                  (merged, 1 if truncated else 0, message_id))
    return merged


def last_message(session_id: int, role: Optional[str] = None) -> Optional[dict]:
    q = "SELECT * FROM messages WHERE session_id=?"
    args: list[Any] = [session_id]
    if role:
        q += " AND role=?"
        args.append(role)
    q += " ORDER BY id DESC LIMIT 1"
    with _conn() as c:
        row = c.execute(q, args).fetchone()
        return dict(row) if row else None


def delete_message(message_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM messages WHERE id=?", (message_id,))


def delete_messages_from(session_id: int, message_id: int) -> None:
    """Raw message deletion. Prefer rewind_to() — see why there."""
    with _conn() as c:
        c.execute(
            "DELETE FROM messages WHERE session_id=? AND id>=?", (session_id, message_id)
        )


def rewind_to(session_id: int, message_id: int) -> dict:
    """Delete this message, everything after it, and everything derived from it.

    Deleting the messages alone does not undo the turns. Every turn also writes
    memories, goals, relationships, stat changes, scene art, a chapter boundary
    and a pacing marker, and all of that outlives the text it came from. Measured
    on a ten-turn session cut in half: the transcript lost five turns and the
    prompt still carried ten long-term memories, ten goals and a relationship
    summary describing a conversation that no longer existed. From the player's
    side that reads as the delete not working, because the narrator goes on
    knowing things it should have forgotten.

    So the cut is by TURN, not by row. Everything stamped at or after the turn
    being cut goes with it.

    Two deliberate exceptions:
      - Stats are not rolled back. Only the current value is stored, never the
        deltas, so there is nothing to reverse. Adjust them by hand if it matters.
      - The lorebook is not touched. It is the one layer the author curates by
        hand, and silently deleting accepted entries would be worse than leaving
        a stale one that takes two clicks to remove.
    """
    # Explicit open/close rather than `with`: the context manager commits on exit
    # and would be handed an already-closed connection.
    c = _conn()
    c.isolation_level = None
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute(
            "SELECT turn FROM messages WHERE id=? AND session_id=?",
            (message_id, session_id),
        ).fetchone()
        if not row:
            c.execute("COMMIT")
            return {"messages": 0}
        turn = int(row["turn"])

        n_msg = c.execute(
            "DELETE FROM messages WHERE session_id=? AND id>=?",
            (session_id, message_id)).rowcount

        # created_turn, not seen_turn: reinforcement bumps seen_turn, so an
        # old memory the story kept returning to would otherwise look recent
        # and be deleted along with the turns that merely mentioned it.
        n_mem = c.execute(
            "DELETE FROM memories WHERE session_id=? AND created_turn>=?",
            (session_id, turn)).rowcount
        n_goal = c.execute(
            "DELETE FROM goals WHERE session_id=? AND created_turn>=?",
            (session_id, turn)).rowcount
        # A goal finished during the cut turns is unfinished again.
        n_reopen = c.execute(
            "UPDATE goals SET status='active', touched_turn=? WHERE session_id=?"
            " AND created_turn<? AND touched_turn>=? AND status IN ('complete','dismissed')",
            (max(0, turn - 1), session_id, turn, turn)).rowcount
        # Standing is a running summary with no history, so one written during
        # the cut turns cannot be reverted — only dropped. "Nothing recorded
        # yet" is at least true; a summary of deleted events is not.
        n_rel = c.execute(
            "DELETE FROM relationships WHERE session_id=? AND updated_turn>=?",
            (session_id, turn)).rowcount
        # Scenes depict a moment that no longer happened. Portraits depict a
        # person, and people survive a rewind, so those stay.
        n_media = c.execute(
            "DELETE FROM media WHERE session_id=? AND kind='scene' AND turn>=?",
            (session_id, turn)).rowcount

        n_chap = c.execute(
            "DELETE FROM chapters WHERE session_id=? AND start_turn>=?",
            (session_id, turn)).rowcount
        # A chapter that straddles the cut was summarised from text that is
        # now partly gone. Reopen it and let it be compacted again later.
        n_unclose = c.execute(
            "UPDATE chapters SET status='open', title='', summary='',"
            " end_msg_id=0, end_turn=0 WHERE session_id=? AND status='closed'"
            " AND end_turn>=?", (session_id, turn)).rowcount

        # The turn counter has to come back too, or chapter length, the goal
        # nudge and the action cadence all keep counting turns that were
        # deleted.
        last = c.execute(
            "SELECT COALESCE(MAX(turn), 0) t FROM messages WHERE session_id=?",
            (session_id,)).fetchone()["t"]
        c.execute("UPDATE sessions SET turn=?, updated=? WHERE id=?",
                  (int(last), time.time(), session_id))
        c.execute("DELETE FROM meta WHERE key=? AND CAST(value AS INTEGER)>=?",
                  (f"action:{session_id}", turn))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()

    return {"turn": turn, "now_turn": int(last), "messages": n_msg, "memories": n_mem,
            "goals": n_goal, "goals_reopened": n_reopen, "relationships": n_rel,
            "scenes": n_media, "chapters": n_chap, "chapters_reopened": n_unclose}


# ---------------------------------------------------------------- memories


def add_memory(
    session_id: int,
    text: str,
    kind: str = "temp",
    salience: float = 50.0,
    turn: int = 0,
    embedding: Optional[Iterable[float]] = None,
) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO memories(session_id, kind, text, salience, reinforced,"
            " created_turn, seen_turn, embedding, ts) VALUES(?,?,?,?,0,?,?,?,?)",
            (
                session_id,
                kind,
                text,
                float(salience),
                turn,
                turn,
                pack_embedding(embedding) if embedding else None,
                time.time(),
            ),
        )
        return int(cur.lastrowid)


def memories(session_id: int, kind: Optional[str] = None) -> list[dict]:
    q = "SELECT * FROM memories WHERE session_id=?"
    args: list[Any] = [session_id]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    q += " ORDER BY id DESC"
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def reinforce(memory_ids: Iterable[int], turn: int) -> None:
    ids = list(memory_ids)
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    with _conn() as c:
        c.execute(
            f"UPDATE memories SET reinforced = reinforced + 1, seen_turn = ?"
            f" WHERE id IN ({marks})",
            [turn, *ids],
        )


def promote_memory(memory_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE memories SET kind='long' WHERE id=?", (memory_id,))


def drop_memories(memory_ids: Iterable[int]) -> None:
    ids = list(memory_ids)
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    with _conn() as c:
        c.execute(f"DELETE FROM memories WHERE id IN ({marks})", ids)


def prune_long(session_id: int, keep: int) -> list[int]:
    """Trim long-term storage back to `keep` rows, dropping the least earned first.

    Order is fewest references, then lowest salience, then oldest — noise first,
    as close as this can get without knowing the future.

    Exempting referenced rows entirely was the obvious rule and it was useless:
    promotion from temp storage *requires* a reference, so on a real 132-turn
    session 416 of 497 rows carried exactly one and only 76 were eligible. A cap
    that cannot reach its target is not a cap. Well-referenced memories still
    survive — they sort to the very end of this ordering — but nothing is
    permanently exempt.
    """
    with _conn() as c:
        total = c.execute(
            "SELECT COUNT(*) n FROM memories WHERE session_id=? AND kind='long'",
            (session_id,),
        ).fetchone()["n"]
        surplus = total - max(0, keep)
        if surplus <= 0:
            return []
        rows = c.execute(
            "SELECT id FROM memories WHERE session_id=? AND kind='long'"
            " ORDER BY reinforced ASC, salience ASC, seen_turn ASC, id ASC LIMIT ?",
            (session_id, surplus),
        ).fetchall()
        ids = [int(r["id"]) for r in rows]
        if ids:
            c.execute(
                f"DELETE FROM memories WHERE id IN ({','.join('?' * len(ids))})", ids
            )
        return ids


def set_memory_embedding(memory_id: int, vec: Iterable[float]) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE memories SET embedding=? WHERE id=?", (pack_embedding(vec), memory_id)
        )


# ---------------------------------------------------------------- relationships


def upsert_relationship(session_id: int, name: str, summary: str, turn: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO relationships(session_id, name, summary, updated_turn, ts)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(session_id, name) DO UPDATE SET"
            " summary=excluded.summary, updated_turn=excluded.updated_turn, ts=excluded.ts",
            (session_id, name.strip(), summary.strip(), turn, time.time()),
        )


def relationships(session_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM relationships WHERE session_id=? ORDER BY updated_turn DESC",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_relationship(session_id: int, name: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM relationships WHERE session_id=? AND name=?", (session_id, name)
        )


# ---------------------------------------------------------------- goals


def add_goal(session_id: int, text: str, status: str, turn: int) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO goals(session_id, text, status, created_turn, touched_turn, ts)"
            " VALUES(?,?,?,?,?,?)",
            (session_id, text.strip(), status, turn, turn, time.time()),
        )
        return int(cur.lastrowid)


def goals(session_id: int, statuses: Optional[Iterable[str]] = None) -> list[dict]:
    q = "SELECT * FROM goals WHERE session_id=?"
    args: list[Any] = [session_id]
    if statuses:
        st = list(statuses)
        q += f" AND status IN ({','.join('?' * len(st))})"
        args += st
    q += " ORDER BY id ASC"
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    return [dict(r) for r in rows]


def set_goal_status(goal_id: int, status: str, turn: int = 0) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE goals SET status=?, touched_turn=MAX(touched_turn, ?) WHERE id=?",
            (status, turn, goal_id),
        )


def touch_goal(goal_id: int, turn: int) -> None:
    """Record that this turn engaged with the goal — mentioned it, or moved it on.

    The nudge is timed from here, so anything that counts as the objective being
    alive in the story has to land on this column or the reminder fires while the
    characters are actively working on it.
    """
    with _conn() as c:
        c.execute("UPDATE goals SET touched_turn=? WHERE id=?", (turn, goal_id))


def active_goal(session_id: int) -> Optional[dict]:
    """The single objective the story is currently pointed at, if there is one."""
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM goals WHERE session_id=? AND status='active' ORDER BY id ASC LIMIT 1",
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def update_goal_text(goal_id: int, text: str) -> None:
    with _conn() as c:
        c.execute("UPDATE goals SET text=? WHERE id=?", (text, goal_id))


# ---------------------------------------------------------------- stats


def set_stat(session_id: int, key: str, value: float) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO stats(session_id, key, value) VALUES(?,?,?)"
            " ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value",
            (session_id, key, float(value)),
        )


def adjust_stat(session_id: int, key: str, delta: float, lo: float, hi: float) -> float:
    cur = stats(session_id).get(key, 0.0)
    new = max(lo, min(hi, cur + float(delta)))
    set_stat(session_id, key, new)
    return new


def stats(session_id: int) -> dict[str, float]:
    with _conn() as c:
        rows = c.execute(
            "SELECT key, value FROM stats WHERE session_id=?", (session_id,)
        ).fetchall()
    return {r["key"]: r["value"] for r in rows}


# ---------------------------------------------------------------- director notes


def set_notes(session_id: int, text: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO notes(session_id, text, ts) VALUES(?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET text=excluded.text, ts=excluded.ts",
            (session_id, text, time.time()),
        )


def notes(session_id: int) -> str:
    with _conn() as c:
        r = c.execute("SELECT text FROM notes WHERE session_id=?", (session_id,)).fetchone()
    return r["text"] if r else ""


# ---------------------------------------------------------------- media


def add_media(
    session_id: int, kind: str, subject: str, path: str, prompt: str, turn: int
) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO media(session_id, kind, subject, path, prompt, turn, ts)"
            " VALUES(?,?,?,?,?,?,?)",
            (session_id, kind, subject, path, prompt, turn, time.time()),
        )
        return int(cur.lastrowid)


def find_media(session_id: int, kind: str, subject: str) -> Optional[dict]:
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM media WHERE session_id=? AND kind=? AND subject=?"
            " ORDER BY id DESC LIMIT 1",
            (session_id, kind, subject),
        ).fetchone()
    return dict(r) if r else None


def media(session_id: int, kind: Optional[str] = None) -> list[dict]:
    q = "SELECT * FROM media WHERE session_id=?"
    args: list[Any] = [session_id]
    if kind:
        q += " AND kind=?"
        args.append(kind)
    q += " ORDER BY id DESC"
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- meta


def set_meta(key: str, value: Any) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO meta(key, value, ts) VALUES(?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (key, json.dumps(value), time.time()),
        )


def clear_meta(key: str) -> None:
    """Delete a meta row. Distinct from storing null — the arc override treats
    'no row' as 'follow the clock' and any stored value as 'the reader decided'."""
    with _conn() as c:
        c.execute("DELETE FROM meta WHERE key=?", (key,))


def get_meta(key: str, default: Any = None) -> Any:
    with _conn() as c:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


# ---------------------------------------------------------------- chapters
#
# A chapter is a span of messages that has been summarised and released from the
# prompt. Exactly one chapter per session is `open` at a time; closing it opens
# the next. Threads outlive chapters entirely — that is the whole point of them.


def open_chapter(session_id: int) -> dict:
    """The current chapter, creating chapter 1 on first use.

    Serialised with BEGIN IMMEDIATE because this is called from several places
    per request (session_state, the turn loop, every chapter route) and the
    browser fires those concurrently. Read-then-insert on separate connections
    let two callers both see "no open chapter" and both create one, which showed
    up as chapter 1 arriving with row id 2.
    """
    c = _conn()
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT * FROM chapters WHERE session_id=? AND status='open'"
            " ORDER BY number DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row:
            c.execute("COMMIT")
            return dict(row)
        prev = c.execute(
            "SELECT number, end_msg_id, end_turn FROM chapters WHERE session_id=?"
            " ORDER BY number DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        number = (prev["number"] + 1) if prev else 1
        start_msg = prev["end_msg_id"] if prev else 0
        start_turn = prev["end_turn"] if prev else 0
        cur = c.execute(
            "INSERT INTO chapters(session_id, number, start_msg_id, start_turn, ts)"
            " VALUES(?,?,?,?,?)",
            (session_id, number, start_msg, start_turn, time.time()),
        )
        out = dict(c.execute("SELECT * FROM chapters WHERE id=?", (cur.lastrowid,)).fetchone())
        c.execute("COMMIT")
        return out
    finally:
        c.close()


def chapters(session_id: int, *, closed_only: bool = False) -> list[dict]:
    q = "SELECT * FROM chapters WHERE session_id=?"
    if closed_only:
        q += " AND status='closed'"
    q += " ORDER BY number ASC"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, (session_id,)).fetchall()]


def close_chapter(
    chapter_id: int, title: str, summary: str, end_msg_id: int, end_turn: int
) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE chapters SET status='closed', title=?, summary=?,"
            " end_msg_id=?, end_turn=? WHERE id=?",
            (title.strip(), summary.strip(), end_msg_id, end_turn, chapter_id),
        )


def update_chapter(chapter_id: int, title: str, summary: str) -> None:
    """Edit a closed chapter after the fact — how a detail that turns out to
    matter later gets rescued into the prompt."""
    with _conn() as c:
        c.execute(
            "UPDATE chapters SET title=?, summary=? WHERE id=?",
            (title.strip(), summary.strip(), chapter_id),
        )


# ---------------------------------------------------------------- lorebook


def rename_media_subject(session_id: int, old: str, new: str) -> None:
    """Re-file a character's images under a new name.

    Media is keyed by subject name, so renaming the player character would
    otherwise orphan their portrait: it would drop out of the cast panel and the
    next request would render a fresh one from the same prompt.
    """
    with _conn() as c:
        c.execute("UPDATE media SET subject=? WHERE session_id=? AND subject=?",
                  (new.strip(), session_id, old.strip()))


_PRO_FIELDS = ("name", "short", "pronouns", "description",
               "power_label", "power_name", "power", "prompt")


def protagonist(session_id: int) -> Optional[dict]:
    with _conn() as c:
        row = c.execute("SELECT * FROM protagonist WHERE session_id=?",
                        (session_id,)).fetchone()
    return dict(row) if row else None


def set_protagonist(session_id: int, values: dict) -> dict:
    """Insert or update. Absent keys keep whatever is already stored."""
    current = protagonist(session_id) or {k: "" for k in _PRO_FIELDS}
    merged = {k: str(values.get(k, current.get(k, "")) or "").strip() for k in _PRO_FIELDS}
    merged["pronouns"] = merged["pronouns"] or "they/them"
    merged["short"] = merged["short"] or (merged["name"].split() or [""])[0]
    with _conn() as c:
        c.execute(
            "INSERT INTO protagonist(session_id, name, short, pronouns, description,"
            " power_label, power_name, power, prompt, ts) VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET name=excluded.name,"
            " short=excluded.short, pronouns=excluded.pronouns,"
            " description=excluded.description, power_label=excluded.power_label,"
            " power_name=excluded.power_name, power=excluded.power,"
            " prompt=excluded.prompt, ts=excluded.ts",
            (session_id, *(merged[k] for k in _PRO_FIELDS), time.time()),
        )
    return {"session_id": session_id, **merged}


def add_lore(
    session_id: int,
    title: str,
    body: str,
    keywords: Iterable[str],
    *,
    always: bool = False,
    status: str = "active",
    source: str = "manual",
    chapter: int = 0,
) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO lorebook(session_id, title, body, keywords, always, status,"
            " source, chapter, ts) VALUES(?,?,?,?,?,?,?,?,?)",
            (session_id, title.strip(), body.strip(), _join_kw(keywords),
             1 if always else 0, status, source, chapter, time.time()),
        )
        return int(cur.lastrowid)


def lore(session_id: int, statuses: Optional[Iterable[str]] = None) -> list[dict]:
    q = "SELECT * FROM lorebook WHERE session_id=?"
    args: list[Any] = [session_id]
    if statuses:
        st = list(statuses)
        q += f" AND status IN ({','.join('?' * len(st))})"
        args += st
    q += " ORDER BY always DESC, id ASC"
    with _conn() as c:
        rows = c.execute(q, args).fetchall()
    return [_lore_row(r) for r in rows]


def update_lore(
    entry_id: int,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    keywords: Optional[Iterable[str]] = None,
    always: Optional[bool] = None,
    status: Optional[str] = None,
) -> None:
    sets: list[str] = []
    args: list[Any] = []
    if title is not None:
        sets.append("title=?"); args.append(title.strip())
    if body is not None:
        sets.append("body=?"); args.append(body.strip())
    if keywords is not None:
        sets.append("keywords=?"); args.append(_join_kw(keywords))
    if always is not None:
        sets.append("always=?"); args.append(1 if always else 0)
    if status is not None:
        sets.append("status=?"); args.append(status)
    if not sets:
        return
    args.append(entry_id)
    with _conn() as c:
        c.execute(f"UPDATE lorebook SET {','.join(sets)} WHERE id=?", args)


def delete_lore(entry_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM lorebook WHERE id=?", (entry_id,))


def _join_kw(keywords: Iterable[str]) -> str:
    seen: list[str] = []
    for k in keywords or []:
        k = " ".join(str(k).split()).strip().lower()
        if k and k not in seen:
            seen.append(k)
    return ", ".join(seen)


def _lore_row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["keywords"] = [k.strip() for k in (d.get("keywords") or "").split(",") if k.strip()]
    d["always"] = bool(d.get("always"))
    return d


def synopsis(session_id: int) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM synopsis WHERE session_id=?", (session_id,)
        ).fetchone()
    return dict(row) if row else {"session_id": session_id, "text": "", "upto_chapter": 0}


def set_synopsis(session_id: int, text: str, upto_chapter: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO synopsis(session_id, text, upto_chapter, ts) VALUES(?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET"
            " text=excluded.text, upto_chapter=excluded.upto_chapter, ts=excluded.ts",
            (session_id, text.strip(), upto_chapter, time.time()),
        )


def chapter_transcript_floor(session_id: int, overlap: int) -> Optional[int]:
    """Lowest message id the prompt should still include.

    Everything up to the last closed chapter is represented by that chapter's
    summary and does not belong in the transcript any more — except for a short
    overlap, so the first turn of a new chapter still has recent verbatim text to
    match voice against instead of reading like a jump cut.

    Returns None when nothing has been closed yet.
    """
    with _conn() as c:
        row = c.execute(
            "SELECT end_msg_id FROM chapters WHERE session_id=? AND status='closed'"
            " ORDER BY number DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if not row or not row["end_msg_id"]:
            return None
        end = int(row["end_msg_id"])
        if overlap <= 0:
            return end
        back = c.execute(
            "SELECT id FROM messages WHERE session_id=? AND id<=? ORDER BY id DESC LIMIT ?",
            (session_id, end, overlap),
        ).fetchall()
    return (min(int(r["id"]) for r in back) - 1) if back else end


def delete_media(media_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM media WHERE id=?", (media_id,))
