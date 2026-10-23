# proof-of-done

**Your Claude Code agent can't say "done" until it has shown a passing check in the same turn.**

A tiny Stop hook plus a skill. Python 3.9+, standard library only, no network, MIT.

## Install (under 1 minute)

1. Copy or clone this folder anywhere, e.g. `~/tools/proof-of-done`.
2. Add the hook to your Claude Code settings (`~/.claude/settings.json` or
   `.claude/settings.json`). `settings.example.json` has the snippet; this tool
   never edits your settings itself:

   ```json
   {
     "hooks": {
       "Stop": [
         { "hooks": [ { "type": "command",
           "command": "python3 /ABSOLUTE/PATH/TO/proof-of-done/hooks/proof_guard.py" } ] }
       ]
     }
   }
   ```
3. Optional: copy `skills/proof-of-done/` to `~/.claude/skills/` so the agent
   knows the report format before the hook has to ask.

Kill switch: `PROOF_GUARD_OFF=1`.

Protect the guard itself: add `proof.json` and the `hooks/proof_guard.py` path to the protected paths of your permission or guard setup (for example the `paths` list of agent-guard-hooks `protected_paths`), otherwise an agent can edit the config that polices it:

```json
{ "protected_paths": { "paths": ["proof.json", "proof-of-done/hooks/", ".claude/settings*.json"] } }
```

## What it does

At the end of every turn the hook reads the session transcript and looks at the
last assistant turn:

1. Does the final message claim success ("done", "fixed", "works", "all tests pass", "ready"...)?
2. Did this turn run a check, i.e. a Bash command like `pytest`, `npm test`,
   `cargo test`, `go test`, `make test`, `curl`, `py_compile`, ... that returned a
   result, or take/read a screenshot, after the last edit?

Claim without evidence: the hook exits with code 2, and Claude Code feeds
`claim without evidence: run a check and include its output` back to the agent.
It blocks at most once per turn (`stop_hook_active` lets the second attempt through),
and fails open on any parsing error, so it can never wedge your session.

### Before / after

Before:

> Fixed the date parsing bug. It works now.

After (the agent was forced to run a check):

```
Claim:      parse_date() handles ISO strings with timezone offsets
Evidence:   python3 -m pytest tests/test_dates.py -q -> 14 passed in 0.22s
Confidence: 85% - not tested against locale-specific formats
```

## Configuration

`proof.json` (next to the hook's parent folder, or the path in `PROOF_DONE_CONFIG`). A `proof.json` in the current working directory is **not** loaded, so a project you open cannot weaken the guard by shipping its own config:

| key | meaning |
| --- | --- |
| `claims` | words and phrases that count as a success claim |
| `change_claims` | claims that need evidence even in a turn with no tool calls ("I fixed it") |
| `enable_polish`, `claims_pl` | optional Polish claim words |
| `evidence_commands` | command names that count as a check |
| `evidence_tools` | tool-name fragments that count (screenshots) |
| `ignore_turns_without_changes` | turns that edited nothing are not policed for generic claims (default `true`) |
| `evidence_after_last_change` | evidence must come after the final edit (default `true`) |

## What does NOT work / limitations

- It is a text heuristic. It matches words in the final message and command names
  in tool calls. It does not understand what the code does.
- An agent can bypass it trivially, for example `pytest --version`, or
  `python3 -c "print('12 passed')"` followed by "all good". Plain `echo`/`printf`
  are rejected, but that only stops the laziest cases.
- Known limitation: masked exit codes. `pytest || true` is detected (`|| true`, `|| :`, `|| exit 0` do not count as evidence), but other tricks such as `pytest; true`, `pytest | cat` or a wrapper script that swallows the status are not.
- It does not check that the check passed, only that one ran and returned. A
  failing test run counts as evidence; the agent is expected to report it honestly.
- It does not check that the check is relevant to the change.
- Claims phrased outside the word list ("shipped", "LGTM") are not detected.
  Negation handling is crude (a few words before the claim).
- Edits made by shell redirection (`> file`) are not detected as changes, so such a
  turn may be treated as "no code changes".
- The transcript format is Claude Code's JSONL. If it changes shape the hook
  fails open (allows the stop) rather than blocking.

This is discipline for your agent, not cryptographic proof. It raises the cost of
unverified "done" from zero to one command.

## Testing

```bash
python3 -m unittest discover -s tests
```

46 tests: claim with evidence, claim without evidence, questions, loop guard,
broken JSON, different transcript shapes, CLI exit codes.

## License

MIT, see `LICENSE`.
