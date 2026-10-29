# Skill: action-reviewer

You review one episode's action fragments. The task gives the EPISODE_DIR
and card. Use the read tool for `EPISODE_DIR/passes/*.action.json` (plus
the matching shots fragments). Do not use `board-tool` or `board-wait`.
Do not commit or push. Do not edit the fragments.

## Checks (action only)

- One continuous, physically possible movement per shot.
- Pose continuity across cuts (or a cut that hides the change).
- Action matches the beat and any overlapping dialogue.

## Write

Write `EPISODE_DIR/reviews/action-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": { "s01": {"grade": "Fail", "pass": false, "review": "..."} } }
```

One short sentence per scene: scene id + concrete fix, max 25 words.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every check holds; put blocking failures in
`--notes` so the writer retake can fix them.
