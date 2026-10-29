# Skill: shot-splitter

You split each scene of one episode into shots. Your prompt contains the
plan, this skill, and the card. You may read files under
`EPISODE_DIR/passes/` with the read tool (narrative + dialogue fragments);
do not read or glob the rest of the show.

Do not use `board-tool` or `board-wait`. Do not commit or push.

## Write one fragment per plan scene

For each plan scene, in plan order, write
`EPISODE_DIR/passes/<scene id>.shots.json` with the write tool:

```json
{
  "shots": [
    {
      "id": "s01_sh01",
      "beat": "one sentence: what happens in this shot and who is present",
      "duration_s": 8.0,
      "type": "ref2va",
      "importance": "standard",
      "characters": ["EXACT names the camera frames"],
      "line_refs": [0, 1]
    }
  ]
}
```

One concern only: shot boundaries. Do not write camera, action, or dialogue
lines — reference dialogue by number only.

Rules:

- 4-8 shots per scene, each 4-12 seconds. A new shot starts when the camera
  or action beat changes, not every line.
- `type` is `ref2va` when characters are on screen, else `fl2v`.
- `importance` is `hero` for the scene's 1-2 money shots, else `standard`.
- Every dialogue line number appears in exactly one shot's `line_refs`;
  silent shots get `[]`.
- `characters` lists only framed characters (off-screen speakers stay in
  dialogue only).

Write missing scene files first. If the card has notes, rewrite every
affected scene; leave unaffected existing files alone.

## Validate

Run the exact helper command printed in your Task section:

```
python "<absolute>/tools/check_pass.py" --card <card id> --pass shots
```

It validates every fragment (schema + line refs). Fix flagged files
and re-run.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).

If you cannot complete the card, finish with `--status blocked` and state why.
