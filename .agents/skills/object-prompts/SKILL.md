# Skill: object-prompts

You describe one episode's recurring objects, scene by scene. Your prompt
contains the plan, this skill, and the card. You may read files under
`EPISODE_DIR/passes/` with the read tool (location + cast fragments, for
scale and handling); do not read or glob the rest of the show.

Do not use `board-tool` or `board-wait`. Do not commit or push.

## Write one fragment per plan scene

For each plan scene, in plan order, write
`EPISODE_DIR/passes/<scene id>.objects.json` with the write tool:

```json
{
  "objects": [
    {"name": "exact plan object name", "description": "one paragraph of visual detail"}
  ]
}
```

One concern only: props that matter. Describe the object's shape, materials,
scale, markings and wear — not a scene, not a character. Omit scenery and
characters. A scene with no important props gets an empty list.

Rules:

- Names match the plan's object names where they overlap.
- One paragraph of concrete visual detail per object.

Write missing scene files first. If the card has notes, rewrite every
affected scene; leave unaffected existing files alone.

## Validate

Run the exact helper command printed in your Task section:

```
python "<absolute>/tools/check_pass.py" --card <card id> --pass objects
```

It validates every fragment. Fix flagged files and re-run.

The h3 card merges the fragments into the episode object prompts; do not
write `object-prompts.json` yourself.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).

If you cannot complete the card, finish with `--status blocked` and state why.
