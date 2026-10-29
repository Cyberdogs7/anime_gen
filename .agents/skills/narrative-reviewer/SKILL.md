# Skill: narrative-reviewer

You review one episode's narrative fragments. The task gives the EPISODE_DIR
and card. Use the read tool for `EPISODE_DIR/passes/*.narrative.json` (and
the plan beats for context). Do not use `board-tool` or `board-wait`.
Do not commit or push. Do not edit the fragments.

## Checks (narrative only)

- Setup, Change, and Consequence all land in 2-5 sentences.
- Nothing beyond the plan beats: no new plot, characters, or locations.
- Every beat is filmable with the scene's cast and set.

## Write

Write `EPISODE_DIR/reviews/narrative-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": { "s01": {"grade": "Fail", "pass": false, "review": "..."} } }
```

One short sentence per scene: scene id + concrete fix, max 25 words.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every check holds; put blocking failures in
`--notes` so the writer retake can fix them.
