# Skill: action-writer

You choreograph the physical action for every shot of one episode. Your
prompt contains the plan, this skill, and the card. You may read files
under `EPISODE_DIR/passes/` with the read tool (shots + camera fragments);
do not read or glob the rest of the show.

Do not use `board-tool` or `board-wait`. Do not commit or push.

## Write one fragment per plan scene

For each plan scene, in plan order, write
`EPISODE_DIR/passes/<scene id>.action.json` with the write tool:

```json
{
  "action": {
    "s01_sh01": "physical action from pose to pose, one continuous movement"
  }
}
```

One concern only: bodies in motion. Do not write camera, dialogue, or story.

Rules:

- One continuous movement per shot, pose to pose; physically possible.
- Continuous across cuts: the end pose of one shot is the start pose of
  the next, unless the cut hides the change.
- Matches the beat and the dialogue happening over it.

Write missing scene files first. If the card has notes, rewrite every
affected scene; leave unaffected existing files alone.

## Validate

Run the exact helper command printed in your Task section:

```
python "<absolute>/tools/check_pass.py" --card <card id> --pass action
```

It validates every fragment. Fix flagged files and re-run.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).

If you cannot complete the card, finish with `--status blocked` and state why.
