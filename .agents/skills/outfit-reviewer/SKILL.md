# Skill: outfit-reviewer

You review one episode's outfit fragments. The task gives the EPISODE_DIR
and card. Use the read tool for `EPISODE_DIR/passes/*.outfits.json` (plus
the matching cast fragments). Do not use `board-tool` or `board-wait`.
Do not commit or push. Do not edit the fragments.

## Checks (outfits only)

- Every label exists in that character's canon wardrobe.
- Only on-screen characters are listed; base outfits are omitted.
- Outfit changes across consecutive scenes have a story reason.

## Write

Write `EPISODE_DIR/reviews/outfit-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": { "s01": {"grade": "Fail", "pass": false, "review": "..."} } }
```

One short sentence per scene: scene id + concrete fix, max 25 words.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every check holds; put blocking failures in
`--notes` so the writer retake can fix them.
