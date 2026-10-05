'use strict';

const $ = (id) => document.getElementById(id);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt != null) n.textContent = txt;
  return n;
};
const api = async (path, body) => {
  const r = await fetch(path, body === undefined
    ? {}
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
};

const S = { id: null, state: null, busy: false, notesTimer: null };

/* Confirmation, in-page rather than window.confirm().

   window.confirm() is suppressible: both Chrome and Firefox offer "prevent this
   page from creating additional dialogs" once a page has shown a few, and after
   that every confirm() returns FALSE immediately with no dialog, for the life of
   the tab. Every confirm-gated action in the app then silently does nothing —
   which is exactly how "delete does nothing" presents, with no network request
   and no error to find.

   A <dialog> cannot be suppressed, and it fails open rather than closed. */
function ask(message, yes = 'Delete') {
  return new Promise((resolve) => {
    const d = $('askDlg');
    $('askText').textContent = message;
    $('askYes').textContent = yes;
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      d.removeEventListener('close', onClose);
      d.close();
      resolve(v);
    };
    const onClose = () => finish(false);          // Escape, or the backdrop
    $('askYes').onclick = () => finish(true);
    $('askNo').onclick = () => finish(false);
    d.addEventListener('close', onClose);
    d.showModal();
  });
}

/* Destructive actions used to swallow their own failures: an api() rejection in
   an un-caught async onclick is an unhandled promise rejection, which looks
   identical to the click not registering. */
async function guard(fn, what) {
  try {
    await fn();
  } catch (e) {
    $('status').textContent = `${what} failed: ${e.message}`;
    setTimeout(() => { $('status').textContent = ''; }, 6000);
  }
}

/* ------------------------------------------------------------------ scrolling

   Streaming used to scroll to the bottom on every token, which makes reading
   along impossible: scroll up to re-read a line and the next token yanks you
   back. Instead the view is "sticky" — it follows new text only while you are
   already at the bottom. Scroll up and it leaves you alone until you come back
   or press the jump button. */

const NEAR_BOTTOM = 90;   // px of slack that still counts as "at the bottom"

function atBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight <= NEAR_BOTTOM;
}

function toBottom(box) {
  box.scrollTop = box.scrollHeight;
}

/* Run a re-render without moving the reader. Pinned to the bottom, we follow the
   new content; otherwise we put them back exactly where they were.

   Restore scrollTop, not distance-from-bottom: this list only ever grows at the
   end, so holding the offset from the top is what actually keeps a paragraph
   under the reader's eye. (Distance-from-bottom would be right if we prepended
   older messages, which we never do.) Capturing it is necessary because
   renderMessages clears the container, which collapses scrollHeight and makes
   the browser clamp scrollTop to zero. */
function keepingPlace(box, fn) {
  const stick = atBottom(box);
  const top = box.scrollTop;
  fn();
  if (stick) toBottom(box);
  else box.scrollTop = top;
  updateLatestButton();
}

function updateLatestButton() {
  const box = $('messages');
  if (!box) return;
  $('toLatest').classList.toggle('hidden', atBottom(box));
}

/* ------------------------------------------------------------------ boot */

async function boot() {
  const [stories, sessions, health] = await Promise.all([
    api('/api/stories'), api('/api/sessions'), api('/api/health').catch(() => null),
  ]);

  const sw = $('bootStories');
  sw.innerHTML = '';
  stories.forEach((st) => {
    const broken = !!st.error;
    const c = el('div', 'card' + (broken ? ' err' : ''));
    c.append(el('b', null, broken ? st.id : st.name),
             el('span', 'muted small', broken ? st.error.split('\n')[0] : st.tagline));
    const edit = el('span', 'del', broken ? 'Fix' : '✎');
    edit.title = broken ? 'Open in the editor to fix' : 'Edit this story';
    edit.onclick = (e) => { e.stopPropagation(); editStory(st.id); };
    c.append(edit);
    // A story that fails validation cannot be played, but it must still be editable.
    if (!broken) c.onclick = () => start(st.id);
    sw.append(c);
  });

  const add = el('div', 'card');
  add.append(el('b', null, '+ New story'),
             el('span', 'muted small', 'Write one from scratch'));
  add.onclick = newStory;
  sw.append(add);

  const sv = $('bootSessions');
  sv.innerHTML = '';
  if (!sessions.length) sv.append(el('div', 'muted small', 'No sessions yet.'));
  sessions.forEach((s) => {
    const c = el('div', 'card');
    c.append(el('b', null, s.title || s.story_id),
             el('span', 'muted small', `turn ${s.turn} · ${s.messages} messages`));
    const del = el('span', 'del', '✕');
    del.title = 'Delete session';
    del.onclick = async (e) => {
      e.stopPropagation();
      if (await ask('Delete this session and all its memory?')) {
        await api('/api/session/delete', { session: s.id });
        boot();
      }
    };
    c.append(del);
    c.onclick = () => open(s.id);
    sv.append(c);
  });

  if (health) {
    const bits = [`prose ${health.prose}`, `utility ${health.utility}`];
    bits.push(health.embeddings ? `embeddings ${health.embeddings}d` : 'embeddings off');
    bits.push(health.comfy ? 'comfy up' : 'comfy unreachable');
    $('bootHealth').textContent = bits.join(' · ');

    // The prose model is the one thing whose outage stops play entirely, so it
    // gets checked for real rather than assumed from the config.
    api('/api/probe?provider=' + encodeURIComponent(health.prose_provider || 'ollama')
        + '&model=' + encodeURIComponent(health.prose)).then((r) => {
      const row = r.results?.[0];
      if (!row || row.ok) return;
      const warn = el('div', 'bootWarn');
      warn.textContent = row.state === 'busy'
        ? `${health.prose} is overloaded right now (${row.code}). Turns will retry, `
          + 'but you may want a different prose model in Settings → Models.'
        : `${health.prose} is not answering: ${row.error || 'unknown error'}`;
      $('bootHealth').after(warn);
    }).catch(() => {});
  }
}

async function start(storyId) {
  // A story that declares a protagonist is asking who you want to be, and the
  // answer has to be settled before the prologue is written — it names you, and
  // it is stored as a real message rather than re-rendered every load.
  let story = null;
  try { story = await api('/api/story/' + encodeURIComponent(storyId)); } catch { /* play anyway */ }
  if (story?.protagonist?.name) {
    openPC({ storyId, defaults: story.protagonist });
    return;
  }
  const st = await api('/api/session', { story: storyId });
  S.id = st.session_id;
  render(st);
  show();
}

/* ------------------------------------------------------------------ the player

   One form, two jobs: character creation before a session exists, and editing
   afterwards. They are the same fields and the same validation, and splitting
   them into two screens would mean two places to get the pronoun handling
   wrong. */

const PC = { mode: null, storyId: null, defaults: null };

function pcField(body, label, help, value, opts = {}) {
  body.append(el('h4', null, label));
  if (help) body.append(el('div', 'lede', help));
  const node = opts.rows ? document.createElement('textarea') : el('input');
  if (opts.rows) node.rows = opts.rows; else node.type = 'text';
  if (opts.placeholder) node.placeholder = opts.placeholder;
  node.value = value || '';
  body.append(node);
  return node;
}

function openPC({ storyId = null, defaults = null } = {}) {
  const editing = !storyId;
  const cur = editing ? (S.state?.protagonist || {}) : defaults;
  PC.mode = editing ? 'edit' : 'create';
  PC.storyId = storyId;
  PC.defaults = defaults;

  $('pcDlgTitle').textContent = editing ? 'Your character' : 'Who are you?';
  $('pcSave').textContent = editing ? 'Save' : 'Begin';
  $('pcStatus').textContent = '';
  $('pcReset').classList.toggle('hidden', !editing);

  const body = $('pcBody');
  body.innerHTML = '';
  if (!editing) {
    body.append(el('div', 'lede',
      'These are the story’s defaults. Change as much or as little as you like — '
      + 'everything here goes into the prompt exactly as you write it, and you can '
      + 'come back and change it mid-story.'));
  }

  const f = {};
  f.name = pcField(body, 'Name', '', cur.name);
  f.short = pcField(body, 'Called', 'What people actually call you day to day.', cur.short);
  f.pronouns = pcField(body, 'Pronouns',
    'Written into the prompt as you type it. he/him, she/her, they/them, or anything else.',
    cur.pronouns, { placeholder: 'they/them' });
  f.description = pcField(body, 'Who you are',
    'Age, background, temperament, what you are doing here. Prose, not a stat block — '
    + 'the narrator reads this every turn.', cur.description, { rows: 7 });

  body.append(el('h4', null, (cur.power_label || 'Ability') + ' name'));
  const powRow = el('div', 'pcRow');
  const label = el('input');
  label.type = 'text';
  label.value = cur.power_label || '';
  label.placeholder = 'Quirk';
  label.title = 'What this world calls a power';
  label.className = 'pcLabel';
  const pname = el('input');
  pname.type = 'text';
  pname.value = cur.power_name || '';
  powRow.append(label, pname);
  body.append(powRow);
  f.power_label = label;
  f.power_name = pname;

  f.power = pcField(body, 'How it works',
    'Be specific about the limits — vague powers make vague scenes. This is the text '
    + 'the narrator uses to decide what you can and cannot do.', cur.power, { rows: 8 });
  f.prompt = pcField(body, 'Appearance',
    'Comma-separated image tags for your portrait. Not used in the prose.',
    cur.prompt, { rows: 3 });

  // Live size against the layer's allowance. A character sheet that overruns is
  // not an error anywhere — the arbiter simply trims the tail — so without this
  // the only symptom is the narrator quietly not knowing what your power does.
  const gauge = el('div', 'pcGauge');
  body.append(gauge);
  const CAP = 4500;
  const measure = () => {
    const n = f.name.value.length + f.description.value.length
            + f.power.value.length + f.power_name.value.length + 60;
    const over = n > CAP;
    gauge.textContent = over
      ? `${n} of ${CAP} characters — too long. The end of your ${(f.power_label.value || 'ability').toLowerCase()} `
        + 'description will be cut from the prompt. Trim it, or raise the '
        + '"protagonist" ceiling under Settings → Context budget.'
      : `${n} of ${CAP} characters`;
    gauge.className = 'pcGauge' + (over ? ' over' : '');
  };
  [f.name, f.description, f.power, f.power_name, f.power_label]
    .forEach((n) => n.addEventListener('input', measure));
  measure();

  $('pcReset').onclick = async () => {
    if (!await ask('Discard your character and go back to the story’s default?', 'Reset')) return;
    render(await api('/api/protagonist', { session: S.id, reset: true }));
    $('pcDlg').close();
  };

  $('pcSave').onclick = async () => {
    const values = Object.fromEntries(Object.entries(f).map(([k, n]) => [k, n.value]));
    if (!values.name.trim()) { $('pcStatus').textContent = 'needs a name'; return; }
    $('pcSave').disabled = true;
    $('pcStatus').textContent = editing ? 'saving…' : 'starting…';
    try {
      if (editing) {
        render(await api('/api/protagonist', { session: S.id, values }));
        refreshContext();
      } else {
        const st = await api('/api/session', { story: PC.storyId, protagonist: values });
        S.id = st.session_id;
        render(st);
        show();
      }
      $('pcDlg').close();
    } catch (e) {
      $('pcStatus').textContent = e.message;
    } finally {
      $('pcSave').disabled = false;
    }
  };

  $('pcDlg').showModal();
}

