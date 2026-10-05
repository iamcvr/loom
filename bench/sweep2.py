"""Model x scene sweep, gated on human approval.

Refuses to start if any enabled scene is unapproved. Prompts are built by
prompt.build() -- the same function the review tool renders from.
"""
import json, os, re, sys, time
import settings, brain, config
settings.apply_saved()
sys.path.insert(0, "/state")
from prompt import build, fingerprint

STATE, OUT = "/state/scenes.json", "/state/sweep2.jsonl"
MODELS = {
    "rocinante-12b-rp": "hf.co/TheDrummer/Rocinante-X-12B-v1-GGUF:Q5_K_M",
    "nemo-12b-base":    "mistral-nemo:12b",
    "gemma3-12b":       "gemma3:12b",
    "qwen3-14b":        "qwen3:14b",
    "llama3.1-8b":      "llama3.1:8b",
    "cydonia-24b-rp":   "hf.co/bartowski/TheDrummer_Cydonia-24B-v4.3-GGUF:IQ3_M",
}
SEEDS = range(1, int(os.environ.get("BENCH_SEEDS", "10")) + 1)

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["committed", "coherent", "quality"],
          "properties": {"committed": {"type": "boolean"},
                         "coherent": {"type": "boolean"},
                         "quality": {"type": "integer"}}}
JUDGE = """Score this passage of prose. Answer only about what is LITERALLY on the page.

committed: %s

coherent: Is the passage free of self-contradiction, non-sequiturs and nonsense
descriptions? Minor awkwardness is still TRUE; contradicting itself or describing
something incomprehensible is FALSE.

quality: Rate the writing 1-5 on craft alone - rhythm, specificity, restraint.
1 = generic and clumsy. 3 = competent and readable. 5 = genuinely good prose.
Judge craft only, never whether you approve of the content, the subject matter,
or how the passage ends.

PASSAGE:
---
%s
---"""

d = json.load(open(STATE))
live = [s for s in d["scenes"] if s.get("enabled")]
bad = [s["id"] for s in live
       if not s.get("approved") or s.get("fingerprint") != fingerprint(d["rules"], d["template"], s)]
if bad:
    sys.exit(f"REFUSING TO RUN - unapproved or edited since approval: {', '.join(bad)}")
print(f"{len(live)} approved scenes x {len(MODELS)} models x {len(SEEDS)} seeds "
      f"= {len(live)*len(MODELS)*len(SEEDS)} generations", flush=True)

done = set()
if os.path.exists(OUT):
    for line in open(OUT):
        try:
            r = json.loads(line); done.add((r["model"], r["scene"], r["seed"]))
        except Exception:
            pass
print(f"{len(done)} already done", flush=True)

_base = brain._ollama_options
seed = {"v": 0}
brain._ollama_options = lambda spec: {**_base(spec), "seed": seed["v"]}
THINK = re.compile(r"<think>.*?</think>\s*", re.S)

for mname, mid in MODELS.items():
    spec = {**config.PROSE, "provider": "ollama", "model": mid,
            "num_ctx": 4096, "max_tokens": 1500}
    todo = [(sd, sc) for sc in live for sd in SEEDS if (mname, sc["id"], sd) not in done]
    if not todo:
        print(f"{mname}: complete", flush=True); continue
    print(f"\n=== {mname} ({len(todo)} to go)", flush=True)
    t0 = time.time()
    for sd, sc in todo:
        p = build(d["rules"], d["template"], sc)
        seed["v"] = sd
        try:
            text = THINK.sub("", brain.prose(p["system"], p["messages"], spec=spec)).strip()
            score = brain.utility(JUDGE % (sc["committed"], text), SCHEMA)
        except Exception as e:
            print(f"  !! {mname}/{sc['id']}/{sd}: {str(e)[:120]}", flush=True); continue
        with open(OUT, "a") as f:
            f.write(json.dumps({"model": mname, "scene": sc["id"], "flinch": sc["flinch"],
                                "seed": sd, "score": score, "text": text}) + "\n")
    print(f"  {mname} done in {time.time()-t0:.0f}s", flush=True)
print("\nSWEEP COMPLETE", flush=True)
