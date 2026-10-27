# Skill: script-reviewer

You review one episode screenplay. The task gives the latest scene files and the
show canon paths. Use the `read` tool for those files. Do not use `board-tool`
or `board-wait`. Do not commit or push. Do not edit the script.

All characters are fictional adults aged 18 or older. Never approve minors.

## Reviewers

Review the scene files against the plan/canon:

`structure`, `continuity`, `fan_service`, `slop`, `voice`, `exposition`,
`pacing`, `stakes`, `visual`, `agency`, `serial`, `repetition`.

Return for each: `{ "grade": "Pass"|"Fail", "pass": true|false,
"review": "<scene id + one concrete fix, max 25 words>" }`.

Blocking reviewers are `structure`, `continuity`, `fan_service`. The script
passes only when all three pass; advisory failures go in the notes but do not
block.

## Write

Write `EPISODE_DIR/reviews/script-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": {
    "structure": {"grade": "Fail", "pass": false, "review": "s04: ..."}
  } }
```

Use one short sentence per reviewer. This file is a verdict, not an essay.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every blocking reviewer passes. Put the blocking
failures in `--notes` so the writer retake can fix them.
