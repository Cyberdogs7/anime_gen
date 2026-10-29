# Skill: narrative-writer

You write what happens in each scene of one episode. Your prompt contains
the plan, this skill, and the card. You may read files under
`EPISODE_DIR/passes/` with the read tool (location, cast, outfits, and
object fragments, for grounding); do not read or glob the rest of the show.

Do not use `board-tool` or `board-wait`. Do not commit or push.

## Write one fragment per plan scene

For each plan scene, in plan order, write
`EPISODE_DIR/passes/<scene id>.narrative.json` with the write tool:

```json
{
  "narrative": "2-5 sentences of what happens in this scene"
}
```

One concern only: the action of the scene in prose. Do not write dialogue
lines, shots, or camera.

Rules:

- Cover the plan beats fully: Setup, Change, Consequence all land.
- Add no new plot, characters, or locations beyond the plan + fragments.
- Name only on-screen characters doing things; keep it filmable.

Write missing scene files first. If the card has notes, rewrite every
affected scene; leave unaffected existing files alone.

## Validate

Run the exact helper command printed in your Task section:

```
python "<absolute>/tools/check_pass.py" --card <card id> --pass narrative
```

It validates every fragment. Fix flagged files and re-run.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).

If you cannot complete the card, finish with `--status blocked` and state why.
