# Skill: shot-reviewer

You review one episode's shot fragments. The task gives the EPISODE_DIR
and card. Use the read tool for `EPISODE_DIR/passes/*.shots.json` (plus the
matching dialogue fragments). Do not use `board-tool` or `board-wait`.
Do not commit or push. Do not edit the fragments.

## Checks (shot splits only)

- 4-8 shots per scene, 4-12 seconds each; durations fit the episode runtime.
- New shots only on camera/action beats; line numbers covered exactly once.
- `ref2va` exactly when characters are framed; 1-2 heroes per scene.

## Write

Write `EPISODE_DIR/reviews/shot-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": { "s01": {"grade": "Fail", "pass": false, "review": "..."} } }
```

One short sentence per scene: scene id + concrete fix, max 25 words.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every check holds; put blocking failures in
`--notes` so the writer retake can fix them.
