# Skill: dialogue-reviewer

You review one episode's dialogue fragments. The task gives the EPISODE_DIR
and card. Use the read tool for `EPISODE_DIR/passes/*.dialogue.json` (and
the matching narrative fragments). Do not use `board-tool` or `board-wait`.
Do not commit or push. Do not edit the fragments.

## Checks (dialogue only)

- Every line is speakable: complete natural sentences, contractions, no
  narration; lengths fit the scene's runtime.
- Every line is motivated by the narrative; no filler quips.
- Silence flags are correct: empty dialogue if and only if silent.
- Fanservice delivery: the lines carry the planned service beats at the
  bible's maturity level — no flat or bait-and-switch delivery.

## Write

Write `EPISODE_DIR/reviews/dialogue-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": { "s01": {"grade": "Fail", "pass": false, "review": "..."} } }
```

One short sentence per scene: scene id + concrete fix, max 25 words.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every check holds; put blocking failures in
`--notes` so the writer retake can fix them.
