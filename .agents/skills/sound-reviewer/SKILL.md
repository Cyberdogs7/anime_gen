# Skill: sound-reviewer

You review one episode's sound fragments. The task gives the EPISODE_DIR
and card. Use the read tool for `EPISODE_DIR/passes/*.sound.json` (plus
the matching shots fragments). Do not use `board-tool` or `board-wait`.
Do not commit or push. Do not edit the fragments.

## Checks (sound only)

- Soundscape fits set, weather, machinery, and action; Foley where movement.
- Music cues exist or are explicitly "none"; no lyrics in soundscape.
- Silent shots carry room tone (unless true vacuum).

## Write

Write `EPISODE_DIR/reviews/sound-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": { "s01": {"grade": "Fail", "pass": false, "review": "..."} } }
```

One short sentence per scene: scene id + concrete fix, max 25 words.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every check holds; put blocking failures in
`--notes` so the writer retake can fix them.
