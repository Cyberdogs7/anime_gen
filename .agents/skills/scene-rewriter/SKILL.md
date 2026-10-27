# Skill: scene-rewriter

You rewrite one scene from the card feedback. The plan and card are in your
prompt. **Do not read or glob the show**; everything you need is in the prompt.

Do not use `board-tool` or `board-wait`. Do not commit or push. Do not write
`plan.json` yourself.

## What to change

Apply the CARD feedback (`payload.scene_id`, `payload.notes`, `notes`) to the
target scene only.

Write `EPISODE_DIR/outline-changes.json` with only that scene:

```json
{
  "scenes": [
    { "id": "sNN", "summary": "...", "characters": ["..."],
      "location_id": "...", "location": "...", "location_setting": "...",
      "time_of_day": "...",
      "beats": ["Setup: ...", "Change: ...", "Consequence: ..."] }
  ]
}
```

- Keep the id; keep location/time/cast unless the feedback explicitly changes
  them. Exactly three beats.
- Do not list or change any other scene.

## Apply

Run the apply command printed in your Task section (absolute helper path).
It merges the one scene, validates it, removes the stale `plan_review`, and
writes `plan.json`.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).
Do not use a relative `../studio.py` path.

If the feedback cannot be applied, finish `--status blocked` and state why.
