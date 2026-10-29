# Skill: location-writer

You set one episode's locations. Your prompt contains the plan, this skill,
and the card. You may read files under `EPISODE_DIR/passes/` with the read
tool; do not read or glob the rest of the show.

Do not use `board-tool` or `board-wait`. Do not commit or push.

## Write one fragment per plan scene

For each plan scene, in plan order, write
`EPISODE_DIR/passes/<scene id>.location.json` with the write tool:

```json
{
  "location_id": "registry id, or empty string for a new set",
  "location": "the specific set",
  "time_of_day": "daylight"
}
```

One concern only: WHERE and WHEN. Do not write cast, outfits, or story.

Rules:

- Prefer a registry set that fits; only leave `location_id` empty when the
  plan genuinely needs a new place (the plan's `location_setting` then
  describes it for the registry pass).
- `time_of_day` must fit the story (a noon scene cannot be midnight).
- Never more than two consecutive scenes in one place; vary sets.
- Keep every id from the plan's `order`; do not rename scenes.

Write missing scene files first. If the card has notes, rewrite every
affected scene; leave unaffected existing files alone.

## Validate

Run the exact helper command printed in your Task section:

```
python "<absolute>/tools/check_pass.py" --card <card id> --pass locations
```

It validates every fragment (schema + registry refs). Fix flagged files
and re-run.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).

If you cannot complete the card, finish with `--status blocked` and state why.
