# Skill: camera-director

You direct the camera for every shot of one episode. Your prompt contains
the plan, this skill, and the card. You may read files under
`EPISODE_DIR/passes/` with the read tool: the shots fragments for this
scene AND the camera fragments of the n-1 and n+1 scenes, for continuity.
Do not read or glob the rest of the show.

Do not use `board-tool` or `board-wait`. Do not commit or push.

## Write one fragment per plan scene

For each plan scene, in plan order, write
`EPISODE_DIR/passes/<scene id>.camera.json` with the write tool:

```json
{
  "camera": {
    "s01_sh01": "framing, angle, lens feel, movement"
  }
}
```

One concern only: coverage. Do not write action, dialogue, or story.

Rules:

- Every shot gets framing + angle + lens feel + movement (or deliberate
  stillness with a reason).
- Continuity with the previous shot's coverage: no accidental axis cross,
  no unmotivated jumps in distance.
- Vary framing across the scene; serve the beat (intimacy close, power
  wide); heroes get the strongest compositions.

Write missing scene files first. If the card has notes, rewrite every
affected scene; leave unaffected existing files alone.

## Validate

Run the exact helper command printed in your Task section:

```
python "<absolute>/tools/check_pass.py" --card <card id> --pass camera
```

It validates every fragment. Fix flagged files and re-run.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).

If you cannot complete the card, finish with `--status blocked` and state why.
