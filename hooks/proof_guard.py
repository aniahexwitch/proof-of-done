#!/usr/bin/env python3
"""proof-of-done: a Claude Code Stop hook.

Blocks the agent from ending a turn with a success claim ("done", "fixed",
"all tests pass") unless that turn also contains evidence: a test/check
command run through the Bash tool (with its result), or a screenshot.

Contract (Claude Code Stop hook):
  stdin  : JSON with `transcript_path` and `stop_hook_active`
  exit 0 : allow the agent to stop
  exit 2 : block; stderr is fed back to the agent

Design rules: standard library only, no network, no shell, fail-open on any
error, and block at most once per turn.
"""
import json
import os
import re
import sys

MESSAGE = (
    "proof-of-done: claim without evidence: run a check and include its output. "
    "Your final message claims success ({claims}) but this turn has no "
    "proof (test run, build, curl, py_compile, screenshot...). Run one now and "
    "report it as Claim / Evidence / Confidence."
)

DEFAULT_CONFIG = {
    "claims": [
        "done", "fixed", "works", "working", "passing", "implemented",
        "all tests pass", "tests pass", "ready", "complete", "completed",
        "resolved", "should now work",
    ],
    # Subset of claim-like words that assert the agent *changed* something.
    # These need evidence even in a turn with no tool calls at all.
    "change_claims": [
        "fixed", "implemented", "all tests pass", "tests pass", "resolved",
        "refactored", "completed",
    ],
    "claims_pl": [
        "gotowe", "zrobione", "naprawione", "dziala", "działa",
        "zaimplementowane", "wszystkie testy przechodza",
        "wszystkie testy przechodzą", "rozwiazane", "rozwiązane",
    ],
    "enable_polish": False,
    "evidence_commands": [
        "pytest", "unittest", "npm test", "npm run test", "yarn test",
        "pnpm test", "cargo test", "cargo check", "cargo build", "go test",
        "go vet", "go build", "make test", "make check", "tox", "nox",
        "curl", "py_compile", "mypy", "ruff", "flake8", "eslint", "tsc",
        "jest", "vitest", "mocha", "rspec", "phpunit", "gradle test",
        "mvn test", "dotnet test", "ctest", "bats",
    ],
    # Tool names (substring, case-insensitive) that count as evidence.
    "evidence_tools": ["screenshot", "browser_snapshot"],
    # Reading an image file counts as "looked at the result".
    "evidence_read_extensions": [".png", ".jpg", ".jpeg", ".webp", ".gif"],
    # Commands whose first word is one of these never count as evidence
    # (stops the laziest bypass: `echo "pytest passed"`).
    "non_evidence_first_words": ["echo", "printf", "true", ":", "cat", "type"],
    "edit_tools": ["Edit", "Write", "MultiEdit", "NotebookEdit"],
    "bash_change_patterns": [
        r"\bsed\s+(-[a-zA-Z]*i|--in-place)", r"\btee\b", r"\bgit\s+apply\b",
        r"\bpatch\b", r"\bperl\s+-[a-zA-Z]*i",
    ],
    # True: a turn with no code changes and no change-claim passes.
    "ignore_turns_without_changes": True,
    # True: evidence must come at or after the last change in the turn.
    "evidence_after_last_change": True,
}

_FENCE = re.compile(r"```.*?```", re.S)
_MASKED = re.compile(r"\|\|\s*(?:true|:|exit\s+0)(?!\w)")
_NEGATION = re.compile(
    r"(\bnot|n't|\bnever|\bno|\bwithout|\bnie)\W+(?:\w+\W+){0,2}$", re.I
)


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    candidates = []
    if path:
        candidates.append(path)
    elif os.environ.get("PROOF_DONE_CONFIG"):
        candidates.append(os.environ["PROOF_DONE_CONFIG"])
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        # Only next to the hook. A proof.json in the working directory is NOT
        # loaded: a repo under review could ship one that weakens the guard.
        candidates.append(os.path.join(os.path.dirname(here), "proof.json"))
    for cand in candidates:
        try:
            with open(cand, "r", encoding="utf-8") as fh:
                user = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(user, dict):
            for key, val in user.items():
                if key in cfg and isinstance(val, type(cfg[key])):
                    cfg[key] = val
        break
    return cfg


# ---------------------------------------------------------------- transcript

def _role(entry):
    msg = entry.get("message")
    if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
        return msg["role"]
    if entry.get("role") in ("user", "assistant"):
        return entry["role"]
    if entry.get("type") in ("user", "assistant"):
        return entry["type"]
    return None


def _blocks(entry):
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if content is None:
        content = entry.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        out = []
        for item in content:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str):
                out.append({"type": "text", "text": item})
        return out
    return []


def read_entries(path):
    entries = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("isSidechain") or obj.get("isMeta"):
                continue
            role = _role(obj)
            if role:
                entries.append((role, _blocks(obj)))
    return entries


