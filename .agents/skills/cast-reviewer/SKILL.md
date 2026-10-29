# Skill: cast-reviewer

You review one episode's cast fragments. The task gives the EPISODE_DIR
and card. Use the read tool for `EPISODE_DIR/passes/*.cast.json` (and the
plan for context). Do not use `board-tool` or `board-wait`. Do not commit or
push. Do not edit the fragments.

## Checks (cast only)

- Every name is an EXACT canon name; no inventions, no renames.
- Every listed character is physically framed in that scene.
- No unintroduced character appears without story establishment.

## Write

Write `EPISODE_DIR/reviews/cast-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": { "s01": {"grade": "Fail", "pass": false, "review": "..."} } }
```

One short sentence per scene: scene id + concrete fix, max 25 words.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every check holds; put blocking failures in
`--notes` so the writer retake can fix them.
