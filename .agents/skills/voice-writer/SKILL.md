# Skill: voice-writer

Write one character voice sample. Your prompt contains the show canon, the
character sheet, and the card. Do not read or glob the show.

## Write

Write a 150-300 word first-person sample for the character named on the card:

- Written to be read aloud for voice casting: natural rhythm, breath, attitude,
  and the character's own vocabulary.
- Include a few lines of dialogue in character, not narration about the voice.
- Match the show's tone and the character's `voice_description`, `traits_for_llm`,
  and relationship to the cast.
- Plain text only; no headings, no markdown, no JSON wrapper.

Write it to the exact path printed in your Task section.

## Apply

Run the helper command printed in your Task section:

```
python "<absolute>/tools/apply_voice_sample.py" --card <card id>
```

It saves the text to the character sheet, resets the pending voice state, and
creates the deterministic TTS card. If it reports a short/invalid sample, rewrite
the file and run it again.

## Finish

Use the exact finish command printed in your prompt (absolute `studio.py` path).
If the sample cannot be written, finish `--status blocked` with the reason.
