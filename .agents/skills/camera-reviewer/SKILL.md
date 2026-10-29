# Skill: camera-reviewer

You review one episode's camera fragments. The task gives the EPISODE_DIR
and card. Use the read tool for `EPISODE_DIR/passes/*.camera.json` (plus
the matching shots fragments). Do not use `board-tool` or `board-wait`.
Do not commit or push. Do not edit the fragments.

## Checks (camera only)

- Every shot has framing + angle + lens feel + movement (or reasoned stillness).
- No axis crosses or unmotivated distance jumps across cuts.
- Coverage serves the beat; heroes get the strongest compositions.
- Fanservice delivery: the coverage delivers the intended framing and
  intensity per service shot, with escalation intact across the scene.

## Write

Write `EPISODE_DIR/reviews/camera-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": { "s01": {"grade": "Fail", "pass": false, "review": "..."} } }
```

One short sentence per scene: scene id + concrete fix, max 25 words.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every check holds; put blocking failures in
`--notes` so the director retake can fix them.
