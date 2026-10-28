# Skill: outline-writer

You write one episode outline. Your prompt contains the show canon (bible,
characters, continuity, scene registry, season entry), this skill, the card,
and — if it exists — the current plan. **Do not read or glob the show**;
everything you need is in the prompt.

Do not use `board-tool` or `board-wait`. Do not commit or push. Do not write
`plan.json` yourself.

## If the prompt says NO PLAN EXISTS (create mode)

Write a complete new outline to `EPISODE_DIR/outline-changes.json`:

```json
{
  "episode": 1,
  "title": "Episode Title",
  "episode_type": "character",
  "threat_of_the_week": "none",
  "plot": "2-4 plain sentences: who wants what, the turn, the outcome.",
  "synopsis": "one paragraph synopsis",
  "characters": ["EXACT cast names on screen"],
  "cast": ["EXACT cast names"],
  "objects": ["narratively important props"],
  "plotline_updates": {"active_plotline_progress": "...", "dormant_plotline_beat": "..."},
  "scenes": [full scene objects, each with exactly 3 beats],
  "order": ["s01", "s02"]
}
```

- Write **14-18 scenes** for a 22-minute episode (60-100 seconds each).
- Expand each season-canon key beat into 2-3 distinct scenes.
- A new scene starts when location, time, or immediate goal changes.
- Vary locations; never more than two consecutive scenes in one place.
- Use short connective scenes (classroom, hallway, prep, aftermath).
- Every scene is one full object:
  `{ "id": "s01", "summary": "...", "characters": ["..."],
     "location_id": "registry id or empty string",
     "location": "specific set", "location_setting": "Krea 2 fragment for a new set",
     "time_of_day": "daylight",
     "beats": ["Setup: ...", "Change: ...", "Consequence: ..."] }`
- Keep ids unique; put the full order in `order`.
- Do not pad with filler; every scene advances plot, relationship, or threat.

## If a plan exists (revision mode)

Apply every CARD note. Write only the changed parts to the same file:

```json
{
  "plot": "optional",
  "synopsis": "optional",
  "scenes": [full changed scene objects],
  "remove_scene_ids": ["sNN"],
  "order": ["s01", "s02"]
}
```

- Scenes you do not list stay unchanged.
- Each listed scene is the full scene object with exactly three beats.
- Address the notes specifically; do not restate the whole plan.

## Apply

Run the apply command printed in your Task section (absolute helper path):

```
python "<absolute>/tools/apply_outline_changes.py" --card <card id>
```

It creates or merges `plan.json`, validates beats/order, attaches the season
entry, and removes the stale `plan_review`. If it reports an error, fix the
changes file and run it again.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).
Do not use a relative `../studio.py` path.

If you cannot complete the card, finish with `--status blocked` and state why.
