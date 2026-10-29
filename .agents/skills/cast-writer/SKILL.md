# Skill: cast-writer

You cast one episode's scenes. Your prompt contains the plan, this skill,
and the card. You may read files under `EPISODE_DIR/passes/` with the read
tool (the location fragments, for set logic); do not read or glob the rest
of the show.

Do not use `board-tool` or `board-wait`. Do not commit or push.

## Write one fragment per plan scene

For each plan scene, in plan order, write
`EPISODE_DIR/passes/<scene id>.cast.json` with the write tool:

```json
{
  "characters": ["EXACT names on screen"]
}
```

One concern only: WHO is physically on screen. Do not write outfits,
dialogue, or story.

Rules:

- EXACT canon names only; never invent or rename.
- List only characters the camera frames. Off-screen voices do not belong
  here (dialogue carries them later).
- Stay within the plan's scene cast; if the plan missed someone the story
  needs, add them and say so in the finish summary.

Write missing scene files first. If the card has notes, rewrite every
affected scene; leave unaffected existing files alone.

## Validate

Run the exact helper command printed in your Task section:

```
python "<absolute>/tools/check_pass.py" --card <card id> --pass cast
```

It validates every fragment (schema + canon names). Fix flagged files
and re-run.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).

If you cannot complete the card, finish with `--status blocked` and state why.