function renderYou(state) {
  const pro = state.protagonist;
  $('youPanel').classList.toggle('hidden', !pro);
  if (!pro) return;

  // Deliberately the same row shape and the same makePortrait() call the cast
  // panel uses. The player is a character in this story like any other, and the
  // one place they were not was the only place they could not get a picture.
  const shot = (state.media || [])
    .filter((m) => m.kind === 'portrait' && m.subject === pro.name)
    .map((m) => '/media/' + m.path)[0] || null;

  const w = $('you');
  w.innerHTML = '';
  const row = el('div', 'who2');

  if (shot) {
    const img = el('img');
    img.src = shot;
    img.alt = pro.name;
    img.title = 'View full size';
    img.onclick = (e) => { e.stopPropagation(); lightbox(shot, pro.name, pro.short); };
    row.append(img);
  } else {
    const ph = el('div', 'noimg', '+');
    ph.title = 'Generate a portrait';
    ph.onclick = (e) => { e.stopPropagation(); makePortrait(pro.name, ph); };
    row.append(ph);
  }

  const txt = el('div', 'whoTxt');
  txt.append(el('div', 'nm', pro.name));
  const sub = [pro.short && pro.short !== pro.name ? pro.short : '', pro.pronouns]
    .filter(Boolean).join(' · ');
  if (sub) txt.append(el('div', 'sm', sub));
  if (pro.power_name) {
    txt.append(el('div', 'youPower',
      (pro.power_label ? pro.power_label + ': ' : '') + pro.power_name));
  }
  row.append(txt);
  row.title = pro.description || '';
  w.append(row);

  const acts = el('div', 'youActs');
  if (shot) {
    const again = el('button', 'ghost small', 'Regenerate');
    again.title = 'Replace this portrait';
    again.onclick = () => makePortrait(pro.name, again, true);
    acts.append(again);
  }
  const tags = el('button', 'ghost small', 'Appearance');
  tags.title = 'Edit the image tags this portrait is drawn from';
  tags.onclick = () => openPC();
  acts.append(tags);
  w.append(acts);
}

async function open(id) {
  S.id = id;
  render(await api('/api/session/' + id));
  show();
  refreshContext();
}

function show() {
  $('boot').classList.add('hidden');
  $('app').classList.remove('hidden');
}

/* ------------------------------------------------------------------ render */

function render(state) {
  S.state = state;
  // Drives the raw-story CSS. Set from the STORY, not from settings, so two
  // stories with different modes can be open in two tabs without fighting.
  document.body.dataset.mode = state.story?.mode || 'play';
  renderLedger();
  $('storyName').textContent = state.story?.name || '';
  $('turnLabel').textContent = 'turn ' + (state.session?.turn ?? 0);
  renderMessages(state.messages || []);
  renderYou(state);
  renderStats(state.stats || []);
  renderGoals(state.goals || []);
  renderCast(state);
  renderChapter(state);
  renderMode(state);
  if ($('castDlg').open) renderCastPane();   // keep the pane live during a turn
  renderLore();
  renderScene(state.media || []);
  if (document.activeElement !== $('notes')) $('notes').value = state.notes || '';
  renderSuggestions();
}

/* Narrative mode: nobody is playing a character, so the composer stops being a
   voice and becomes the director's channel — optional, and empty is the normal
   case. The Send button turns into Continue because advancing the story is what
   the reader actually does, several hundred times. */
function narrative() {
  return S.state?.story?.mode === 'narrative';
}

function renderMode(state) {
  const on = narrative();
  document.body.classList.toggle('narrative', on);
  $('input').placeholder = on
    ? 'Direct the story — or just press Continue'
    : 'What do you do?';
  $('send').textContent = on ? 'Continue ▸' : 'Send';

  const chip = $('actChip');
  const arc = state.arc;
  if (!on || !arc) { chip.classList.add('hidden'); return; }
  chip.classList.remove('hidden');
  chip.classList.toggle('manual', !!arc.manual);
  chip.textContent = `Act ${arc.number}/${arc.total} · ${arc.name}`;
  chip.title = arc.manual
    ? 'Held here by hand. Click to choose another act, or return it to the chapter clock.'
    : 'Following the chapter clock. Click to hold the story in a particular act.';
}

/* The reader overriding the clock. The counter knows time has passed; only the
   reader knows whether the beat actually landed. */
async function pickAct() {
  const acts = S.state?.acts || [];
  if (!acts.length) return;
  const cur = S.state?.arc;
  const lines = acts.map((a, i) => `${i + 1}. ${a.name}`).join('\n');
  const answer = prompt(
    `Which act should the story be in?\n\n${lines}\n\n`
    + 'Enter a number, or 0 to follow the chapter clock again.',
    cur ? String(cur.number) : '1');
  if (answer === null) return;
  const n = parseInt(answer, 10);
  if (Number.isNaN(n)) return;
  if (n === 0) render(await api('/api/arc', { session: S.id, clear: true }));
  else if (n >= 1 && n <= acts.length) {
    render(await api('/api/arc', { session: S.id, act: acts[n - 1].id }));
  }
  refreshContext();
}

function renderMessages(msgs) {
  const w = $('messages');
  keepingPlace(w, () => {
    w.innerHTML = '';
    msgs.forEach((m) => w.append(messageNode(m)));
  });
}

/* Render **bold** and *italics* instead of showing the asterisks.

   Italics are deliberately stricter than bold: single line only, no asterisk
   inside, and the opening marker may not be followed by a space. Models write
   *emphasis* inline, but bare asterisks also show up as bullets and as
   multiplication, and turning `3 * 4 * 5` into italics would be worse than
   leaving the markers visible.

   Built from text nodes and <strong> elements rather than innerHTML — message
   content comes from a model and from the user, and neither should ever be able
   to inject markup into the page.

   The raw text is kept on the node so editing gives you the source back rather
   than the rendered version. */
const FMT_RE = /\*\*\*([\s\S]+?)\*\*\*|\*\*([\s\S]+?)\*\*|\*(?!\s)([^*\n]+?)\*/g;

function formatInto(node, text) {
  node.dataset.raw = text;
  node.textContent = '';
  let last = 0, m;
  FMT_RE.lastIndex = 0;
  while ((m = FMT_RE.exec(text)) !== null) {
    if (m.index > last) node.append(document.createTextNode(text.slice(last, m.index)));
    // Longest marker first, so *** is not read as ** followed by a stray *.
    const [tag, cls, inner] = m[1] !== undefined ? ['strong', 'em both', m[1]]
                            : m[2] !== undefined ? ['strong', 'em', m[2]]
                                                 : ['em', 'it', m[3]];
    const node2 = document.createElement(tag);
    node2.className = cls;
    node2.textContent = inner;
    node.append(node2);
    last = m.index + m[0].length;
  }
  if (last < text.length) node.append(document.createTextNode(text.slice(last)));
}

function messageNode(m) {
  const n = el('div', 'msg ' + m.role);
  n.dataset.id = m.id;
  const who = m.role === 'user'
    ? (narrative() ? 'Direction' : 'You')
    : (S.state?.story?.name || 'Narrator');
  n.append(el('div', 'who', who));
  const body = el('div', 'body');
  formatInto(body, m.content);
  n.append(body);

  const tools = el('div', 'tools');
  const edit = el('button', null, 'edit');
  edit.onclick = () => {
    const on = body.getAttribute('contenteditable') === 'true';
    body.setAttribute('contenteditable', on ? 'false' : 'true');
    edit.textContent = on ? 'edit' : 'save';
    if (on) {
      const text = body.textContent;
      api('/api/message', { id: m.id, content: text });
      formatInto(body, text);          // back to rendered
    } else {
      body.textContent = body.dataset.raw || body.textContent;   // show the source
      body.focus();
    }
  };
  tools.append(edit);

  if (m.role === 'assistant') {
    const retry = el('button', null, 'retry');
    retry.onclick = () => send('', m.id);
    tools.append(retry);

    // A reply that hit the length limit is unfinished, not wrong, so retry is
    // the wrong tool for it — it would throw away a scene that was fine as far
    // as it got. Continue picks the same beat back up mid-sentence.
    const cont = el('button', null, 'continue');
    cont.title = 'Finish this reply from where it was cut off';
    cont.onclick = () => send('', null, m.id);
    tools.append(cont);
  }

  if (m.truncated) {
    n.classList.add('truncated');
    const flag = el('div', 'cutoff',
      'Cut off at the length limit — press continue to finish the beat.');
    body.after(flag);
  }

  const del = el('button', null, 'delete');
  del.title = 'Remove this message from the transcript';
  del.onclick = async () => {
    if (!await ask('Delete this message?')) return;
    await guard(async () => {
      render(await api('/api/message/delete', { session: S.id, id: m.id }));
      refreshContext();
    }, 'delete');
  };
  tools.append(del);

  // Rewinding to a branch point. Kept separate from plain delete because it
  // throws away everything after this point, which is not undoable.
  const cut = el('button', null, 'delete from here');
  cut.title = 'Remove this message and every message after it';
  cut.onclick = async () => {
    const at = (S.state?.messages || []).findIndex((x) => x.id === m.id);
    const rest = at < 0 ? 0 : (S.state.messages.length - at - 1);
    const ok = await ask(
      `Delete this message and the ${rest} after it?\n\n`
      + 'Everything those turns produced goes too — memories, goals, standing and '
      + 'scene art. This cannot be undone.', 'Delete from here');
    if (!ok) return;
    await guard(async () => {
      const r = await api('/api/message/delete', { session: S.id, id: m.id, after: true });
      render(r);
      refreshContext();
      const g = r.removed || {};
      $('status').textContent =
        `Rewound to turn ${g.now_turn ?? '?'} — removed ${g.messages || 0} messages, `
        + `${g.memories || 0} memories, ${g.goals || 0} goals.`;
      setTimeout(() => { $('status').textContent = ''; }, 6000);
    }, 'rewind');
  };
  tools.append(cut);

  n.append(tools);
  return n;
}

function renderStats(stats) {
  const w = $('stats');
  w.innerHTML = '';
  if (!stats.length) { w.append(el('div', 'muted small', 'No stats in this story.')); return; }
  stats.forEach((s) => {
    const row = el('div', 'stat');
    const top = el('div', 'statTop');
    top.append(el('span', null, s.name),
               el('b', null, `${(+s.value).toLocaleString()}${s.unit ? ' ' + s.unit : ''}`));
    row.append(top);
    const span = (s.max - s.min) || 1;
    const pct = Math.max(0, Math.min(100, ((s.value - s.min) / span) * 100));
    const bar = el('div', 'statBar');
    const fill = el('div');
    fill.style.width = pct + '%';
    bar.append(fill);
    row.append(bar);
    w.append(row);
  });
}

function renderGoals(goals) {
  const w = $('goals');
  w.innerHTML = '';
  const active = goals.filter((g) => g.status === 'active');
  const waiting = goals.filter((g) => g.status === 'pending');
  const quiet = S.state?.goal_quiet;

  const badge = $('goalPending');
  badge.classList.toggle('hidden', !waiting.length);
  badge.textContent = waiting.length + ' waiting';
  badge.title = 'Captured, but the story is pointed at something else. '
    + 'Promoted automatically when the current one is done.';

  if (!active.length && !waiting.length) {
    w.append(el('div', 'muted small',
      'Nothing outstanding. The story is free to wander until it commits to something.'));
    return;
  }

  // The active objective is the story's direction, so it gets the weight. Its
  // quiet counter is the honest version of what the prompt is doing: at
  // GOAL_NUDGE_AFTER turns the narrator starts being asked to raise it.
  active.forEach((g) => {
    const row = el('div', 'goal active');
    row.append(el('div', 'txt', g.text));
    if (quiet != null) {
      const q = el('div', 'quiet', quiet === 0 ? 'came up this turn' : `quiet ${quiet} turns`);
      q.title = quiet === 0
        ? 'The last turn engaged with this.'
        : 'After a few quiet turns the narrator is asked to let a character mention '
          + 'it in passing, then drop it.';
      row.append(q);
    }
    const done = el('button', null, 'done');
    done.onclick = () => setGoal(g.id, 'complete');
    const drop = el('button', null, 'drop');
    drop.onclick = () => setGoal(g.id, 'dismissed');
    row.append(done, drop);
    w.append(row);
  });

  waiting.forEach((g) => {
    const row = el('div', 'goal pending');
    row.append(el('div', 'txt', g.text));
    const up = el('button', null, 'now');
    up.title = 'Make this the objective instead';
    up.onclick = () => setGoal(g.id, 'active');
    const no = el('button', null, 'drop');
    no.onclick = () => setGoal(g.id, 'dismissed');
    row.append(up, no);
    w.append(row);
  });
}

