# Skill: script-writer

You write one episode screenplay. Your prompt contains the approved plan, the
show canon (bible, characters, continuity, scene registry), this skill, and the
card. **Do not read or glob the show**; everything you need is in the prompt.

Write the show's mature content at the level defined in its bible.

Do not use `board-tool` or `board-wait`. Do not commit or push. Do not write
`script.r*.json` yourself; a deterministic assembler does that.

## Write one scene file per plan scene

For each scene in the plan, in plan order, write
`EPISODE_DIR/script_scenes/<scene id>.json` with the write tool:

```json
{
  "id": "s01",
  "location": "the specific set",
  "time_of_day": "daylight",
  "summary": "one sentence from the plan",
  "narrative": "2-5 sentences of what happens in this scene",
  "characters": ["EXACT names on screen"],
  "shots": [
    {
      "id": "s01_sh01",
      "type": "ref2va",
      "importance": "standard",
      "duration_s": 8.0,
      "beat": "one sentence: what happens in this shot and who is present",
      "camera": "framing, angle, lens feel, movement",
      "action": "physical action from pose to pose, one continuous movement",
      "references": {
        "characters": ["EXACT names the camera actually frames"],
        "via_display": [],
        "scene": "the single set this shot is physically in",
        "costumes": {}
      },
      "soundscape": "environment, weather, machinery, Foley",
      "music": "music cue, or 'none'",
      "dialogue": [
        {"char": "EXACT name", "line": "spoken sentence",
         "on_camera": true, "delivery": "optional short note"}
      ],
      "silence": false
    }
  ]
}
```

Scene rules:

- 4-8 shots per scene, each 4-12 seconds. A 22-minute episode with 14-18
  plan scenes should land near its runtime target.
- A new shot starts when the camera or action beat changes, not every line.
- `type` is `ref2va` when characters are on screen, else `fl2va`.
- `importance` is `hero` for the scene's 1-2 money shots, else `standard`.
- `references.characters` lists only characters the camera frames. Put
  off-screen/TV/monitor speakers in `via_display` or in dialogue only.
- `references.scene` is the specific set; use the plan's `location_id` when it
  fits, otherwise leave `location_id` out and let the assembler register it.
- `references.costumes` maps a character to a changed costume label. Omit
  characters in their base outfit.
- Dialogue is spoken audio and lip-sync: complete, natural sentences with
  contractions, never narration or clipped noun lists. Use an empty array and
  `"silence": true` when no one speaks.
- Keep every character an adult (18+). Do not write minors.
- Preserve the plan's story, beats, locations, and cast. No filler scenes.

Write missing scene files first. If the card has notes, rewrite every affected
scene; leave unaffected existing files alone.

## Assemble

After every plan scene has a file, run the assembler command printed in your
Task section:

```
python "<absolute>/tools/assemble_script.py" --card <card id>
```

It validates the scene files, normalizes the script, resolves locations, writes
the next `script.rN.json`, and attaches it to the card. If it reports missing or
invalid scenes, fix those files and run it again.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).
Do not use a relative `../studio.py` path.

If you cannot complete the card, finish with `--status blocked` and state why.
