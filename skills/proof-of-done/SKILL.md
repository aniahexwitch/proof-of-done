---
name: proof-of-done
description: Use before telling the user that work is done, fixed, working, passing or ready. Requires a real check (tests, build, curl, screenshot) in the same turn and a Claim / Evidence / Confidence report.
---

# proof-of-done

Rule: you may not announce success without evidence produced in this turn.

## Before you say "done"

1. Run the cheapest real check that exercises your change: the test suite,
   `py_compile`, a build, a `curl` against the running service, or a
   screenshot of the UI.
2. Read the output. If it failed, say so and keep working.
3. Report using the template below.

"It should work" and "import OK" are not evidence. A check you did not run
is not a check.

## Report template

```
Claim:      <one sentence: what is now true>
Evidence:   <exact command> -> <key line of its output (e.g. "12 passed")>
Confidence: <0-100%> - <what was NOT verified>
```

## Rules of thumb

- Evidence must come after your last edit. Re-run after any further change.
- Several claims, several evidence lines.
- Cannot test (no environment, needs credentials)? Say "UNVERIFIED" in the
  Claim line and state what the user must run. Never present it as done.
- A failing check that you report honestly is a good outcome. A hidden one is not.

## If the Stop hook blocks you

The hook `proof_guard.py` found a success claim and no check in this turn.
Run a check now, then restate the result in the template above.