async function setGoal(id, status) {
  render(await api('/api/goal', { id, status, session: S.id }));
}

/* One list of who exists, built once and used by both the side panel and the
   expanded pane, so the two can never disagree about who is on stage. */
function castRoster(state) {
  const portraits = {};
  (state.media || []).filter((m) => m.kind === 'portrait')
    .forEach((m) => { if (!portraits[m.subject]) portraits[m.subject] = m.path; });

  const rels = Object.fromEntries((state.relationships || []).map((r) => [r.name, r.summary]));
  const shorts = Object.fromEntries((state.cast || []).map((c) => [c.name, c.short]));

  return [...new Set([...Object.keys(rels), ...Object.keys(portraits)])].map((name) => ({
    name,
    portrait: portraits[name] ? '/media/' + portraits[name] : null,
    summary: rels[name] || '',
    short: shorts[name] || '',
  }));
}

/* Rendering takes 10-30s on the 4070, so a fixed 4s refresh usually landed before
   the image existed and looked like the click had done nothing. Poll instead, and
   keep the button showing that work is happening. */
async function makePortrait(name, node, replace) {
  const label = node.textContent;
  node.textContent = replace ? 'rendering…' : '…';
  if (node.disabled !== undefined) node.disabled = true;

  const had = (S.state?.media || [])
    .filter((m) => m.kind === 'portrait' && m.subject === name)
    .map((m) => m.path)[0] || null;

  try {
    await api('/api/portrait', { session: S.id, name, replace: !!replace });
  } catch (e) {
    node.textContent = 'failed';
    setTimeout(() => { node.textContent = label; node.disabled = false; }, 2500);
    return;
  }

  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 2500));
    let st;
    try { st = await api('/api/session/' + S.id); } catch { continue; }
    const now = (st.media || [])
      .filter((m) => m.kind === 'portrait' && m.subject === name)
      .map((m) => m.path)[0] || null;
    if (now && now !== had) {
      render(st);
      return;
    }
    const q = st.images || {};
    if (!q.queued && !q.rendering) break;   // worker went idle without producing one
  }
  node.textContent = label;
  if (node.disabled !== undefined) node.disabled = false;
  $('status').textContent = 'portrait did not render — check ComfyUI is reachable.';
  setTimeout(() => { $('status').textContent = ''; }, 5000);
}

function renderCast(state) {
  const w = $('cast');
  w.innerHTML = '';
  const roster = castRoster(state);
  if (!roster.length) { w.append(el('div', 'muted small', 'Nobody yet.')); return; }

  roster.forEach((p) => {
    const row = el('div', 'who2');
    row.title = 'Open ' + p.name;

    if (p.portrait) {
      const img = el('img');
      img.src = p.portrait;
      img.alt = p.name;
      row.append(img);
    } else {
      const ph = el('div', 'noimg', '+');
      ph.title = 'Generate a portrait';
      // Generating is its own action — don't also open the pane.
      ph.onclick = (e) => { e.stopPropagation(); makePortrait(p.name, ph); };
      row.append(ph);
    }

    const txt = el('div', 'whoTxt');
    txt.append(el('div', 'nm', p.name));
    if (p.summary) txt.append(el('div', 'sm', p.summary));
    row.append(txt);
    row.onclick = () => openCast(p.name);
    w.append(row);
  });
}


/* ------------------------------------------------------------- portrait builder

   Typing booru tags is a skill nobody should need to play a story. These produce a
   usable prompt from four choices, and Randomise fills in whatever is left blank —
   good enough for an innkeeper, and always better than letting the checkpoint
   invent someone from a bare name. */

const PORTRAIT_OPTS = {
  Gender: [
    ['man',         '1boy, solo, male'],
    ['woman',       '1girl, solo, female'],
    ['androgynous', '1other, solo, androgynous face'],
  ],
  Age: [
    ['young',       'young adult, smooth face'],
    ['adult',       'adult, early thirties'],
    ['middle-aged', 'middle aged, faint lines, some grey'],
    ['older',       'older, grey hair, lined face'],
    ['elderly',     'elderly, white hair, deeply lined face'],
  ],
  Mood: [
    ['warm',      'warm friendly expression, soft eyes, slight smile'],
    ['stern',     'stern expression, hard eyes, set jaw'],
    ['weary',     'tired eyes, weary expression, shadows under eyes'],
    ['sly',       'sly smirk, knowing eyes, one raised eyebrow'],
    ['cheerful',  'bright cheerful smile, lively eyes'],
    ['cold',      'cold flat expression, distant eyes'],
    ['nervous',   'nervous expression, darting eyes, tense mouth'],
    ['imposing',  'imposing presence, heavy brow, broad shoulders'],
  ],
  Dress: [
    ['commoner',  'plain worn linen, simple clothes'],
    ['official',  'guild robes, neat collar, ink-stained fingers'],
    ['soldier',   'worn leather armor, buckles and straps'],
    ['merchant',  'well-made coat, fur trim, rings'],
    ['scholar',   'scholar robes, spectacles'],
    ['labourer',  'rough tunic, dirt on the hands, thick forearms'],
    ['innkeeper', 'apron over a linen shirt, sleeves rolled'],
  ],
};

// Styles, not colours. The age tags already imply colour ("older, grey hair"), so a
// random colour here produced "older, grey hair ... red hair" and the model split the
// difference. Styles compose with any age.
const PORTRAIT_EXTRA = [
  'close-cropped hair', 'hair tied back', 'braided hair', 'unkempt hair',
  'shoulder-length hair', 'hair in a bun', 'short neat hair', 'stubble',
];

function portraitBuilder(onCompose) {
  const wrap = el('div', 'pbuild');
  const picks = {};

  Object.entries(PORTRAIT_OPTS).forEach(([label, opts]) => {
    const cell = el('label', 'pbCell');
    cell.append(el('span', null, label));
    const sel = document.createElement('select');
    const blank = document.createElement('option');
    blank.value = ''; blank.textContent = '—';
    sel.append(blank);
    opts.forEach(([name, tags]) => {
      const o = document.createElement('option');
      o.value = tags; o.textContent = name;
      sel.append(o);
    });
    picks[label] = sel;
    cell.append(sel);
    wrap.append(cell);
  });

  const compose = (fill) => {
    const parts = [];
    Object.entries(PORTRAIT_OPTS).forEach(([label, opts]) => {
      let v = picks[label].value;
      if (!v && fill) {
        const pick = opts[Math.floor(Math.random() * opts.length)];
        picks[label].value = pick[1];
        v = pick[1];
      }
      if (v) parts.push(v);
    });
    if (fill) parts.push(PORTRAIT_EXTRA[Math.floor(Math.random() * PORTRAIT_EXTRA.length)]);
    onCompose(parts.join(', '));
  };

  const row = el('div', 'pbRow');
  const apply = el('button', 'ghost small', 'Use these');
  apply.onclick = () => compose(false);
  const rand = el('button', 'ghost small', '🎲 Randomise');
  rand.title = 'Fill anything left blank at random';
  rand.onclick = () => compose(true);
  row.append(apply, rand);
  wrap.append(row);
  return wrap;
}

/* ------------------------------------------------------------------ cast pane */

function openCast(name) {
  CAST.selected = name || null;
  renderCastPane();
  if (!$('castDlg').open) $('castDlg').showModal();
}

const CAST = { selected: null };

function renderCastPane() {
  const roster = castRoster(S.state || {});
  if (!roster.length) return;
  if (!roster.some((p) => p.name === CAST.selected)) CAST.selected = roster[0].name;
  const who = roster.find((p) => p.name === CAST.selected);

  const list = $('castList');
  list.innerHTML = '';
  roster.forEach((p) => {
    const t = el('div', 'castTab' + (p.name === CAST.selected ? ' on' : ''));
    if (p.portrait) {
      const img = el('img');
      img.src = p.portrait;
      img.alt = '';
      t.append(img);
    } else {
      t.append(el('div', 'noimg', '?'));
    }
    t.append(el('div', 'nm', p.name));
    t.onclick = () => { CAST.selected = p.name; renderCastPane(); };
    list.append(t);
  });

  const d = $('castDetail');
  d.innerHTML = '';
  const media = el('div', 'castMedia');
  if (who.portrait) {
    const img = el('img');
    img.src = who.portrait;
    img.alt = who.name;
    img.title = 'View full size';
    img.onclick = () => lightbox(who.portrait, who.name, who.short);
    media.append(img);
    const again = el('button', 'ghost small', 'Regenerate');
    again.title = 'Replace this portrait';
    again.onclick = () => makePortrait(who.name, again, true);
    media.append(again);
  } else {
    const ph = el('div', 'noimg big', '+');
    ph.title = 'Generate a portrait';
    ph.onclick = () => makePortrait(who.name, ph);
    media.append(ph);
  }
  d.append(media);

  const body = el('div', 'castText');
  body.append(el('h3', null, who.name));
  if (who.short) body.append(el('div', 'castShort', who.short));

  // Characters the story does not define have no description anywhere, so the
  // checkpoint invents one and regenerating only rolls the dice again. Let the
  // player supply the description instead.
  if (!who.short) {
    const cur = (S.state?.portrait_prompts || {})[who.name] || '';
    body.append(el('div', 'castLabel', 'Appearance'));
    const desc = document.createElement('textarea');
    desc.rows = 3;
    desc.className = 'castDesc';
    desc.value = cur;
    desc.placeholder =
      '1boy, older man, grey beard, guild robes, tired eyes\n\n'
      + 'Image tags, not prose. Without this the portrait is a guess and '
      + 'regenerating just produces a different guess.';
    desc.onchange = async () => {
      await api('/api/portrait/describe',
                { session: S.id, name: who.name, text: desc.value });
      S.state.portrait_prompts = S.state.portrait_prompts || {};
      S.state.portrait_prompts[who.name] = desc.value;
      $('status').textContent = 'description saved — press Regenerate to use it.';
      setTimeout(() => { $('status').textContent = ''; }, 4000);
    };
    body.append(portraitBuilder((tags) => {
      desc.value = tags;
      desc.dispatchEvent(new Event('change'));
    }));
    body.append(desc);
  }
  body.append(el('div', 'castLabel', 'How they see you'));
  body.append(el('div', 'castSummary',
    who.summary || 'Nothing recorded yet — this develops as you interact.'));
  d.append(body);
}

/* Full-size image viewer. Scene art is letterboxed in the banner so nothing is
   cropped, but it is still small — this is how you actually look at it. */
function lightbox(src, caption, sub) {
  $('lightboxImg').src = src;
  $('lightboxImg').alt = caption || '';
  const cap = $('lightboxCap');
  cap.innerHTML = '';
  if (caption) cap.append(el('b', null, caption));
  if (sub) cap.append(el('div', 'muted small', sub));
  $('lightbox').showModal();
}

/* ------------------------------------------------------------------ lorebook

   What replaced the memory panel. Memory still runs underneath — heat, decay,
   promotion, embedding retrieval, all of it — but it is machinery, and a list of
   537 rows was not something anyone could usefully act on.

   A lorebook entry is different in kind: few, written once, and fired by a
   keyword rather than by a similarity search, so "mention the Diamond Dogs and
   the Diamond Dogs entry is in the prompt" is a guarantee rather than a hope.
   Suggestions are drafted at a chapter break and are inert until accepted. */

