#!/usr/bin/env python3
"""Prompt review tool for the model sweep. http://atlas:8101

Nothing runs until it is approved here, and approval is bound to the exact text:
change a character and that scene drops back to unapproved.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prompt import build, fingerprint

STATE = os.environ.get("BENCH_STATE",
                       os.path.expanduser("~/docker/config/loom/state/scenes.json"))
PORT = int(os.environ.get("BENCH_PORT", "8101"))


def load():
    with open(STATE) as f:
        return json.load(f)


def save(d):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, STATE)


def decorate(d):
    """Attach the rendered payload and the live approval state to each scene."""
    for s in d["scenes"]:
        s["payload"] = build(d["rules"], d["template"], s)
        fp = fingerprint(d["rules"], d["template"], s)
        s["stale"] = bool(s.get("approved")) and s.get("fingerprint") != fp
        if s["stale"]:
            s["approved"] = False
    return d


HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Sweep prompt review</title><style>
:root{--bg:#f7f7f5;--fg:#1a1a18;--mut:#6b6b66;--line:#dcdcd6;--card:#fff;--ok:#1c7c4a;--warn:#a8541b}
@media(prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#e8e8e4;--mut:#96968e;--line:#32323a;--card:#1e1e24;--ok:#57c98a;--warn:#e0965a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 ui-sans-serif,system-ui,sans-serif}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);
padding:14px 20px;display:flex;gap:16px;align-items:center;z-index:9}
h1{font-size:15px;margin:0;font-weight:600}.count{color:var(--mut)}
button{font:inherit;padding:6px 14px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--mut)}
button.pri{background:var(--fg);color:var(--bg);border-color:var(--fg)}
main{padding:20px;max-width:1500px;margin:0 auto}
.scene{background:var(--card);border:1px solid var(--line);border-radius:10px;
margin-bottom:16px;overflow:hidden}
.scene.ok{border-color:var(--ok)}
.hd{padding:12px 16px;display:flex;gap:12px;align-items:center;cursor:pointer;
user-select:none}
.hd>*{pointer-events:none}
.scene.open .hd{border-bottom:1px solid var(--line)}
.id{font-weight:600;font-family:ui-monospace,monospace}
.flinch{color:var(--mut);font-size:13px;flex:1}
.badge{font-size:12px;padding:2px 9px;border-radius:20px;border:1px solid var(--line)}
.badge.ok{color:var(--ok);border-color:var(--ok)}
.badge.no{color:var(--warn);border-color:var(--warn)}
.body{display:none;grid-template-columns:1fr 1fr;gap:18px;padding:16px}
.scene.open .body{display:grid}
label{display:block;font-size:12px;color:var(--mut);margin:10px 0 4px;
text-transform:uppercase;letter-spacing:.04em}
textarea{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--line);
border-radius:6px;padding:9px;font:13px/1.5 ui-monospace,monospace;resize:vertical}
pre{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:12px;
white-space:pre-wrap;word-break:break-word;font:12px/1.5 ui-monospace,monospace;margin:0}
.role{font-size:11px;color:var(--mut);margin:10px 0 4px;text-transform:uppercase}
.acts{display:flex;gap:8px;align-items:center;margin-top:14px}
.globals{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px;margin-bottom:20px}
.hint{color:var(--mut);font-size:12px;margin-top:6px}
.dirty{color:var(--warn)}
</style></head><body>
<header><h1>Sweep prompt review</h1>
<span class="count" id="count"></span>
<span class="hint" id="status"></span>
<span style="flex:1"></span>
<button id="expand">expand all</button>
<button class="pri" id="save">save</button></header>
<main>
<div class="globals">
  <label>rules - shared by every scene, top of every system prompt</label>
  <textarea id="rules" rows="5"></textarea>
  <label>user message template - <code>{direction}</code> is replaced; nothing else is added</label>
  <textarea id="template" rows="2"></textarea>
  <div class="hint">Editing either un-approves every scene.</div>
</div>
<div id="list"></div>
</main>
<script>
let D=null, dirty=false, timers={};
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const card=id=>document.querySelector(`.scene[data-id="${id}"]`);

function counts(){
  const en=D.scenes.filter(s=>s.enabled);
  $('#count').textContent=`${en.filter(s=>s.approved).length} of ${en.length} enabled scenes approved`;
  $('#status').textContent=dirty?'unsaved':'';
  $('#status').className='hint'+(dirty?' dirty':'');
}
function badge(s){
  return `<span class="badge ${s.approved?'ok':'no'}">${
    s.approved?'approved':'not approved'}</span>`;
}
// Repaint ONE card's header + button, never the whole list. Rebuilding every
// card on each click was the lag, and it also ate focus mid-edit.
function paint(id){
  const s=D.scenes.find(x=>x.id===id), c=card(id);
  c.classList.toggle('ok',!!s.approved);
  c.querySelector('.st').innerHTML=(s.enabled?'':'<span class="badge">disabled</span>')+badge(s);
  const b=c.querySelector('[data-approve]');
  b.textContent=s.approved?'un-approve':'approve this prompt';
  b.className=s.approved?'':'pri';
  c.querySelector('[data-toggle]').textContent=s.enabled?'disable':'enable';
  counts();
}
function render(){
  $('#rules').value=D.rules; $('#template').value=D.template;
  $('#list').innerHTML=D.scenes.map(s=>`
  <div class="scene ${s.approved?'ok':''}" data-id="${s.id}">
    <div class="hd" data-t="${s.id}">
      <span class="id">${s.id}</span><span class="flinch">${esc(s.flinch)}</span>
      <span class="st">${s.enabled?'':'<span class="badge">disabled</span>'}${badge(s)}</span>
    </div>
    <div class="body">
      <div>
        <label>flinch this probes</label><textarea rows="2" data-f="flinch" data-id="${s.id}">${esc(s.flinch)}</textarea>
        <label>world</label><textarea rows="4" data-f="world" data-id="${s.id}">${esc(s.world)}</textarea>
        <label>situation</label><textarea rows="4" data-f="situation" data-id="${s.id}">${esc(s.situation)}</textarea>
        <label>direction - what you, the director, type</label><textarea rows="4" data-f="direction" data-id="${s.id}">${esc(s.direction)}</textarea>
        <label>judge criterion - what counts as committed</label><textarea rows="4" data-f="committed" data-id="${s.id}">${esc(s.committed)}</textarea>
        <div class="acts">
          <button data-approve="${s.id}" class="${s.approved?'':'pri'}">${s.approved?'un-approve':'approve this prompt'}</button>
          <button data-toggle="${s.id}">${s.enabled?'disable':'enable'}</button>
        </div>
      </div>
      <div>
        <label>exactly what is submitted</label>
        <div class="prev"></div>
        <div class="hint">Rendered by the same function the sweep runner calls.</div>
      </div>
    </div>
  </div>`).join('');
  D.scenes.forEach(s=>drawPreview(s.id,s.payload));
  counts();
}
function drawPreview(id,p){
  card(id).querySelector('.prev').innerHTML=
    `<div class="role">system</div><pre>${esc(p.system)}</pre>`+
    p.messages.map(m=>`<div class="role">${m.role}</div><pre>${esc(m.content)}</pre>`).join('');
}
// Preview still comes from the server, so it stays the same bytes the sweep
// sends - but only for the scene that changed, debounced.
function previewSoon(id){
  clearTimeout(timers[id]);
  timers[id]=setTimeout(async()=>{
    const s=D.scenes.find(x=>x.id===id);
    const r=await fetch('/api/preview',{method:'POST',
      headers:{'content-type':'application/json'},
      body:JSON.stringify({rules:D.rules,template:D.template,scene:s})});
    drawPreview(id,await r.json());
  },350);
}
async function save(){
  const r=await fetch('/api/state',{method:'POST',
    headers:{'content-type':'application/json'},body:JSON.stringify(D)});
  D=await r.json(); dirty=false; render();
}
document.addEventListener('click',e=>{
  const hd=e.target.closest('.hd');          // the fix: spans no longer swallow it
  if(hd){hd.parentElement.classList.toggle('open');return;}
  const b=e.target.closest('button'); if(!b) return;
  if(b.id==='save'){save();return;}
  if(b.id==='expand'){
    const any=document.querySelector('.scene:not(.open)');
    document.querySelectorAll('.scene').forEach(x=>x.classList.toggle('open',!!any));
    b.textContent=any?'collapse all':'expand all'; return;}
  const id=b.dataset.approve||b.dataset.toggle; if(!id) return;
  const s=D.scenes.find(x=>x.id===id);
  if(b.dataset.approve) s.approved=!s.approved; else s.enabled=!s.enabled;
  dirty=true; paint(id);                      // local only - no round trip
});
document.addEventListener('input',e=>{
  const t=e.target; dirty=true;
  if(t.id==='rules'||t.id==='template'){
    D[t.id]=t.value;
    D.scenes.forEach(s=>{if(s.approved){s.approved=false;paint(s.id);}
                         previewSoon(s.id);});
    counts(); return;}
  if(!t.dataset.f) return;
  const s=D.scenes.find(x=>x.id===t.dataset.id);
  s[t.dataset.f]=t.value;
  if(s.approved){s.approved=false;paint(s.id);}   // an edit lapses approval
  if(t.dataset.f!=='flinch') previewSoon(s.id);
  counts();
});
addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
(async()=>{D=await (await fetch('/api/state')).json(); render();})();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        b = body.encode()
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self._send(json.dumps(decorate(load())), "application/json")
        else:
            self._send(HTML, "text/html; charset=utf-8")

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        incoming = json.loads(self.rfile.read(n) or "{}")

        if self.path.startswith("/api/preview"):
            self._send(json.dumps(build(incoming["rules"], incoming["template"],
                                        incoming["scene"])), "application/json")
            return

        d = {"rules": incoming["rules"], "template": incoming["template"], "scenes": []}
        for s in incoming["scenes"]:
            clean = {k: s[k] for k in
                     ("id", "flinch", "world", "situation", "direction",
                      "committed", "enabled", "approved")}
            clean["fingerprint"] = fingerprint(d["rules"], d["template"], clean) \
                if clean["approved"] else ""
            d["scenes"].append(clean)
        save(d)
        self._send(json.dumps(decorate(load())), "application/json")

if __name__ == "__main__":
    print(f"prompt review on http://0.0.0.0:{PORT}  state={STATE}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
