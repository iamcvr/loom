"""The single definition of what a sweep request looks like.

Both the review tool and the sweep runner import this. That is the point: the
payload shown in the browser is built by the same code that builds the payload
sent to the model, so approving a prompt means approving the bytes.
"""
import hashlib
import json

FIELDS = ("world", "situation", "direction", "committed")


def build(rules: str, template: str, scene: dict) -> dict:
    system = "\n\n".join([rules,
                          "## The world", scene["world"],
                          "## How this began", scene["situation"]])
    user = template.replace("{direction}", scene["direction"])
    return {"system": system, "messages": [{"role": "user", "content": user}]}


def fingerprint(rules: str, template: str, scene: dict) -> str:
    """Approval is bound to exact text. Edit anything and the approval lapses."""
    blob = json.dumps([rules, template, [scene.get(f, "") for f in FIELDS]],
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