function renderLore() {
  const st = S.state || {};
  const entries = st.lorebook || [];
  const sugg = st.lore_suggestions || [];

  const badge = $('loreNew');
  badge.classList.toggle('hidden', !sugg.length);
  badge.textContent = sugg.length + ' suggested';

  const m = st.memory_counts || {};
  $('loreCounts').textContent = entries.length ? `${entries.length} entries` : '';
  $('loreCounts').title =
    `Working memory underneath: ${m.temp || 0} recent, ${m.long || 0} long-term. `
    + 'Managed automatically — the lorebook is the part worth curating.';

  const w = $('loreSuggest');
  w.innerHTML = '';
  sugg.forEach((e) => {
    const row = el('div', 'loreSug');
    row.append(el('div', 'lt', e.title));
    row.append(el('div', 'lb', e.body));
    row.append(el('div', 'lk', e.keywords.join(' · ')));
    const acts = el('div', 'lacts');
    const yes = el('button', 'primary small', 'Add');
    yes.onclick = async () =>
      render(await api('/api/lore', { session: S.id, id: e.id, status: 'active' }));
    const edit = el('button', 'ghost small', 'Edit');
    edit.onclick = () => openLore(e);
    // Dismissed rather than deleted: a dismissal is remembered, so the same
    // entry is not proposed again at every chapter for the rest of the story.
    const no = el('button', 'ghost small', 'No');
    no.title = 'Never suggest this again';
    no.onclick = async () =>
      render(await api('/api/lore', { session: S.id, id: e.id, status: 'dismissed' }));
    acts.append(yes, edit, no);
    row.append(acts);
    w.append(row);
  });

  const l = $('lore');
  l.innerHTML = '';
  if (!entries.length) {
    l.append(el('div', 'muted small',
      'Empty. Entries are suggested when a chapter is compacted, or add one yourself.'));
    return;
  }
  entries.forEach((e) => {
    const row = el('div', 'loreRow' + (e.always ? ' always' : ''));
    row.append(el('div', 'lt', e.title));
    row.append(el('div', 'lk', e.always ? 'always on' : e.keywords.join(' · ')));
    row.title = e.body;
    row.onclick = () => openLore(e);
    l.append(row);
  });
}

/* One dialog for creating, editing and accepting-with-changes, because they are
   the same form and the third case is the common one. */
function openLore(e) {
  const isNew = !e;
  e = e || { title: '', body: '', keywords: [], always: false };
  const body = $('loreBody');
  body.innerHTML = '';
  $('loreDlgTitle').textContent = isNew ? 'New lorebook entry'
    : (e.status === 'suggested' ? 'Suggested entry' : 'Lorebook entry');
  $('loreStatus').textContent = '';

  body.append(el('h4', null, 'Title'));
  const title = el('input');
  title.type = 'text';
  title.value = e.title || '';
  body.append(title);

  body.append(el('h4', null, 'Entry'));
  body.append(el('div', 'lede',
    'Established fact, present tense. This is injected verbatim whenever a keyword '
    + 'below appears in recent play, so keep it short and keep it true.'));
  const text = document.createElement('textarea');
  text.rows = 5;
  text.value = e.body || '';
  body.append(text);

  body.append(el('h4', null, 'Keywords'));
  body.append(el('div', 'lede',
    'Comma separated. A keyword that fires every turn — "man", "city" — spends the '
    + 'lore allowance permanently and pushes out entries that were actually relevant.'));
  const kw = el('input');
  kw.type = 'text';
  kw.value = (e.keywords || []).join(', ');
  body.append(kw);

  const alwaysRow = el('label', 'loreAlways');
  const always = el('input');
  always.type = 'checkbox';
  always.checked = !!e.always;
  alwaysRow.append(always, el('span', null,
    'Always on — in every prompt, no keyword needed'));
  body.append(alwaysRow);

  $('loreDelete').classList.toggle('hidden', isNew);
  $('loreDelete').onclick = async () => {
    if (!await ask('Delete this entry?')) return;
    render(await api('/api/lore', { session: S.id, delete: e.id }));
    $('loreDlg').close();
  };

  $('loreSave').textContent = e.status === 'suggested' ? 'Add to lorebook' : 'Save';
  $('loreSave').onclick = async () => {
    const payload = {
      session: S.id,
      title: title.value,
      body: text.value,
      keywords: kw.value.split(',').map((k) => k.trim()).filter(Boolean),
      always: always.checked,
    };
    if (!payload.title.trim() || !payload.body.trim()) {
      $('loreStatus').textContent = 'needs a title and an entry';
      return;
    }
    if (!isNew) { payload.id = e.id; payload.status = 'active'; }
    render(await api('/api/lore', payload));
    $('loreDlg').close();
    refreshContext();
  };
  $('loreDlg').showModal();
}

/* Scene art is the single biggest thing competing with the prose for screen, and
   on a phone that matters more than it does on a desktop. Collapsed state is
   remembered across sessions — being asked to re-collapse it every turn would be
   worse than not having the control. */
const SCENE_KEY = 'loom.scene.open';
const sceneOpen = () => localStorage.getItem(SCENE_KEY) !== '0';

function setScene(open) {
  localStorage.setItem(SCENE_KEY, open ? '1' : '0');
  paintScene();
}

function paintScene() {
  const box = $('scene');
  const open = sceneOpen();
  box.classList.toggle('collapsed', !open);
  // Collapsed, the strip is the only handle left, so it says what it is rather
  // than being a bare chevron.
  $('sceneToggle').textContent = open ? '▾  Hide scene' : '▸  Show scene';
  $('sceneToggle').title = open ? 'Collapse the scene image' : 'Show the scene image';
  if (open) box.classList.remove('fresh');
}

function renderScene(media) {
  const scene = media.find((m) => m.kind === 'scene');
  const box = $('scene');
  if (!scene) { box.classList.add('hidden'); return; }

  const src = '/media/' + scene.path;
  if ($('sceneImg').getAttribute('src') !== src) {
    $('sceneImg').src = src;
    // New art arriving while collapsed must not pop the panel open — but you
    // should be able to tell there is something new to look at.
    if (!sceneOpen() && S.lastScene) box.classList.add('fresh');
    S.lastScene = src;
  }
  $('sceneImg').onclick = () => lightbox(src, scene.subject || 'Scene');
  box.classList.remove('hidden');
  paintScene();
}

function renderSuggestions() {
  const w = $('suggestions');
  w.innerHTML = '';
  const msgs = S.state?.messages || [];
  if (msgs.length !== 1) return;   // only on the opening beat
  (S.state?.suggestions || window.__suggestions || []).forEach((t) => {
    const b = el('button', null, t);
    b.onclick = () => { $('input').value = t; w.innerHTML = ''; $('input').focus(); };
    w.append(b);
  });
}

/* ------------------------------------------------------------------ context */

function renderContext(c) {
  if (!c) return;
  const pct = Math.round((c.fill || 0) * 100);
  $('ctxFill').style.width = Math.min(100, pct) + '%';
  $('ctxLabel').textContent = `${pct}%`;
  $('ctxAdvice').textContent = c.advice || '';

  const w = $('ctxLayers');
  w.innerHTML = '';
  (c.layers || []).forEach((l) => {
    const row = el('div', 'layer' + (l.dropped ? ' drop' : ''));
    row.append(el('div', 'nm', l.layer.replace(/_/g, ' ')));
    const bar = el('div', 'bar');
    const fill = el('div');
    fill.style.width = Math.min(100, (l.used / Math.max(1, c.budget)) * 100 * 4) + '%';
    bar.append(fill);
    row.append(bar);
    row.append(el('div', 'n', l.used > 999 ? (l.used / 1000).toFixed(1) + 'k' : String(l.used)));
    row.title = `${l.layer}: used ${l.used} of ${l.allocated} allocated ` +
                `(wanted ${l.wanted}, floor ${l.floor}, ceiling ${l.ceiling})` +
                (l.dropped ? ` — ${l.dropped} item(s) dropped` : '');
    w.append(row);
  });
}

async function refreshContext() {
  try { renderContext(await api('/api/context/' + S.id)); } catch (e) { /* non-fatal */ }
}

async function refreshState() {
  render(await api('/api/session/' + S.id));
}

/* ------------------------------------------------------------------ turn */

async function send(text, retryId, resumeId) {
  if (S.busy) return;
  S.busy = true;
  S.gotMessage = false;
  S.gotError = false;
  S.sawToken = false;
  $('send').disabled = true;
  $('suggestions').innerHTML = '';
  $('status').textContent = 'thinking…';
  S.turnStart = Date.now();

  if (text) {
    S.state.messages.push({ id: 'tmp', role: 'user', content: text });
    $('messages').append(messageNode({ id: 'tmp', role: 'user', content: text }));
  }
  if (retryId) {
    const node = document.querySelector(`.msg[data-id="${retryId}"]`);
    if (node) node.remove();
  }

  // A resume streams INTO the message it is finishing rather than starting a new
  // bubble. Appending to a fresh one would show the same beat as two replies,
  // which is exactly the split the server is joining back together.
  let live, body, raw = '';
  const resuming = resumeId != null;
  if (resuming) {
    live = document.querySelector(`.msg[data-id="${resumeId}"]`);
    if (!live) { S.busy = false; $('send').disabled = false; $('status').textContent = ''; return; }
    body = live.querySelector('.body');
    raw = body.dataset.raw ?? body.textContent;
    live.classList.remove('truncated');
    live.querySelector('.cutoff')?.remove();
  } else {
    live = el('div', 'msg assistant');
    live.append(el('div', 'who', S.state?.story?.name || 'Narrator'));
    body = el('div', 'body', '');
    live.append(body);
    $('messages').append(live);
  }
  // Sending is an explicit act, so it always jumps to the bottom. What follows
  // only tracks if the reader stays there.
  toBottom($('messages'));
  updateLatestButton();

  const url = resuming ? '/api/continue' : (retryId ? '/api/retry' : '/api/send');
  const payload = resuming
    ? { session: S.id, message: resumeId }
    : (retryId ? { session: S.id, message: retryId } : { session: S.id, text });

  try {
    const resp = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (!resuming) live.remove();
      // Never lose what they typed to a failed send.
      if (text) $('input').value = text;
      S.state.messages = (S.state.messages || []).filter((m) => m.id !== 'tmp');
      renderMessages(S.state.messages);
      $('status').textContent = 'error: ' + (err.error || resp.statusText);
      return;
    }

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let ix;
      while ((ix = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, ix);
        buf = buf.slice(ix + 2);
        const ev = /event: (.+)/.exec(chunk)?.[1];
        const dm = /data: ([\s\S]*)$/.exec(chunk)?.[1];
        if (!ev || dm == null) continue;
        let data;
        try { data = JSON.parse(dm); } catch { continue; }

        if (ev === 'token') {
          if (!S.sawToken) { S.sawToken = true; $('status').textContent = ''; }
          const box = $('messages');
          const stick = atBottom(box);
          raw += data;
          // Re-render only when an asterisk is involved; otherwise just append,
          // so a long reply is not rebuilt hundreds of times.
          if (data.includes('*')) formatInto(body, raw);
          else { body.append(document.createTextNode(data)); body.dataset.raw = raw; }
          if (stick) toBottom(box); else updateLatestButton();
        }
        else if (ev === 'context') { renderContext(data); }
        else if (ev === 'retrying') {
          $('status').textContent =
            `${data.reason} — retrying in ${Math.round(data.delay)}s ` +
            `(${data.attempt}/${data.of})`;
        }
        else if (ev === 'thinking') {
          // Reasoning models go quiet for a long time before the first word.
          const secs = Math.round((Date.now() - S.turnStart) / 1000);
          $('status').textContent =
            `reasoning… ${secs}s (${data.chars.toLocaleString()} chars of thought)`;
        }
        else if (ev === 'message') {
          live.dataset.id = data.id;
          S.gotMessage = true;
          // A resume returns the whole merged reply, so re-render from that
          // rather than trusting the tokens we happened to append — the join is
          // the server's, and this is the version that is actually on disk.
          if (data.replaced) { raw = data.content; formatInto(body, raw); body.dataset.raw = raw; }
        }
        else if (ev === 'chapter') { chapterClosed(data); }
        else if (ev === 'state') {
          render(data);
          const a = data.applied || {};
          const bits = [];
          if (a.relationships) bits.push(`${a.relationships} relationships`);
          if (a.goals_added) bits.push(a.goals_added > 1 ? `${a.goals_added} goals` : 'new goal');
          if (a.goals_completed) bits.push('goal done');
          if (a.scene_changed) bits.push('new scene');
          $('status').textContent = bits.length ? 'updated: ' + bits.join(', ') : '';
        }
        else if (ev === 'warn') { $('status').textContent = data.message; }
        else if (ev === 'error') {
          // A reported error is not a dropped connection — recovery must not run
          // and tell the reader to keep waiting for a turn that already failed.
          S.gotError = true;
          $('status').textContent = 'error: ' + data.message;
        }
      }
    }
    if (!S.gotMessage && !S.gotError) await recoverTurn(live, null, resumeId);
    if (S.gotError && !resuming) live.remove();
  } catch (e) {
    // A dropped stream is not a failed turn. The server finishes generating and
    // stores the reply either way, so the honest move is to go and look rather
    // than report an error the user cannot act on.
    if (!S.gotMessage && !S.gotError) await recoverTurn(live, e, resumeId);
    else $('status').textContent = 'error: ' + e.message;
  } finally {
    S.busy = false;
    $('send').disabled = false;
    refreshContext();
  }
}

