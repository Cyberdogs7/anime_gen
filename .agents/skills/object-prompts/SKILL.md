# Skill: object-prompts

Write the recurring-object image prompts for one episode. Your prompt contains
the plan, show canon, this skill, and the card. Do not read or glob the show.

## Write

The plan lists the episode's narratively important objects. For each object,
write one concrete visual description: shape, materials, scale, markings,
wear/damage, and the show's style. Describe the object itself, not a scene or a
character.

Write `EPISODE_DIR/object-prompts.json` with the write tool:

```json
{
  "objects": [
    {"name": "exact plan object name", "description": "one paragraph of visual detail"}
  ]
}
```

Include every object in the plan; omit scenery and characters.

## Apply

Run the helper command printed in your Task section:

```
python "<absolute>/tools/apply_object_prompts.py" --card <card id>
```

It builds and saves the image prompts for the keyframe worker. If it reports a
missing object, add it and run it again.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).
If the objects cannot be written, finish `--status blocked` with the reason.