def _is_real_user_prompt(blocks):
    for blk in blocks:
        if blk.get("type") == "text" and str(blk.get("text", "")).strip():
            return True
    return False


def last_turn(entries):
    """Entries after the last real user prompt (tool results do not count)."""
    start = 0
    for i, (role, blocks) in enumerate(entries):
        if role == "user" and _is_real_user_prompt(blocks):
            start = i + 1
    return entries[start:]


# ------------------------------------------------------------------ analysis

def final_text(turn):
    for role, blocks in reversed(turn):
        if role != "assistant":
            continue
        texts = [str(b.get("text", "")) for b in blocks if b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t.strip())
        if joined:
            return joined
    return ""


def _phrase_regex(phrase):
    return re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", re.I)


def find_claims(text, phrases):
    """Return the claim phrases found (outside code fences, not negated)."""
    text = _FENCE.sub(" ", text)
    found = []
    for phrase in phrases:
        for m in _phrase_regex(phrase).finditer(text):
            before = text[max(0, m.start() - 30):m.start()]
            if _NEGATION.search(before):
                continue
            found.append(phrase)
            break
    return found


def _tool_uses(turn):
    """Yield (name, input_dict, tool_use_id) in order."""
    for role, blocks in turn:
        if role != "assistant":
            continue
        for b in blocks:
            if b.get("type") == "tool_use":
                inp = b.get("input")
                yield (b.get("name") or "",
                       inp if isinstance(inp, dict) else {}, b.get("id"))


def _result_ids(turn):
    ids = set()
    for role, blocks in turn:
        if role != "user":
            continue
        for b in blocks:
            if b.get("type") == "tool_result" and b.get("tool_use_id"):
                ids.add(b["tool_use_id"])
    return ids


def _first_word(command):
    parts = command.strip().split()
    return os.path.basename(parts[0]) if parts else ""


def _is_evidence_command(command, cfg):
    if not isinstance(command, str) or not command.strip():
        return False
    if _first_word(command) in cfg["non_evidence_first_words"]:
        return False
    # `pytest || true` always exits 0, so it proves nothing.
    if _MASKED.search(command):
        return False
    for pat in cfg["evidence_commands"]:
        if re.search(r"(?<![\w-])" + re.escape(pat) + r"(?!\w)", command):
            return True
    return False


def analyze_tools(turn, cfg):
    """Return (n_tools, last_change_idx, evidence_idxs)."""
    results = _result_ids(turn)
    n = 0
    last_change = -1
    evidence = []
    edit_tools = set(cfg["edit_tools"])
    for idx, (name, inp, tid) in enumerate(_tool_uses(turn)):
        n += 1
        has_result = (tid in results) if tid else True
        if name in edit_tools:
            last_change = idx
            continue
        if name == "Bash":
            cmd = inp.get("command", "")
            if isinstance(cmd, str) and any(
                    re.search(p, cmd) for p in cfg["bash_change_patterns"]):
                last_change = idx
            if has_result and _is_evidence_command(cmd, cfg):
                evidence.append(idx)
            continue
        lname = name.lower()
        if has_result and any(t.lower() in lname for t in cfg["evidence_tools"]):
            evidence.append(idx)
        elif has_result and name == "Read":
            path = str(inp.get("file_path", "")).lower()
            if any(path.endswith(ext) for ext in cfg["evidence_read_extensions"]):
                evidence.append(idx)
    return n, last_change, evidence


def evaluate(payload, cfg=None):
    """Return (exit_code, message)."""
    cfg = cfg or load_config()
    if not isinstance(payload, dict):
        return 0, ""
    if payload.get("stop_hook_active"):
        return 0, ""
    path = payload.get("transcript_path")
    if not path or not os.path.isfile(path):
        return 0, ""
    turn = last_turn(read_entries(path))
    text = final_text(turn)
    if not text:
        return 0, ""
    phrases = list(cfg["claims"])
    if cfg.get("enable_polish"):
        phrases += cfg["claims_pl"]
    claims = find_claims(text, phrases)
    if not claims:
        return 0, ""
    n_tools, last_change, evidence = analyze_tools(turn, cfg)
    if cfg["ignore_turns_without_changes"] and last_change < 0:
        change_claims = {c.lower() for c in cfg["change_claims"]}
        if n_tools > 0 or not any(c.lower() in change_claims for c in claims):
            return 0, ""
    if cfg["evidence_after_last_change"] and last_change >= 0:
        evidence = [i for i in evidence if i >= last_change]
    if evidence:
        return 0, ""
    return 2, MESSAGE.format(claims=", ".join(claims[:4]))


def main():
    if os.environ.get("PROOF_GUARD_OFF") == "1":
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        code, msg = evaluate(payload)
    except Exception:  # fail-open by design
        return 0
    if code == 2:
        sys.stderr.write(msg + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