/* The stream ended without ever delivering the reply — backgrounded tab, sleeping
   phone, network blip. The turn is almost certainly still running or already
   finished on the server, so poll for it instead of leaving a dead half-message
   on screen. */
async function recoverTurn(liveNode, err, resumeId) {
  const before = (S.state?.messages || []).length;
  // A resume adds no message, so counting them would wait out the full three
  // minutes and then report a failure for a turn that had already landed. What
  // grows instead is the message being finished.
  const growing = resumeId != null
    ? ((S.state?.messages || []).find((m) => m.id === resumeId)?.content || '').length
    : 0;
  $('status').textContent = 'connection dropped — the turn is still running, waiting for it…';

  for (let i = 0; i < 40; i++) {          // up to ~3 min, matching generation time
    await new Promise((r) => setTimeout(r, i < 10 ? 2000 : 6000));
    let st;
    try { st = await api('/api/session/' + S.id); } catch { continue; }
    const last = st.messages?.[st.messages.length - 1];
    const done = resumeId != null
      ? ((st.messages || []).find((m) => m.id === resumeId)?.content || '').length > growing
      : (st.messages.length > before && last?.role === 'assistant');
    if (done) {
      if (resumeId == null) liveNode.remove();
      render(st);
      $('status').textContent = 'recovered — the turn completed on the server.';
      setTimeout(() => { $('status').textContent = ''; }, 4000);
      return;
    }
  }
  $('status').textContent = err
    ? 'error: ' + err.message
    : 'the turn did not complete — reload to check, or send again.';
}

/* ------------------------------------------------------------------ settings */

/* Pending edits are held here and only sent on Save. The knobs interact — a
   budget change is meaningless until the layer floors agree with it — so the
   server applies a batch all-or-nothing, and the UI mirrors that. */
const SET = { data: null, group: null, pending: {}, from: 'boot', warnings: [] };

async function openSettings(from) {
  SET.from = from;
  SET.pending = {};
  SET.data = await api('/api/settings');
  SET.group = SET.group && SET.data.groups.includes(SET.group)
    ? SET.group : SET.data.groups[0];
  $('boot').classList.add('hidden');
  $('app').classList.add('hidden');
  $('settings').classList.remove('hidden');
  renderSettings();
}

function closeSettings() {
  $('settings').classList.add('hidden');
  if (SET.from === 'app') { $('app').classList.remove('hidden'); refreshContext(); }
  else { $('boot').classList.remove('hidden'); boot(); }
}

const knobValue = (k) => (k.key in SET.pending ? SET.pending[k.key] : k.value);
const sameValue = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function stageKnob(key, value) {
  const k = SET.data.knobs.find((x) => x.key === key);
  if (sameValue(value, k.value)) delete SET.pending[key];
  else SET.pending[key] = value;
  renderSettings();
}

function renderSettings() {
  const dirty = Object.keys(SET.pending).length;
  $('setSave').disabled = !dirty;
  $('setSave').textContent = dirty ? `Save ${dirty} change${dirty > 1 ? 's' : ''}` : 'Save';

  const nav = $('setNav');
  nav.innerHTML = '';
  SET.data.groups.forEach((g) => {
    const n = SET.data.knobs.filter((k) => k.group === g && k.key in SET.pending).length;
    const b = el('button', g === SET.group ? 'on' : null, g);
    if (n) b.append(el('span', 'dot', '•'));
    b.onclick = () => { SET.group = g; renderSettings(); };
    nav.append(b);
  });

  const body = $('setBody');
  body.innerHTML = '';
  const wrap = el('div', 'setGroup');
  wrap.append(el('h2', null, SET.group));

  if (SET.group === 'Models') {
    wrap.append(probePanel());
    // A provider with no key is the single most likely reason a swap silently
    // fails, so say it here rather than at the first failed turn.
    const missing = Object.entries(SET.data.keys || {})
      .filter(([, has]) => !has).map(([p]) => p);
    const used = new Set([
      knobValue(SET.data.knobs.find((k) => k.key === 'PROSE.provider')),
      knobValue(SET.data.knobs.find((k) => k.key === 'UTILITY.provider')),
    ]);
    const blocking = missing.filter((p) => used.has(p));
    if (blocking.length) {
      wrap.append(el('div', 'setNote bad',
        `No API key for: ${blocking.join(', ')}. Turns will fail until it is set. ` +
        'Keys are environment variables, not settings — add them to loom.env and restart.'));
    }
    if (missing.length && !blocking.length) {
      wrap.append(el('div', 'setNote',
        `No API key configured for: ${missing.join(', ')}. ` +
        'Switching a tier to one of those needs the key set in loom.env first.'));
    }
  }

  if (SET.warnings?.length) {
    const box = el('div', 'setNote bad');
    box.append(el('b', null, 'Saved, with warnings'));
    SET.warnings.forEach((wn) => box.append(el('div', null, wn)));
    wrap.append(box);
  }

  SET.data.knobs.filter((k) => k.group === SET.group)
    .forEach((k) => wrap.append(knobNode(k)));
  body.append(wrap);
}

function knobNode(k) {
  const v = knobValue(k);
  const box = el('div', 'knob');
  if (k.key in SET.pending) box.classList.add('dirty');
  if (k.overridden && !(k.key in SET.pending)) box.classList.add('over');

  const top = el('div', 'knobTop');
  const label = el('label', null, k.label);
  top.append(label);

  if (k.type === 'bool') {
    const c = el('input');
    c.type = 'checkbox';
    c.checked = !!v;
    c.onchange = () => stageKnob(k.key, c.checked);
    top.append(c);
  } else if (k.type === 'choice') {
    const s = document.createElement('select');
    k.choices.forEach((o) => {
      const opt = document.createElement('option');
      opt.value = opt.textContent = o;
      if (o === v) opt.selected = true;
      s.append(opt);
    });
    s.onchange = () => stageKnob(k.key, s.value);
    top.append(s);
  } else if (k.type === 'str') {
    const i = el('input');
    i.type = 'text';
    i.value = v ?? '';
    i.onchange = () => stageKnob(k.key, i.value);
    top.append(i);
  } else if (k.type === 'model') {
    top.append(modelPicker(k, v));
  } else if (k.type === 'int' || k.type === 'float') {
    const num = el('input');
    num.type = 'number';
    num.value = v;
    num.min = k.min; num.max = k.max; num.step = k.step ?? 1;
    const rng = el('input');
    rng.type = 'range';
    rng.value = v;
    rng.min = k.min; rng.max = k.max; rng.step = k.step ?? 1;
    // The slider is for feel, the box for precision. Mirror while dragging so
    // the number is readable, but only stage on release/commit.
    rng.oninput = () => { num.value = rng.value; };
    rng.onchange = () => stageKnob(k.key, Number(rng.value));
    num.onchange = () => stageKnob(k.key, Number(num.value));
    top.append(rng, num);
  }

  if (!(k.type === 'text' || k.type === 'layers')) {
    const rst = el('button', 'reset', 'default');
    rst.title = 'Default: ' + JSON.stringify(k.default);
    rst.onclick = () => stageKnob(k.key, k.default);
    top.append(rst);
  }
  box.append(top);

  if (k.type === 'text') {
    const t = document.createElement('textarea');
    t.rows = 3;
    t.value = v ?? '';
    t.onchange = () => stageKnob(k.key, t.value);
    box.append(t);
  } else if (k.type === 'layers') {
    box.append(layerTable(k, v));
  }

  if (k.help) box.append(el('div', 'help', k.help));
  return box;
}

/* Model names come from the provider's own listing, but the control is
   deliberately not a closed dropdown. What a provider lists and what actually
   serves need not match — ollama tags drift from what a Modelfile registers —
   and a strict select would make a working model unselectable. The menu is a
   convenience; typing an unlisted name stays legal and only earns a warning. */
const MODELS = {};

/* Live availability. A model listing says what exists, not what is answering —
   during the Opus 5 incident every model was still listed and every call to it
   returned 529. Only a real request tells them apart. */
const PROBE = { rows: null, provider: null, busy: false, when: null };

function probePanel() {
  const box = el('div', 'setNote probeBox');
  const head = el('div', 'probeHead');
  head.append(el('b', null, 'Model availability'));
  head.append(el('div', 'grow'));

  const btn = el('button', 'ghost small', PROBE.busy ? 'checking…' : 'Check now');
  btn.disabled = PROBE.busy;
  btn.onclick = async () => {
    const provider = knobValue(SET.data.knobs.find((x) => x.key === 'PROSE.provider'));
    PROBE.busy = true; PROBE.provider = provider; renderSettings();
    try {
      const r = await api('/api/probe?provider=' + encodeURIComponent(provider));
      PROBE.rows = r.results;
      PROBE.inUse = r.in_use;
      PROBE.when = new Date().toLocaleTimeString();
    } catch (e) {
      PROBE.rows = [{ model: '—', state: 'unusable', ms: 0, error: e.message }];
    }
    PROBE.busy = false;
    renderSettings();
  };
  head.append(btn);
  box.append(head);

  if (!PROBE.rows) {
    box.append(el('div', 'probeHint',
      'Sends one tiny request per model and reports what came back. ' +
      'A listing says what exists; only a real request says what is answering.'));
    return box;
  }

  const order = { busy: 0, unusable: 1, up: 2 };
  const rows = [...PROBE.rows].sort((a, b) =>
    (order[a.state] - order[b.state]) || a.model.localeCompare(b.model));

  const t = document.createElement('table');
  t.className = 'probeTable';
  rows.forEach((r) => {
    const tr = t.insertRow();
    const st = tr.insertCell();
    st.className = 'st ' + r.state;
    st.textContent = r.state === 'up' ? '● up'
                   : r.state === 'busy' ? '● busy' : '● unusable';
    const nm = tr.insertCell();
    nm.className = 'nm';
    nm.textContent = r.model;
    const inUse = PROBE.inUse &&
      (r.model === PROBE.inUse.prose || r.model === PROBE.inUse.utility);
    if (inUse) {
      const tag = el('span', 'inuse',
        r.model === PROBE.inUse.prose ? 'prose' : 'utility');
      nm.append(tag);
    }
    const ms = tr.insertCell();
    ms.className = 'ms';
    ms.textContent = r.ok ? r.ms.toLocaleString() + 'ms' : (r.code || '—');
    const why = tr.insertCell();
    why.className = 'why';
    why.textContent = r.state === 'busy'
      ? 'overloaded — try again shortly'
      : r.state === 'unusable' ? 'rejects this request shape' : '';
    if (r.error) why.title = r.error;
  });
  box.append(t);
  box.append(el('div', 'probeHint',
    `Checked ${PROBE.when}. "busy" is temporary — retries usually ride it out. ` +
    '"unusable" means switching to it will not help.'));
  return box;
}

