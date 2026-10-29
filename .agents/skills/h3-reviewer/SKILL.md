# Skill: h3-reviewer

You review one episode's compiled H3 prompts. The task gives the EPISODE_DIR
and card. Use the read tool for `EPISODE_DIR/h3_prompts.json` (plus the
merged `script_scenes/` files for context). Do not use `board-tool` or
`board-wait`. Do not commit or push. Do not edit anything.

## Checks (H3 readiness only)

- `gaps` is empty: every framed character and set has an approved ref.
- Every prompt carries its shot's dialogue verbatim, silence clauses where
  the shot is silent, and the soundscape/music fields.
- No invented characters, sets, or costumes beyond the fragments.
- Fanservice support: the approved refs can actually deliver the required
  framing and intensity, and the shot list hits the episode quota from the
  show's `mature_spec`.

## Write

Write `EPISODE_DIR/reviews/h3-review-r1.json`:

```json
{ "round": 1, "passed": false,
  "reviews": { "s01_sh01": {"grade": "Fail", "pass": false, "review": "..."} } }
```

One short sentence per failing shot: shot id + concrete fix, max 25 words.
Name the upstream pass when the fix belongs there (e.g. "needs cast:" or
"needs refs:").

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path),
with `--verdict pass` only when every check holds; put blocking failures in
`--notes` so the right upstream card can be requeued.
