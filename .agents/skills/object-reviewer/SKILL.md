# Skill: object-reviewer

You review one episode's object fragments. The task gives the EPISODE_DIR
and card. Use the read tool for `EPISODE_DIR/passes/*.objects.json` (and
the plan for the object list). Do not use `board-tool` or `board-wait`.
Do not commit or push. Do not edit the fragments.

## Checks (objects only)

- Every plan object appears with a concrete visual description.
- Descriptions cover shape, materials, scale, markings/wear — no scenery,
  no characters, no story.
- No duplicate objects across scenes under different names.

## Write

Write `EPISODE_DIR/reviews/object-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": { "s01": {"grade": "Fail", "pass": false, "review": "..."} } }
```

One short sentence per scene: scene id + concrete fix, max 25 words.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every check holds; put blocking failures in
`--notes` so the writer retake can fix them.
