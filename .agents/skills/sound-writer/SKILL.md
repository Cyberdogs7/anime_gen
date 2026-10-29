# Skill: sound-writer

You design the sound for every shot of one episode. Your prompt contains
the plan, this skill, and the card. You may read files under
`EPISODE_DIR/passes/` with the read tool (shots fragments); do not read or
glob the rest of the show.

Do not use `board-tool` or `board-wait`. Do not commit or push.

## Write one fragment per plan scene

For each plan scene, in plan order, write
`EPISODE_DIR/passes/<scene id>.sound.json` with the write tool:

```json
{
  "sound": {
    "s01_sh01": {"soundscape": "environment, weather, machinery, Foley", "music": "music cue, or 'none'"}
  }
}
```

One concern only: what is heard. Do not write dialogue lines, camera, or story.

Rules:

- Soundscape fits set, weather, machinery, and action; Foley for movement.
- Music is a cue or "none"; never lyrics in the soundscape field.
- Silent shots still get room tone unless true vacuum.

Write missing scene files first. If the card has notes, rewrite every
affected scene; leave unaffected existing files alone.

## Validate

Run the exact helper command printed in your Task section:

```
python "<absolute>/tools/check_pass.py" --card <card id> --pass sound
```

It validates every fragment. Fix flagged files and re-run.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).

If you cannot complete the card, finish with `--status blocked` and state why.
