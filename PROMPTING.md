# PROMPTING — Image & Audio Reference Workflow

How H3 shot prompts are built when a shot carries **image references** (`<Picture N>`)
and **audio references** (`<Audio N>`). This is the `ref2va` ("reference-to-video")
path that gives the studio character identity and voice-timbre consistency per shot.

Authoritative code:
- `studio/render.py` — `compile_shot_prompt()` builds the prompt (six sections).
- `studio/h3.py` — `build_h3_ref2va_workflow()` wires refs as sockets on `MiniMaxH3ReferenceToVideo`.
- `studio/prompts.py` — `h3_rewrite_prompt()` optional LLM expansion.
- `studio/compile/h3_prompt.py` — the older deterministic multi-shot compiler.

---

## 1. What the workflow is

One shot = one H3 generation. The shot's refs are uploaded to ComfyUI and connected
to the node's `ref_image_0..8` / `ref_audio_0..2` sockets **in connection order**.
The node auto-labels them `<Picture 1>…<Picture 9>` and `<Audio 1>…<Audio 3>` in that
same order, and the prompt references those tags directly. There is no other link
between the prompt and the actual refs — get the order or the tags wrong and H3
uses the wrong face or wrong voice.

Two kinds of reference:

| Kind | Tag | Feeds from | Limit |
|---|---|---|---|
| Image ref | `<Picture N>` | character refs, object refs, storyboard keyframe | ≤ 9 total |
| Audio ref | `<Audio N>` | per-character voice samples (raw wav) | ≤ 3 |

Plus the derived identity tag:

| Tag | Meaning |
|---|---|
| `<Subject N>` | the reusable person/identity. Bound to its `<Picture N>` in `subject_definitions`. Every appearance of a character in dialogue/action uses `<Subject N>`, never a bare name. |
| `(Sx)` | speaker index **within the shot**, from spoken-line order — first spoken line is `S1`. Silent beats produce no `(Sx)`. |

---

## 2. Reference ordering (critical)

The pipeline builds the image list in a fixed order (`render.py:_render_shot`):

1. **Character refs first**, one per on-screen character, in the order returned by
   `_shot_refs()` (`studio/storyboard.py:106`). This is the identity anchor — its
   `<Picture N>` number **must** match the `names` order used to compile `<Subject N>`.