function modelPicker(k, value) {
  const wrap = el('div', 'modelPick');
  const provider = knobValue(SET.data.knobs.find((x) => x.key === k.provider_from));

  const sel = document.createElement('select');
  const custom = el('input');
  custom.type = 'text';
  custom.value = value ?? '';
  custom.placeholder = 'model id';

  const fill = (list, err) => {
    sel.innerHTML = '';
    const known = list.includes(value);
    list.forEach((m) => {
      const o = document.createElement('option');
      o.value = o.textContent = m;
      if (m === value) o.selected = true;
      sel.append(o);
    });
    const other = document.createElement('option');
    other.value = '__custom__';
    other.textContent = list.length ? 'Other…' : 'Type a model id…';
    if (!known) other.selected = true;
    sel.append(other);

    custom.classList.toggle('hidden', known);
    const note = el('div', 'hint');
    if (err) {
      note.textContent = `Could not list ${provider} models (${err}). Type the id by hand.`;
      note.className = 'hint warn';
    } else if (!known) {
      note.textContent = `'${value}' is not in ${provider}'s list of ${list.length}. ` +
        'That is allowed — some working ids are aliases the listing omits.';
      note.className = 'hint warn';
    } else {
      note.textContent = `${list.length} models offered by ${provider}.`;
    }
    wrap.querySelector('.hint')?.remove();
    wrap.append(note);
  };

  sel.onchange = () => {
    if (sel.value === '__custom__') { custom.classList.remove('hidden'); custom.focus(); }
    else { custom.value = sel.value; stageKnob(k.key, sel.value); }
  };
  custom.onchange = () => stageKnob(k.key, custom.value.trim());

  wrap.append(sel, custom);

  const cached = MODELS[provider];
  if (cached) fill(cached.models, cached.error);
  else {
    fill([], null);
    api('/api/models?provider=' + encodeURIComponent(provider)).then((r) => {
      MODELS[provider] = r;
      fill(r.models, r.error);
    }).catch((e) => fill([], e.message));
  }
  return wrap;
}

function layerTable(k, rows) {
  const t = document.createElement('table');
  t.className = 'layerTable';
  const head = t.insertRow();
  ['Layer', 'Floor', 'Ceiling', ''].forEach((h, i) => {
    const th = document.createElement('th');
    th.textContent = h;
    if (i === 1 || i === 2) th.className = 'n';
    head.append(th);
  });

  const commit = (next) => stageKnob(k.key, next);

  rows.forEach((row, i) => {
    const [name, floor, ceil] = row;
    const tr = t.insertRow();
    tr.insertCell().textContent = name;

    [1, 2].forEach((col) => {
      const td = tr.insertCell();
      td.className = 'n';
      const inp = el('input');
      inp.type = 'number';
      inp.min = 0; inp.step = 100;
      inp.value = row[col];
      inp.onchange = () => {
        const next = rows.map((r) => r.slice());
        next[i][col] = Number(inp.value);
        commit(next);
      };
      td.append(inp);
    });

    const td = tr.insertCell();
    const ord = el('div', 'ord');
    [['↑', -1], ['↓', 1]].forEach(([glyph, d]) => {
      const b = el('button', null, glyph);
      b.title = d < 0 ? 'Higher priority' : 'Lower priority';
      b.disabled = i + d < 0 || i + d >= rows.length;
      b.onclick = () => {
        const next = rows.map((r) => r.slice());
        [next[i], next[i + d]] = [next[i + d], next[i]];
        commit(next);
      };
      ord.append(b);
    });
    td.append(ord);
  });

  const wrap = el('div');
  wrap.append(t);

  // The budget is DERIVED from the context window — there is no BUDGET_TOTAL knob
  // any more — so show the derivation here, because this panel is where someone
  // decides what the window should be. Reading it off a knob that no longer
  // exists is what made this whole group fail to render.
  const ctx = SET.data.context || {};
  const budget = Number(ctx.budget || 0);
  const floors = rows.reduce((n, r) => n + Number(r[1]), 0);
  const ok = floors <= budget;

  const gib = ctx.kv_bytes ? (ctx.kv_bytes / 1073741824).toFixed(2) + ' GiB' : '\u2014';
  wrap.append(el('div', 'floorNote muted',
    `Window ${Number(ctx.window || 0).toLocaleString()} tokens`
    + ` \u2212 ${Number(ctx.reply_tokens || 0).toLocaleString()} reserved for the reply`
    + ` = ${budget.toLocaleString()} characters of budget`
    + ` \u00b7 KV cache ${gib}`
    + (ctx.trained_ctx
        ? ` \u00b7 model trained to ${Number(ctx.trained_ctx).toLocaleString()}`
        : '')));

  // Past the trained length a model still loads and still answers, just worse —
  // so this is a warning, not a refusal.
  if (ctx.within_trained === false) {
    wrap.append(el('div', 'floorNote bad',
      `Window exceeds what this model was trained for`
      + ` (${Number(ctx.trained_ctx).toLocaleString()}). Quality degrades past the`
      + ' trained length even when the model loads.'));
  }

  // Floors are the one constraint the arbiter cannot work around: if they sum
  // past the budget, every layer is squeezed on every turn. Say so here rather
  // than waiting for the context panel to report it mid-scene.
  wrap.append(el('div', 'floorNote' + (ok ? ' muted' : ' bad'),
    `Floors total ${floors.toLocaleString()} of ${budget.toLocaleString()}` +
    (ok ? ` \u2014 ${(budget - floors).toLocaleString()} left to distribute.`
        : ' \u2014 floors exceed the budget. Raise the context window.')));
  return wrap;
}

async function saveSettings() {
  const btn = $('setSave');
  btn.disabled = true;
  $('setStatus').textContent = 'saving…';
  try {
    const r = await api('/api/settings', { changes: SET.pending });
    SET.warnings = r.warnings || [];
    SET.data = r;
    SET.pending = {};
    $('setStatus').textContent = SET.warnings.length ? 'saved with warnings' : 'saved';
    setTimeout(() => { $('setStatus').textContent = ''; }, 2500);
  } catch (e) {
    $('setStatus').textContent = e.message;
  }
  renderSettings();
}

/* ------------------------------------------------------------------ editor */

const ED = { id: '', data: null, section: 'Story', isNew: true, idTouched: false, dirty: false };
const ED_SECTIONS = ['Story', 'Intros', 'Stats', 'Keywords', 'Cast'];

async function newStory() {
  ED.data = await api('/api/story/new');
  ED.id = '';
  ED.isNew = true;
  ED.idTouched = false;
  ED.dirty = false;
  ED.section = 'Story';
  showEditor();
}

async function editStory(id) {
  ED.data = await api('/api/story/' + id);
  ED.id = id;
  ED.isNew = false;
  ED.idTouched = true;
  ED.dirty = false;
  ED.section = 'Story';
  showEditor();
  if (ED.data._problems?.length) showProblems(ED.data._problems);
}

function showEditor() {
  $('boot').classList.add('hidden');
  $('editor').classList.remove('hidden');
  $('edProblems').classList.add('hidden');
  $('edStatus').textContent = '';
  renderEditor();
}

function closeEditor() {
  $('editor').classList.add('hidden');
  $('boot').classList.remove('hidden');
  boot();
}

function showProblems(problems, ok, fit) {
  const box = $('edProblems');
  box.innerHTML = '';
  box.classList.remove('hidden');

  // A story that fails the budget is valid but broken in a way you would never
  // see from the prose — the arbiter just quietly stops sending part of it.
  const over = (fit || []).filter((f) => !f.fits);
  box.classList.toggle('ok', !!ok && !over.length);

  if (problems.length) {
    box.append(el('b', null, `${problems.length} problem${problems.length > 1 ? 's' : ''}`));
    const ul = document.createElement('ul');
    problems.forEach((p) => ul.append(el('li', null, p)));
    box.append(ul);
  } else if (ok) {
    box.append(el('b', null, over.length
      ? 'Valid, but too big for the context budget'
      : 'No problems — ready to save.'));
  }

  if (over.length) {
    const ul = document.createElement('ul');
    over.forEach((f) => ul.append(el('li', null,
      `${f.layer} (${f.scope}): ${f.wanted.toLocaleString()} chars against a ` +
      `${f.ceiling.toLocaleString()} ceiling — ${(f.wanted - f.ceiling).toLocaleString()} ` +
      `over. The lowest-priority part (${f.detail}) will be dropped from every turn. ` +
      'Shorten it, or raise the ceiling under Settings → Context budget.')));
    box.append(ul);
  }
}

/* -- small bound controls. They mutate ED.data in place and never re-render,
      because re-rendering on each keystroke would steal focus mid-word. */

const touch = () => { ED.dirty = true; };

function field(labelText, control, opts = {}) {
  const f = el('div', 'field' + (opts.cls ? ' ' + opts.cls : ''));
  if (labelText) {
    const l = el('label', null, labelText);
    if (opts.req) l.append(el('span', 'req', '*'));
    if (opts.count) {
      const c = el('span', 'count');
      const upd = () => { c.textContent = (control.value || '').length + ' chars'; };
      control.addEventListener('input', upd);
      upd();
      l.append(c);
    }
    f.append(l);
  }
  f.append(control);
  if (opts.hint) f.append(el('div', 'hint', opts.hint));
  return f;
}

function txt(obj, key, ph) {
  const i = el('input');
  i.type = 'text';
  i.value = obj[key] ?? '';
  if (ph) i.placeholder = ph;
  i.oninput = () => { obj[key] = i.value; touch(); };
  return i;
}

function area(obj, key, rows, ph) {
  const t = document.createElement('textarea');
  t.rows = rows;
  t.value = obj[key] ?? '';
  if (ph) t.placeholder = ph;
  t.oninput = () => { obj[key] = t.value; touch(); };
  return t;
}

function num(obj, key) {
  const i = el('input');
  i.type = 'number';
  i.step = 'any';
  i.value = obj[key] ?? 0;
  i.oninput = () => { obj[key] = i.value === '' ? '' : Number(i.value); touch(); };
  return i;
}

function chk(obj, key) {
  const i = el('input');
  i.type = 'checkbox';
  i.checked = !!obj[key];
  i.onchange = () => { obj[key] = i.checked; touch(); };
  return i;
}

/* Lists are edited as text — comma-separated for short tokens, one-per-line for
   anything that might itself contain a comma. */
function csvList(obj, key, ph) {
  const i = el('input');
  i.type = 'text';
  i.value = (obj[key] || []).join(', ');
  if (ph) i.placeholder = ph;
  i.oninput = () => {
    obj[key] = i.value.split(',').map((s) => s.trim()).filter(Boolean);
    touch();
  };
  return i;
}

function lineList(obj, key, rows, ph) {
  const t = document.createElement('textarea');
  t.rows = rows;
  t.value = (obj[key] || []).join('\n');
  if (ph) t.placeholder = ph;
  t.oninput = () => {
    obj[key] = t.value.split('\n').map((s) => s.trim()).filter(Boolean);
    touch();
  };
  return t;
}

function row(...fields) {
  const r = el('div', 'fieldRow');
  fields.forEach((f) => r.append(f));
  return r;
}

/* -- repeatable sections */

function repeatable(list, opts) {
  const wrap = el('div');
  if (!list.length) wrap.append(el('div', 'edEmpty', opts.empty));

  list.forEach((item, i) => {
    const card = el('div', 'edCard');
    const head = document.createElement('header');
    head.append(el('span', null, opts.title(item, i)), el('div', 'grow'));

    [['↑', -1], ['↓', 1]].forEach(([glyph, d]) => {
      const b = el('button', null, glyph);
      b.disabled = i + d < 0 || i + d >= list.length;
      b.onclick = () => {
        [list[i], list[i + d]] = [list[i + d], list[i]];
        touch();
        renderEditor();
      };
      head.append(b);
    });

    const rm = el('button', 'rm', 'Remove');
    rm.disabled = !!opts.minOne && list.length === 1;
    rm.title = rm.disabled ? 'A story needs at least one' : '';
    rm.onclick = async () => {
      if (!await ask(`Remove ${opts.title(item, i)}?`, 'Remove')) return;
      list.splice(i, 1);
      touch();
      renderEditor();
    };
    head.append(rm);

    card.append(head);
    opts.body(item).forEach((n) => card.append(n));
    wrap.append(card);
  });

  const add = el('button', 'addRow', opts.add);
  add.onclick = () => { list.push(opts.blank()); touch(); renderEditor(); };
  wrap.append(add);
  return wrap;
}

/* -- sections */

