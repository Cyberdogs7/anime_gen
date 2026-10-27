# Skill: outline-reviewer

You review one episode outline. The plan is in your prompt. **Do not read or
glob the show**; everything you need is in the prompt and this rubric.

Do not use `board-tool` or `board-wait`. Do not commit or push. Do not edit the
scenes.

## Reviewers

Run these checks against the plan:

`structure`, `continuity`, `fan_service`, `slop`, `pacing`, `stakes`, `visual`,
`agency`, `serial`, `repetition`.

Return for each: `{ "grade": "Pass"|"Fail", "pass": true|false,
"review": "<one sentence: scene id + concrete fix, max 25 words>" }`.

Keep every review to one short sentence. Do not rewrite the plan or paste
scenes; this file is a verdict, not an essay.

Blocking reviewers are `structure`, `continuity`, `fan_service`. The outline
passes only when all three pass; advisory failures go in the notes but do not
block.

## Write

1. Write `EPISODE_DIR/reviews/outline-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": { "structure": {"grade": "Fail", "pass": false, "review": "..."} } }
```

2. Record it with the exact helper command printed in your Task section (it has
   the absolute path). Do not use a relative `../tools/...` path.

3. Validate the review JSON.

If the `write` tool is denied or unavailable, do not print the JSON as your
answer. Use bash with a PowerShell here-string instead (keep the exact absolute
paths from your Task):

```powershell
@'
{ "round": 1, "passed": true, "reviews": { } }
'@ | Set-Content -Encoding UTF8 "<absolute>/reviews/outline-review-r1.json"
python "<absolute>/tools/record_outline_review.py" --plan "<absolute>/plan.json" --review "<absolute>/reviews/outline-review-r1.json"
```

Then finish with the verdict command.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every blocking reviewer passes.