2. **Object refs** (recurring props matched from the shot's action text, `_shot_object_refs()`).
3. **The shot's storyboard keyframe** (`runs/EP##/storyboard/<sid>.png`) last — it is
   the composition anchor, not an identity anchor.

Rule of thumb: **who goes in `<Subject N>` goes first.** If a character ref is missing
or fails to upload, the pipeline skips it rather than reordering the rest, so the
`<Picture N>` numbers still line up with `names`. Never hand-edit the order or you
will decouple subjects from their pictures.

Audio refs are appended **after** all image refs, one raw voice sample per speaking
character, in dialogue order, up to 3. Audio refs are only attached when the shot
also has image refs (the model requires them together).

---

## 3. Image references

### Character refs (identity)
Generated once at bootstrap under reference-sheet discipline
(`studio/bootstrap.py:300`):

> `Anime character reference portrait of {name}. {appearance_canonical}. Full body,
> front view, neutral standing pose, plain studio background, clean lineart,
> consistent character design, high quality.` — aspect 3:4.

Deliberately staged: neutral pose, plain background, minimal accessories. That makes
the identity conditioning strong, so the ref stays reusable across every shot and
costume. Approved refs live in `shows/<id>/characters/<cid>/refs/`; the first
approved image is the one H3 uses (`_shot_character_ref`, `render.py:63`).

- **Use the base ref for identity.** Costume changes are declared per shot via
  `references.costumes`; the wardrobe pass keeps the ref list stable so a costume
  swap never changes `<Picture N>` numbering.
- **One ref per character.** A character in the ref list with no uploaded ref simply
  doesn't appear as a subject — check the render log if a character is missing.

### Object refs (recurring props)
Slug-matched from capitalized multi-word phrases in the shot's action/camera text
against `runs/EP##/objects/<slug>.png` (`_shot_object_refs`). Camera phrases are
excluded. Objects pull lightly — they guide the prop, never the character.
Reference prompts demand the object **alone and person-free** (`generate_object_refs` /
`regenerate_object_ref`): `THE OBJECT ALONE … NO PEOPLE, NO HUMANS, NO CHARACTERS,
NO HANDS, NO FACES, NO FIGURES` — a person in a prop ref pollutes the prop's identity
conditioning the same way a wrong face pollutes a character ref.

**Object feedback uses the same loop as costumes/char-refs**: a reject-with-notes does
NOT raw-append the notes and re-render. It LLM-revises the object's current stored
prompt (`revise_object_prompt`) and persists it to `runs/EP##/objects/prompts.json`, so
successive rejects accumulate corrections. If you wire a new reject/regenerate path,
follow this pattern (see HANDOFF §5).

### Storyboard keyframe (composition)
Every shot's first-frame storyboard still is appended as the final image ref. It
anchors composition/pose; identity still comes from the character refs ahead of it.

---

## 4. Audio references

- Source: the **approved voice sample** for each speaking character,
  `assets/voice/<cid>_voice.wav` (`_voice_sample_path`, `render.py:85`).
- Uploaded **raw as-is — no conversion** (the proven example workflow does the same).
- Attached per speaking character in dialogue order; **max 3 audio refs per shot**.
- Only characters who both (a) speak in the shot **and** (b) are in `names` get an
  audio ref.
- **Silent shots attach no audio refs.** A shot with no `<d>` lines is silent and
  carries no `<Audio N>`. Attaching a voice ref to a silent shot would tell H3 to
  voice something — exactly the gibberish-audio failure mode.

The audio ref is a **voice-timbre** reference, not a line read. H3 keeps the character's
vocal quality but speaks the lines written in the `<d>` tokens. The prompt states this
explicitly in both `subject_definitions` and `retention_analysis` (below).

> Because H3 renders video and audio jointly, the audio ref conditions the video too —
> this is what produces **in-model lip-sync** for on-camera speakers.

---

## 5. Prompt anatomy — the six sections

`compile_shot_prompt()` emits exactly six sections, in this order, per MiniMax's
`VIDEO_PROMPT_WRITING_GUIDE_ref_en.md`. When a section has no content it is `N/A`,
never omitted. Tags (`<Picture N>`, `<Audio N>`, `<Subject N>`) are used verbatim.

### 5.1 `subject_definitions`
Define every reusable identity, then bind each audio ref to its speaker:

```
<Subject 1> is Kiyo, shown in <Picture 1>, mid-20s woman, silver bob cut...
<Audio 1> is the voice-timbre reference for <Subject 1> (S1).
```

- Appearance comes from the character's `appearance_canonical`, lowercased first
  letter, trailing period stripped.
- The `(S1)` here is the shot-local speaker id (first line spoken → `S1`).

### 5.2 `summary`
One line starting with the task-type prefix — the prefix changes with what refs exist:

```
[reference generation + audio reference] Kiyo gets the package on the rooftop.
```

| Refs present | Prefix |
|---|---|
| images only | `[reference generation] …` |
| images + audio | `[reference generation + audio reference] …` |

The body is the script/scene summary, capped at 300 chars.

### 5.3 `retention_analysis`
Confirms what is preserved, one line per subject and per audio ref:

```
<Subject 1> (appears in [Shot 1]): fully_preserved - Kiyo's identity, clothing and appearance are retained exactly as shown.
<Audio 1>: reference - its vocal timbre guides the dialogue delivery of <Subject 1> without copying the original signal.
```

Note: the audio line carries **no `(Sx)`** here — the `(Sx)` appears only in
`subject_definitions` and `detailed_description`.

### 5.4 `detailed_description`
Style opening line, then the shot beat:

```
{style_guide}
[Shot 1] <Subject 1> (Kiyo), mid-20s woman, silver bob cut. Kiyo lands on the rooftop,
looks over the district. wide establishing, slow push-in. <Subject 1> (S1) says, using
the voice timbre referenced from <Audio 1>, <d>[English] The package. Where is it?</d>
```

- Composition: `action` then `camera`, joined.
- **Every spoken line lives inside `<d>[English] …</d>`.** Never put dialogue words
  outside the token — H3 reads bare words as narration, and a spoken word floating
  in the prose can also get synthesized twice (once as <d> speech, once as
  "narration" voice). What the writer writes inside the token is the ONLY audio the
  model has for that line.
- Dialogue is bound to the speaker and, when an audio ref exists, to that ref.
  Delivery and camera/off-screen notes go OUTSIDE the token, before it:

  | Case | Template |
  |---|---|
  | speaker + audio ref | `<Subject N> (Sx) says, using the voice timbre referenced from <Audio N>, <d>[English] line</d>` |
  | speaker + audio ref + delivery | `<Subject N> (Sx) says in a low, breathy voice, using the voice timbre referenced from <Audio N>, <d>[English] line</d>` |
  | speaker, no audio ref | `<Subject N> (Sx) says, <d>[English] line</d>` |
  | off-screen / voiceover | `<Subject N> (Sx) says in an off-screen voiceover, <d>[English] line</d> while their lips remain completely closed.` |
  | no subject | `A voice says, <d>[English] line</d>` |

- **Speaker IDs** `(S1)`, `(S2)` come from the order of *spoken* lines in the shot
  (first spoken line → `S1`). A stage direction like `(Grunting)` is NOT a spoken
  line and is dropped before the prompt is compiled. Lines that are only a grunt /
  internal note / stage direction are FORBIDDEN — H3 will try to synthesize them
  and you get gibberish speech.

### 5.5 `overall_soundscape`
Ambience and physical action sounds only — `wind, distant sirens`. `N/A` means
**complete silence**, so it is emitted for a silent shot and NEVER for a shot that
has dialogue. When a shot has spoken lines but no soundscape entry, the compiler
writes `No background ambience; the only voices are the spoken lines in
detailed_description.` instead of `N/A`.

### 5.6 `non_diegetic_music`
Music cue, e.g. `bass drone`. `N/A` when absent. Independent of the soundscape:
a shot can be diegetically silent (`overall_soundscape: N/A`) and still carry a
score.

### 5.7 Dialogue & silence discipline

H3 renders **video and audio jointly**, and its prompt grammar treats prose as
narration. Consequences, from MiniMax's official guides (§4.4 of the Video Prompt
Writing Guide, §5.4 of the Ref2VA guide), the [Luma H3 guide](https://lumalabs.ai/learning-center/articles/minimax-h3-prompting-guide)
and the community [cheat sheet](https://www.reddit.com/r/StableDiffusion/comments/1vnis23/minimax_h3_prompting_cheat_sheet_this_structure/):

1. **Every shot states its speech state explicitly.** It is either SPOKEN (every
   line a `<d>[English] …</d>` token bound to `<Subject N> (Sx)`) or SILENT
   (an explicit `No one speaks; all characters remain silent.` clause). A silent
   shot is a normal, intentional beat — action inserts and reaction shots are
   routinely silent.
2. **Silence must be prompted.** An un-prompted shot with no `<d>` tokens and no
   silence statement makes H3 guess the audio track, and it hallucinates voice —
   the gibberish-speech failure. The compiled prompt always resolves the ambiguity
   one way or the other.
3. **The shot's data carries the same decision.** The dialogue pass writes
   `silence: true` with an empty `dialogue` array for silent beats and real lines
   for speaking beats; `_normalize` derives `silence` from the actual dialogue so
   a stale flag can never contradict the compiled prompt.
4. **Never fake speech.** Grunts, sighs, internal notes and parenthetical stage
   directions are not lines. If a beat needs a vocalized non-word, it belongs in
   `overall_soundscape` (e.g. `a low grunt, soft footsteps`, per the guide §4.6),
   not in a `<d>` token.
5. **Keep lines short enough for the clip.** The Luma guide: dialogue is part of
   the performance — the visual action, spoken line, and sound all compete for the
   same 4–15 s. A 10.125 s shot comfortably carries 1–2 short exchanges.
6. **Voiceover/lip-sync.** `on_camera: false` lines become off-screen voiceover
   and must state the on-screen character's lips stay closed; `on_camera: true`
   lines keep H3's in-model lip-sync through the `<Audio N>` conditioning.
7. **Write for the ear at script time.** The DIALOGUE WRITER pass (and the
   single-pass `script_prompt`) write lines that become synthesized audio. Lines
   are SPOKEN, not read: 2-20 words, one thought per line, conversational rhythm,
   read-aloud clean; numbers/abbreviations spelled as spoken; a ~10 s shot carries
   ~2-3 short lines (~20-30 words total). Tone the words don't carry goes in
   `delivery` — never in ALL-CAPS or stacked punctuation inside `line`.
8. **Voice anchors.** The DIALOGUE WRITER anchors each speaking character's
   register with 1-2 sample lines from their personality/traits before writing
   the scene, so every line stays voice-distinct (reinforced downstream by the
   `voice` reviewer's same-voice and unspeakable-line checks).

---

## 6. The optional LLM rewrite pass

When `pipeline.h3_rewrite_prompt` is enabled (`render.py:357`), the deterministic
prompt above is handed to a local LLM (`h3_rewrite_prompt`, `prompts.py:1551`) which
expands it into a rich 250–500 word production brief. Its hard constraints:

- **Keep every reference tag verbatim** — `<Picture N>`, `<Audio N>`, `<Subject N>`
  unchanged.
- **Keep the exact dialogue words** — only re-wrap/expand around them.
- Dialogue stays inside `<d>[English] …</d>`, bound to the speaking character's tag.
- **Silence stays silent.** When the shot has no dialogue, the rewriter must keep
  it a silent shot — state `No one speaks; all characters remain silent.` and
  NEVER invent, add, or suggest speech. This is why the rewriter's user message
  now includes `SHOT DIALOGUE`, `SHOT SOUNDSCAPE`, `SHOT MUSIC` and a
  `SHOT SILENCE FLAG` (see `h3_rewrite_prompt`, `prompts.py`).
- Returns plain text; on any failure the deterministic prompt is used unchanged.

The LLM runs GGUF/NF4 off the primary GPU so it does not compete with the H3 render
for VRAM. Its job is enrichment (composition, lighting, camera, motion, placement) —
never re-numbering or re-voicing.

---

## 7. The deterministic multi-shot compiler (other path)

`compile_h3_prompt()` (`studio/compile/h3_prompt.py`) is the non-ref path for
prompting a full timeline (t2v/`fl2va`). Reference-relevant differences:

- `subject_definitions:` is a single space-joined line, not per-line.
- `retention_analysis:` is generic: `Keep the identity, face and clothing of
  <Subject 1> and <Subject 2> consistent across every shot.`
- Shots are numbered `[Shot N]`, later shots carry strictly increasing
  `At MM:SS.mmm` cut timestamps computed from cumulative durations.
- Dialogue in list form emits `<Subject N> speaks, <d>[English] line</d>`.

`render.py` does **not** use this path for reference shots — the Full-Reference
six-section form in §5 is what ships to `ref2va`. Keep the two forms straight: the
six-section form for ref2va shots, the timeline form for fl2va/keyframe batches.

---

## 8. Rules checklist

- [ ] Character refs are the first image refs; their order matches `names`/`<Subject N>`.
- [ ] ≤ 9 image refs, ≤ 3 audio refs, ≤ 3 speaking characters with audio.
- [ ] Audio refs only on shots that also have image refs.
- [ ] Voice samples uploaded raw (no conversion).
- [ ] `subject_definitions` binds every `<Subject N>` to its `<Picture N>`.
- [ ] `summary` prefix matches the ref set: `[reference generation + audio reference]`.
- [ ] `retention_analysis` audio lines have **no** `(Sx)`.
- [ ] Every spoken line inside `<d>[English] …</d>`, bound to `<Subject N> (Sx)` and, when
      present, `referenced from <Audio N>`; delivery/off-screen notes live outside the token.
- [ ] Off-screen/voiceover lines append `while their lips remain completely closed.`
- [ ] Every silent shot is explicit: `silence: true` + empty `dialogue` in the data,
      and `No one speaks; all characters remain silent.` in `detailed_description`.
- [ ] No stage directions, grunts, or internal notes as `dialogue` lines.
- [ ] `overall_soundscape` / `non_diegetic_music` = `N/A` when empty (never omitted);
      `overall_soundscape: N/A` is emitted ONLY for truly silent shots, never
      for a shot that has dialogue.
- [ ] LLM rewrite preserves tags verbatim and dialogue words exactly, keeps silent shots
      silent, or is dropped.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Character has the wrong face | `<Picture N>` / `names` order mismatch; ref missing/upload failed | Re-check `_shot_refs` order; confirm the ref file exists and `refs.json` status is `real` |
| Character doesn't appear at all | Character not in `references.characters`; no approved ref | Fix the shot's cast; approve/regenerate the ref image |
| Wrong vocal timbre or no lip-sync | Audio ref missing/not attached | Speaker must be in `names`, have a voice sample, and the shot must have image refs |
| Bare words spoken as narration | Dialogue outside `<d>` tokens | Re-wrap lines inside `<d>[English] …</d>` |
| Gibberish / invented speech on a silent shot | Shot had no `<d>` lines and no silence clause — H3 guessed the audio track | Compile emits `No one speaks; all characters remain silent.`; ensure `silence: true` in data / the rewriter did not invent dialogue |
| Grunt / stage direction spoken aloud | A `(Grunting)`-style "line" reached the prompt | Filter padded lines (they cannot be compiled or reach TTS now); rewrite the beat as action in `action` or sound in `soundscape`, never in `dialogue` |
| Dialogue spoken twice / narration voice over a line | Words duplicated outside `<d>` | Keep the exact words ONLY inside `<d>`; remove them from the prose |
| Missing subject/audio in output | Rewriter dropped a tag | Disable `pipeline.h3_rewrite_prompt` or tighten the rewrite system prompt |
| Ref rejected by renderer | File too large / bad format | Re-export the wav/png; keep refs at H3-native sizes |

---

## 10. Sources

- **MiniMax H3 prompt-writing skill** — the authoritative guides the six-section
  form ships from. [repo](https://github.com/MiniMax-AI/MiniMax-H3) includes
  `skills/h3-prompt-writing/references/ref-en.txt` (full-reference / Ref2VA:
  `subject_definitions`, speaker/`(Sx)` rules §5.4, `overall_soundscape` N/A =
  complete silence §6) and `base-en.txt` (T2VA/FL2VA: speaker IDs §4.4,
  voiceover + lips-closed, shots/cuts).
- [MiniMax H3 prompting cheat sheet](https://www.reddit.com/r/StableDiffusion/comments/1vnis23/minimax_h3_prompting_cheat_sheet_this_structure/)
  — community distillation of the same structure: establish scene → observable
  action → camera → cuts → dialogue as performance → soundscape → music.
- [Luma's MiniMax H3 prompting guide](https://lumalabs.ai/learning-center/articles/minimax-h3-prompting-guide)
  — "Treat dialogue as part of the performance" (§5): write exact quoted lines
  with delivery info, keep them short enough for the clip; make sound and music
  explicit layers.
