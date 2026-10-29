# Skill: outfit-writer

You dress one episode's cast. Your prompt contains the plan, this skill,
and the card. You may read files under `EPISODE_DIR/passes/` with the read
tool (cast + location fragments, and the previous scene's outfits fragment
for continuity); do not read or glob the rest of the show.

Do not use `board-tool` or `board-wait`. Do not commit or push.

## Write one fragment per plan scene

For each plan scene, in plan order, write
`EPISODE_DIR/passes/<scene id>.outfits.json` with the write tool:

```json
{
  "costumes": { "Character Name": "costume label" }
}
```

One concern only: WHAT each on-screen character wears. Omit characters in
their base outfit. Do not write cast, dialogue, or story.

Rules:

- Labels must exist in the character's canon wardrobe.
- Check the previous scene's outfits fragment: a changed outfit needs a
  story reason (time pass, location change, deliberate change).
- Every cast-fragment character is either listed here or in base.

Write missing scene files first. If the card has notes, rewrite every
affected scene; leave unaffected existing files alone.

## Validate

Run the exact helper command printed in your Task section:

```
python "<absolute>/tools/check_pass.py" --card <card id> --pass outfits
```

It validates every fragment (schema + on-screen refs). Fix flagged files
and re-run.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).

If you cannot complete the card, finish with `--status blocked` and state why.
