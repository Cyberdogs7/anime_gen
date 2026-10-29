# Skill: dialogue-writer

You write only the spoken lines for one episode, scene by scene. Your prompt
contains the plan, this skill, and the card. You may read files under
`EPISODE_DIR/passes/` with the read tool (narrative fragments, for what is
said and why); do not read or glob the rest of the show.

Do not use `board-tool` or `board-wait`. Do not commit or push.

## Write one fragment per plan scene

For each plan scene, in plan order, write
`EPISODE_DIR/passes/<scene id>.dialogue.json` with the write tool:

```json
{
  "dialogue": [
    {"n": 0, "char": "EXACT name", "line": "spoken sentence"}
  ],
  "silence": false
}
```

One concern only: spoken words. Do not write shots, camera, or story.

Rules:

- Complete, natural sentences with contractions, never narration or clipped
  noun lists. Speakable out loud at a natural pace.
- Number lines sequentially from 0; every line motivated by the narrative.
- A scene with no one speaking gets `"dialogue": []` and `"silence": true`.
- Write the show's mature content at the level defined in its bible.

Write missing scene files first. If the card has notes, rewrite every
affected scene; leave unaffected existing files alone.

## Validate

Run the exact helper command printed in your Task section:

```
python "<absolute>/tools/check_pass.py" --card <card id> --pass dialogue
```

It validates every fragment (schema + canon names). Fix flagged files
and re-run.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).

If you cannot complete the card, finish with `--status blocked` and state why.
