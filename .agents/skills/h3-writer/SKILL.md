# Skill: h3-writer

You turn the finished fragment passes into the episode screenplay and the
compiled H3 prompts. Your prompt contains this skill and the card. You may
read files under `EPISODE_DIR/passes/` with the read tool to sanity-check
the merge inputs; do not read or glob the rest of the show.

Do not use `board-tool` or `board-wait`. Do not commit or push. Do not
write `script.r*.json` yourself and do not hand-edit scene files — the
helpers below own every write.

## Assemble

Run (bash), in order:

```
python "<absolute>/tools/merge_passes.py" --card <card id>
python "<absolute>/tools/compile_h3_prompts.py" --card <card id>
python "<absolute>/tools/assemble_script.py" --card <card id>
```

The merge builds `script_scenes/<sid>.json` from the fragments plus the
episode `object-prompts.json`; the compiler writes `h3_prompts.json`
(without rendering) and reports shots whose framed characters or sets lack
approved refs; the assembler validates, normalizes, writes the next
`script.rN.json`, and attaches it.

If the merge reports missing or invalid fragments, do NOT fix other passes'
files by hand: finish `--status retake` with notes naming the pass and
scene, so the right upstream card re-runs.

If the compiler reports ref gaps, do NOT invent refs: finish
`--status verified` anyway (the merge is mechanically complete) — the
reviewer fails the card with directives for the right upstream pass.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).

If a helper keeps failing, finish with `--status blocked` and state why.