function secStory(d) {
  const out = [];

  const idInput = txt(ED, 'id', 'lowercase-with-hyphens');
  idInput.disabled = !ED.isNew;
  idInput.oninput = () => { ED.idTouched = true; ED.id = idInput.value; touch(); };

  const name = txt(d, 'name', 'The Lirath Road');
  name.oninput = () => {
    d.name = name.value;
    touch();
    // The id is a directory name and can never change after creation, so it is
    // derived from the title only until the author takes it over.
    if (ED.isNew && !ED.idTouched) {
      ED.id = name.value.toLowerCase().replace(/[^a-z0-9]+/g, '-')
        .replace(/^-|-$/g, '').slice(0, 64);
      idInput.value = ED.id;
    }
  };

  out.push(field('Title', name, { req: true }));
  out.push(field('Folder name', idInput, {
    hint: ED.isNew
      ? 'Becomes stories/<name>/story.yaml. Fixed once the story is created.'
      : 'Fixed after creation. Use Duplicate to make a copy under a new name.',
    req: true,
  }));
  out.push(field('Tagline', txt(d, 'tagline', 'One line, shown on the story card.')));

  // Mode is structural, not a preference: it decides which subsystems run at
  // all. Without this control a story could only ever be 'play', and raw was
  // reachable only by hand-editing the yaml.
  const modeSel = document.createElement('select');
  [['play', 'Play - you are a character in it'],
   ['raw', 'Raw - you direct; no memory, chapters, lorebook or goals'],
   ['narrative', 'Narrative - nobody plays; the model writes everyone'],
  ].forEach(([v, label]) => {
    const o = document.createElement('option');
    o.value = v; o.textContent = label;
    if ((d.mode || 'play') === v) o.selected = true;
    modeSel.append(o);
  });
  modeSel.onchange = () => { d.mode = modeSel.value; touch(); };
  out.push(field('Mode', modeSel, {
    hint: 'Raw runs the story on its rules, the Ledger and the transcript alone '
        + '- the other subsystems do not execute, so a global setting cannot '
        + 'switch them back on underneath it. Its panels are hidden during play.',
  }));

  out.push(field('Rules', area(d, 'rules', 12,
    'How the story is narrated. Tense, person, tone, pacing, what the narrator ' +
    'may and may not decide on the player\'s behalf.'), {
    req: true, count: true,
    hint: 'The custom prompt. Always present, second in priority only to a ' +
          'director\'s note — this is the one block that is never trimmed away.',
  }));

  out.push(field('World details', area(d, 'details', 10,
    'Setting, factions, geography, what is common knowledge.'), {
    count: true,
    hint: 'Also always present. Split anything that only matters sometimes into ' +
          'a keyword note instead, so it costs budget only when relevant.',
  }));

  out.push(field('Image style', txt(d, 'style_prompt', 'oil painting, muted palette'), {
    hint: 'Appended to every portrait and scene prompt.',
  }));

  if (!ED.isNew) {
    out.push(el('div', 'edEmpty',
      'Saving rewrites stories/' + ED.id + '/story.yaml from these fields, which ' +
      'drops any YAML comments in it. The previous version is kept as story.yaml.bak.'));
  }
  return out;
}

function secIntros(d) {
  return [repeatable(d.intros, {
    title: (it, i) => it.name || it.id || `Intro ${i + 1}`,
    add: '+ Add an opening',
    empty: 'Every story needs at least one opening.',
    minOne: true,
    blank: () => ({ id: 'opening-' + (d.intros.length + 1), name: '', prologue: '',
                    opening_scene: '', play_guide: '', suggestions: [] }),
    body: (it) => [
      row(field('Name', txt(it, 'name', 'Out of a Clear Sky'), { req: true }),
          field('Id', txt(it, 'id', 'out-of-a-clear-sky'), { req: true, cls: 'narrow' })),
      field('Prologue', area(it, 'prologue', 9, 'The first thing the player reads.'),
        { req: true, count: true,
          hint: 'Stored as the opening assistant turn — written as narration, not as setup.' }),
      field('Opening scene', area(it, 'opening_scene', 5), {
        hint: 'Context for the model about how things began. Sits inside the rules layer.',
      }),
      field('Play guide', area(it, 'play_guide', 3), {
        hint: 'Shown to the player, never sent to the model.',
      }),
      field('Suggested first moves', lineList(it, 'suggestions', 4,
        'One per line.\nThey appear as buttons under the composer.')),
    ],
  })];
}

function secStats(d) {
  return [
    el('div', 'edEmpty', 'Numbers the model may move up and down as the story goes. ' +
      'They are shown to the player and included in every prompt, so keep them few.'),
    repeatable(d.stats, {
      title: (s, i) => s.name || s.key || `Stat ${i + 1}`,
      add: '+ Add a stat',
      empty: 'No stats. That is a valid choice — a story can be pure narration.',
      blank: () => ({ key: '', name: '', unit: '', min: 0, max: 100, default: 0, description: '' }),
      body: (s) => [
        row(field('Name', txt(s, 'name', 'Coin'), { req: true }),
            field('Key', txt(s, 'key', 'coin'), { req: true, cls: 'narrow' }),
            field('Unit', txt(s, 'unit', 'sc'), { cls: 'narrow' })),
        row(field('Min', num(s, 'min'), { cls: 'narrow' }),
            field('Max', num(s, 'max'), { cls: 'narrow' }),
            field('Start', num(s, 'default'), { cls: 'narrow' })),
        field('Description', area(s, 'description', 2,
          'What it means, and what should move it.'), {
          req: true,
          hint: 'The model reads this to decide when to change the value.',
        }),
      ],
    }),
  ];
}

function secKeywords(d) {
  return [
    el('div', 'edEmpty', 'Lore that enters the prompt only when its keywords appear in ' +
      'recent turns. This is where the bulk of a world belongs — it costs nothing on ' +
      'the turns it is not needed.'),
    repeatable(d.keywords, {
      title: (k, i) => k.title || `Note ${i + 1}`,
      add: '+ Add a keyword note',
      empty: 'No keyword notes yet.',
      blank: () => ({ title: '', keywords: [], body: '', always: false }),
      body: (k) => [
        field('Title', txt(k, 'title', 'The Lirath Guild'), { req: true }),
        field('Triggers', csvList(k, 'keywords', 'guild, lirath, guildhall'), {
          req: true,
          hint: 'Comma separated, matched case-insensitively against recent turns.',
        }),
        field('Body', area(k, 'body', 6), { req: true, count: true }),
        (() => {
          const f = field('', chk(k, 'always'), { cls: 'inline' });
          f.append(el('label', null, 'Always include, without a keyword match'));
          return f;
        })(),
      ],
    }),
  ];
}

function secCast(d) {
  return [
    el('div', 'edEmpty', 'Characters the story can draw portraits for — and, just as ' +
      'importantly, the descriptions the extractor uses to work out who is who.'),
    repeatable(d.cast, {
      title: (c, i) => c.name || `Character ${i + 1}`,
      add: '+ Add a character',
      empty: 'No cast yet. Portraits fall back to a generic prompt without one.',
      blank: () => ({ name: '', short: '', aliases: [], prompt: '' }),
      body: (c) => [
        row(field('Name', txt(c, 'name', 'Wren Sato'), { req: true }),
            field('Also known as', csvList(c, 'aliases', 'Wren, the scribe'))),
        field('Short descriptor', txt(c, 'short', 'the half-elf with the ledger'), {
          hint: 'How narration refers to them before the player learns their name. ' +
                'Without this the state extractor has to guess which "the half-elf" ' +
                'is meant, and it guesses wrong.',
        }),
        field('Portrait prompt', area(c, 'prompt', 4,
          '1girl, half-elf, dark bob, ink-stained fingers, guild coat'), {
          req: true,
          hint: 'Image tags, not prose. Quality tags and image style are added for you.',
        }),
      ],
    }),
  ];
}

const ED_RENDER = { Story: secStory, Intros: secIntros, Stats: secStats,
                    Keywords: secKeywords, Cast: secCast };

function renderEditor() {
  const d = ED.data;
  $('edTitle').textContent = d.name || (ED.isNew ? 'New story' : ED.id);
  $('edIdLabel').textContent = ED.id ? `stories/${ED.id}` : '';

  $('edDup').classList.toggle('hidden', ED.isNew);
  $('edDel').classList.toggle('hidden', ED.isNew);

  const counts = { Story: null, Intros: d.intros.length, Stats: d.stats.length,
                   Keywords: d.keywords.length, Cast: d.cast.length };
  const nav = $('edNav');
  nav.innerHTML = '';
  ED_SECTIONS.forEach((s) => {
    const b = el('button', s === ED.section ? 'on' : null, s);
    if (counts[s] != null) b.append(el('span', 'dot muted', ' ' + counts[s]));
    b.onclick = () => { ED.section = s; renderEditor(); };
    nav.append(b);
  });

  const body = $('edBody');
  body.innerHTML = '';
  const wrap = el('div', 'setGroup');
  wrap.append(el('h2', null, ED.section));
  ED_RENDER[ED.section](d).forEach((n) => wrap.append(n));
  body.append(wrap);
}

function edPayload() {
  const { _problems, _path, _loaded, id, ...story } = ED.data;
  return { id: ED.id.trim(), story, create: ED.isNew };
}

async function checkStory() {
  const r = await api('/api/story/validate', edPayload());
  showProblems(r.problems, r.ok, r.fit);
  return r.ok;
}

async function saveStory() {
  $('edStatus').textContent = 'saving…';
  try {
    const r = await api('/api/story/save', edPayload());
    ED.id = r.id;
    ED.isNew = false;
    ED.dirty = false;
    $('edStatus').textContent = 'saved';
    const v = await api('/api/story/validate', edPayload()).catch(() => null);
    showProblems([], true, v && v.fit);
    renderEditor();
    setTimeout(() => { $('edStatus').textContent = ''; }, 2000);
  } catch (e) {
    $('edStatus').textContent = '';
    // The server reports every problem at once; prefer that structured list to
    // the flattened exception text.
    const r = await api('/api/story/validate', edPayload()).catch(() => null);
    showProblems(r && !r.ok ? r.problems : [e.message], false, r && r.fit);
  }
}


/* ------------------------------------------------------------------ chapters

   Compaction, not a checkpoint. A chapter's turns are replaced in the prompt by
   its summary, which is the only reason a 130-turn story still fits in a fixed
   window. It happens on its own every N turns and says so afterwards.

   It used to block: you could not play on until you had reviewed the recap. The
   review was genuinely valuable and the timing was indefensible — being stopped
   mid-scene produces a rubber-stamp, not a careful edit. Every summary is still
   editable here, whenever you feel like editing it. */

const CH = { draft: null, busy: false };

function renderChapter(state) {
  const c = state.chapter || {};
  const chip = $('chapChip');
  if (!c.enabled) { chip.classList.add('hidden'); return; }
  chip.classList.remove('hidden');
  // Amber near the end of a chapter — not a warning, just a heads-up that the
  // next few turns will end with a compaction.
  chip.classList.toggle('due', c.every ? c.turns_in >= c.every - 2 : false);
  chip.textContent = `Ch. ${c.number} · ${c.turns_in}/${c.every}`;
  chip.title = `Turn ${c.turns_in} of about ${c.every}. `
    + 'Click to compact now and start the next chapter.';

  renderChapterList(state.chapters || []);
}

/* The notice after an automatic compaction. Deliberately a persistent strip
   rather than a toast: it is the one moment the prompt changes shape underneath
   the story, and it usually arrives with lorebook suggestions attached. */
function chapterClosed(data) {
  const bar = $('chapNotice');
  bar.innerHTML = '';
  bar.classList.remove('hidden');

  const n = (data.suggestions || []).length;
  bar.append(el('span', 'cn', `Chapter ${data.number} — ${data.title}`));
  bar.append(el('span', 'muted small',
    `${data.turns} turns compacted${n ? `, ${n} lorebook suggestion${n > 1 ? 's' : ''}` : ''}`));

  const read = el('button', 'ghost small', 'Read');
  read.onclick = () => {
    const c = (S.state?.chapters || []).find((x) => x.number === data.number);
    if (c) openChapterEditor(c);
  };
  const hide = el('button', 'ghost small', '✕');
  hide.onclick = () => bar.classList.add('hidden');
  bar.append(el('span', 'grow'), read, hide);
}

