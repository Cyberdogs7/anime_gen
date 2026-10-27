# Skill: show-bootstrap

You build the text artifacts for a new show. Your prompt contains the show
profile, the owner's brief, the current artifacts, and the card.

All characters are fictional adults aged 18 or older. Never write minors.

Do not use `board-tool` or `board-wait`. Do not commit or push.

## Create mode

Write all four artifact sets under `SHOW_DIR`:

### `concept.json`

```json
{
  "title": "str",
  "logline": "one sentence",
  "genre": ["str"],
  "tone": ["str"],
  "maturity": "the show profile's maturity"
}
```

### `bible.yaml`

```yaml
title: str
logline: str
genre: [str]
tone: [str]
world:
  setting: str
  rules: [str]
  established_facts: [str]
overall_plotline:
  id: slug
  name: str
  characters: [cast names]
  summary: the single continuous driving force of the whole show
  objects: [key props with visual continuity]
plotlines:
  - id: slug
    name: str
    characters: [cast names]
    status: active
    summary: standing thread
    objects: [key props]
style_guide: ONE sentence naming ONLY the rendering technique/linework
art_direction:
  camera: bible reference only
  lighting: bible reference only
  palette: bible reference only
runtime_target_s: 1320
mature_spec:
  quotient: light_ecchi | ecchi | explicit_ecchi
  quotas:
    service_scenes_per_episode: [min, max]
    reward_scenes_per_arc: int
  escalation: {schedule: str}
  tone_boundaries: [str]
  characters: {name: primary | support}
  scene_types: [str]
cast:
  - {name: str, role: str}
content_policy: the show profile's maturity
```

3-5 cast members. Define ONE overall_plotline plus standing plotlines. The
style_guide is only the rendering technique/linework; camera/lighting/palette
go in art_direction and are never pasted into generation prompts.

### `characters/<id>.yaml` (one per cast member)

```yaml
id: slug of the name
name: exact cast name
role: str
appearance_canonical: ONE fixed look written as an image prompt
gender: female | male | nonbinary | none
identity_canonical: the BODY ONLY (age, build, hair, eyes, marks), nothing worn
wardrobe_canonical: the canonical default outfit only, no body/face/hair
is_person: true
appearance_notes: immutable visual rules
personality: [str]
traits_for_llm: how they speak and behave
voice_sample_text: a 70-110 word first-person monologue in their own voice
h3_slot: "@char1" | "@char2" | "@char3"
voice:
  mode: designed
  voice_description: concrete age, register, texture, pace, delivery
```

Every character is an adult (18+). Write the exact cast name and its slug as
`id`; never substitute another character.

### `scenes/<id>.yaml` (5-8 recurring locations)

```yaml
id: slug
name: str
description: canonical one-sentence description
setting_prompt: Krea 2 background fragment, empty establishing shot, no people
```

## Revision mode

If the card names a `step` (`concept`, `bible`, `character`, `scenes`), rewrite
only that artifact from the notes. A concept change also refreshes the bible,
characters, and scenes to match; a bible change refreshes characters and scenes.
Keep approved artifacts that are not affected.

## Apply

Run the helper printed in your Task section:

```
python "<absolute>/tools/apply_show_bootstrap.py" --card <card id>
```

It validates the files, writes the bootstrap state, and attaches the artifacts.
If it reports an invalid/missing artifact, fix it and run it again.

## Finish

Use the exact finish command printed in your prompt. If you cannot complete the
artifacts, finish `--status blocked` with the reason.