function renderChapterList(chapters) {
  const w = $('chapterList');
  w.innerHTML = '';
  if (!chapters.length) {
    w.append(el('div', 'muted small',
      'None yet. The first is compacted automatically once the story has run long enough.'));
    return;
  }
  chapters.forEach((c) => {
    const row = el('div', 'chapRow');
    row.append(el('span', 'n', String(c.number)),
               el('span', 'nm', c.title || '(untitled)'));
    row.onclick = () => openChapterEditor(c);
    w.append(row);
  });
}

/* -- the review dialog */

async function openChapterBreak() {
  if (CH.busy) return;
  CH.busy = true;
  $('chapDlgTitle').textContent = 'Compact chapter';
  $('chapDlgStatus').textContent = '';
  $('chapAccept').disabled = true;
  $('chapDlgBody').innerHTML = '';
  $('chapDlgBody').append(el('div', 'chapDrafting',
    'Reading the chapter and drafting a recap…'));
  $('chapWhy').textContent = '';
  $('chapDlg').showModal();
  try {
    CH.draft = await api('/api/chapter/draft', { session: S.id });
    if (CH.draft.error) {
      $('chapDlgBody').innerHTML = '';
      $('chapDlgBody').append(el('div', 'chapDrafting', CH.draft.error));
      return;
    }
    renderChapterBreak();
  } catch (e) {
    $('chapDlgBody').innerHTML = '';
    $('chapDlgBody').append(el('div', 'chapDrafting', 'Draft failed: ' + e.message));
  } finally {
    CH.busy = false;
  }
}

function renderChapterBreak() {
  const d = CH.draft;
  const body = $('chapDlgBody');
  body.innerHTML = '';
  $('chapAccept').disabled = false;
  $('chapAccept').textContent = 'Compact & continue';
  $('chapDlgTitle').textContent = `Compact chapter ${d.chapter.number}`;
  $('chapWhy').textContent =
    'Releases this chapter’s turns from the prompt and keeps the summary instead.';

  body.append(el('h4', null, 'Title'));
  const title = el('input');
  title.type = 'text';
  title.value = d.title || '';
  body.append(title);

  body.append(el('h4', null, 'Recap'));
  body.append(el('div', 'lede',
    'This replaces the chapter’s turns in the prompt from here on. Anything not in it '
    + 'is carried only by the lorebook, so add what must not be lost.'));
  const summary = document.createElement('textarea');
  summary.rows = 11;
  summary.value = d.summary || '';
  body.append(summary);

  let syn = null;
  if (d.synopsis != null) {
    body.append(el('h4', null, 'Story so far'));
    body.append(el('div', 'lede',
      'Older chapters have aged out of the verbatim window and been folded into this. '
      + 'It stays in the prompt permanently, so it is worth reading properly.'));
    syn = document.createElement('textarea');
    syn.rows = 7;
    syn.value = d.synopsis || '';
    body.append(syn);
  }

  $('chapAccept').onclick = async () => {
    $('chapAccept').disabled = true;
    $('chapDlgStatus').textContent = 'saving…';
    try {
      const st = await api('/api/chapter/close', {
        session: S.id,
        title: title.value,
        summary: summary.value,
        synopsis: syn ? syn.value : undefined,
      });
      $('chapDlg').close();
      render(st);
      refreshContext();
      $('status').textContent = `Chapter ${d.chapter.number} compacted.`;
      setTimeout(() => { $('status').textContent = ''; }, 4000);
    } catch (e) {
      $('chapDlgStatus').textContent = e.message;
      $('chapAccept').disabled = false;
    }
  };
}

/* -- re-reading and editing a closed chapter. This is how a detail that turns
      out to matter later gets rescued back into the prompt. */
function openChapterEditor(c) {
  const body = $('chapDlgBody');
  body.innerHTML = '';
  $('chapDlgTitle').textContent = `Chapter ${c.number}`;
  $('chapDlgStatus').textContent = '';
  $('chapWhy').textContent = 'Edits take effect on the next turn.';
  $('chapAccept').disabled = false;
  $('chapAccept').textContent = 'Save';

  body.append(el('h4', null, 'Title'));
  const title = el('input');
  title.type = 'text';
  title.value = c.title || '';
  body.append(title);

  body.append(el('h4', null, 'Recap'));
  const summary = document.createElement('textarea');
  summary.rows = 12;
  summary.value = c.summary || '';
  body.append(summary);

  body.append(el('div', 'lede',
    `Turns ${c.start_turn}–${c.end_turn}. These turns are no longer in the prompt; ` +
    'this text is what the story remembers of them.'));

  $('chapAccept').onclick = async () => {
    $('chapDlgStatus').textContent = 'saving…';
    render(await api('/api/chapter/update', {
      session: S.id, id: c.id, title: title.value, summary: summary.value }));
    $('chapDlg').close();
    refreshContext();
  };
  $('chapDlg').showModal();
}

/* ------------------------------------------------------------------ wiring */

$('composer').addEventListener('submit', (e) => {
  e.preventDefault();
  const t = $('input').value.trim();
  // An empty turn is meaningless when you are playing a character and is the
  // whole interaction when you are reading one. assemble.build() supplies the
  // user turn the provider requires.
  if (!t && !narrative()) return;
  $('input').value = '';
  send(t);
});

$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('composer').requestSubmit(); }
});

$('notes').addEventListener('input', () => {
  clearTimeout(S.notesTimer);
  $('notesSaved').textContent = '';
  S.notesTimer = setTimeout(async () => {
    await api('/api/notes', { session: S.id, text: $('notes').value });
    $('notesSaved').textContent = 'saved';
    refreshContext();
    setTimeout(() => { $('notesSaved').textContent = ''; }, 1500);
  }, 700);
});

/* Coming back to a backgrounded tab. Browsers throttle or suspend timers and
   fetch readers in hidden tabs, so a turn that was streaming when you switched
   away is usually finished-but-undelivered by the time you return. Pull fresh
   state on the way back in rather than showing whatever was frozen on screen. */
document.addEventListener('visibilitychange', async () => {
  if (document.hidden || !S.id || $('app').classList.contains('hidden')) return;
  try {
    const st = await api('/api/session/' + S.id);
    const shown = (S.state?.messages || []).length;
    if (st.messages.length !== shown) {
      render(st);
      refreshContext();
    }
  } catch { /* offline — the next interaction will refresh */ }
});

$('messages').addEventListener('scroll', updateLatestButton, { passive: true });
$('toLatest').onclick = () => { toBottom($('messages')); updateLatestButton(); };
$('lightboxImg').onclick = () => $('lightbox').close();
$('lightbox').onclick = (e) => { if (e.target === $('lightbox')) $('lightbox').close(); };

$('sceneToggle').onclick = () => setScene(!sceneOpen());
$('btnCast').onclick = () => openCast(CAST.selected);
$('chapChip').onclick = openChapterBreak;
$('actChip').onclick = pickAct;
$('btnChapters').onclick = openChapterBreak;
$('chapCancel').onclick = () => $('chapDlg').close();
$('btnYou').onclick = () => openPC();
$('pcCancel').onclick = () => $('pcDlg').close();
$('btnLore').onclick = () => openLore(null);
$('loreCancel').onclick = () => $('loreDlg').close();
$('loreDlg').onclick = (e) => { if (e.target === $('loreDlg')) $('loreDlg').close(); };
$('castClose').onclick = () => $('castDlg').close();
$('castDlg').onclick = (e) => { if (e.target === $('castDlg')) $('castDlg').close(); };

$('btnPanels').onclick = () => $('panels').classList.toggle('open');
$('btnBack').onclick = () => {
  $('app').classList.add('hidden');
  $('boot').classList.remove('hidden');
  boot();
};
$('btnPrompt').onclick = async () => {
  const p = await api('/api/prompt?session=' + S.id);
  $('promptText').textContent =
    p.system + '\n\n--- transcript (' + p.messages.length + ' messages) ---\n\n' +
    p.messages.map((m) => m.role.toUpperCase() + ': ' + m.content).join('\n\n');
  $('promptDlg').showModal();
};

$('btnSettings').onclick = () => openSettings('boot');
$('btnSettings2').onclick = () => openSettings('app');
$('setBack').onclick = async () => {
  if (Object.keys(SET.pending).length && !await ask('Discard unsaved changes?', 'Discard')) return;
  closeSettings();
};
$('setSave').onclick = saveSettings;
$('edBack').onclick = async () => {
  if (ED.dirty && !await ask('Discard unsaved changes to this story?', 'Discard')) return;
  closeEditor();
};
$('edCheck').onclick = checkStory;
$('edSave').onclick = saveStory;
$('edDup').onclick = async () => {
  if (ED.dirty && !await ask('Unsaved changes will not be copied. Continue?', 'Continue')) return;
  const name = prompt('Name for the copy:', ED.data.name + ' (copy)');
  if (!name) return;
  const id = (await api('/api/story/slug', { name })).id;
  try {
    const r = await api('/api/story/duplicate', { from: ED.id, id, name });
    await editStory(r.id);
    $('edStatus').textContent = 'copied to stories/' + r.id;
  } catch (e) {
    showProblems([e.message]);
  }
};
$('edDel').onclick = async () => {
  const name = ED.data.name || ED.id;
  if (!await ask('Delete "' + name + '"? The folder is archived to '
                 + 'state/deleted-stories first, so this is recoverable.')) return;
  try {
    // First call reports what would break. It only deletes outright when
    // nothing references the story, which is the case just confirmed above.
    let r = await api('/api/story/delete', { id: ED.id });
    if (r.needs_confirm) {
      const who = r.titles.length ? ' (' + r.titles.join(', ') + ')' : '';
      if (!await ask(r.sessions + ' saved session(s) use this story' + who
            + '. Their transcripts are kept, but they will not open until the '
            + 'story is restored. Delete anyway?', 'Delete anyway')) return;
      r = await api('/api/story/delete', { id: ED.id, confirm: true });
    }
    $('edStatus').textContent = 'archived to ' + r.archive;
    setTimeout(() => location.reload(), 900);
  } catch (e) {
    showProblems([e.message]);
  }
};

$('setReset').onclick = async () => {
  if (!await ask('Restore every setting to its default?', 'Reset all')) return;
  SET.data = await api('/api/settings/reset', {});
  SET.pending = {};
  $('setStatus').textContent = 'reset to defaults';
  setTimeout(() => { $('setStatus').textContent = ''; }, 1800);
  renderSettings();
};

boot();


/* ------------------------------------------------------------------ ledger
   Facts you have approved. Proposed automatically when the context budget
   crosses LEDGER_TRIGGER_FILL, and inert until you press keep -- so a wrong
   proposal costs nothing. The full editor is at /ledger. */

async function renderLedger() {
  const sid = S.state?.session?.id;
  const box = $('ledgerActive'), prop = $('ledgerProposed');
  if (!sid || !box) return;
  let d;
  try { d = await (await fetch('/api/ledger?session=' + sid)).json(); }
  catch (e) { return; }

  const chip = $('ledgerNew');
  chip.textContent = d.counts.proposed || '';
  chip.classList.toggle('hidden', !d.counts.proposed);
  $('ledgerCounts').textContent = d.counts.active + '/' + d.max_active;

  const row = (f, proposed) => {
    const w = el('div', 'ledFact');
    w.append(el('span', 'k', f.kind.slice(0, 4)), el('div', 't', f.text));
    const act = (a) => fetch('/api/ledger', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ session: sid, action: a, id: f.id })
    }).then(renderLedger);
    if (proposed) {
      const y = el('button', 'ghost small', 'keep'); y.onclick = () => act('accept');
      const n = el('button', 'ghost small', 'bin');  n.onclick = () => act('dismiss');
      w.append(y, n);
    } else {
      const x = el('button', 'ghost small', '\u00d7'); x.onclick = () => act('delete');
      w.append(x);
    }
    return w;
  };

  prop.replaceChildren(...d.proposed.map((f) => row(f, true)));
  box.replaceChildren(...(d.active.length
    ? d.active.map((f) => row(f, false))
    : [el('div', 'muted small', 'No facts yet. They are proposed when the '
         + 'context budget fills.')]));
}
