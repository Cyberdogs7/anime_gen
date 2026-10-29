# Show Ideas (Gate 0)

Pitch 5 distinct executions of the owner's brief. You are the fast small
model in a refine loop: draft HOT and bold, then critique your own batch
like a hostile editor, rewrite only the weak pitches, and repeat until the
batch is sharp. Sample hot, judge cold.

## Output

Write `shows/<id>/ideas.json` with the write tool (absolute path is in your
task prompt):

```json
{"brief": str, "n": int, "created": int, "updated": int,
 "pitches": [{"id": "p1", "title": str, "show_id": str, "logline": str,
              "premise": "2-3 sentences", "genre": [str], "tone": [str],
              "hook": "one sentence", "maturity": str,
              "status": "proposed", "selected_at": null}]}
```

- 5 pitches per run (`p1`..`p5`; on a re-pitch continue numbering).
- `show_id`: kebab-case slug derived from that pitch's own title
  (`Salt Meridian` -> `salt-meridian`), <= 40 chars, lowercase
  alphanumerics and hyphens only, unique across the whole archive.
- Every pitch must clearly derive from the owner's brief: same core
  premise, world, and tone — a different angle on it, never an unrelated
  idea. Vary the plot focus, the emphasized relationship, or the central
  conflict.
- Titles distinct and evocative. No AI-slop tells.

## Loop (max 4 validation rounds, then finish with what passes)

Your FIRST action must be step 1. Do not write pitches in chat — the
archive file is the only draft surface.

1. **Draft (bash, deterministic).** Run:
   `python tools/draft_show_ideas.py --card <card id>`
   It makes the single draft LLM call and writes `ideas.json`. Be bold in
   spirit, but this step is mechanical: run the command, read nothing else.
   Never `type`, `cat`, `read`, or `glob` the archive — your bash box
   allows only `python` helper commands, and any other shell command is
   denied. A missing `ideas.json` is EXPECTED, not an error: it means you
   have not run the draft command yet. Run it; do not inspect first.
2. **Validate.** Run (bash):
   `python tools/check_show_ideas.py --show <id>`
   It reports per-pitch failures: bad schema, bad slug, duplicates
   within the batch or against the archive. Exit code is nonzero on failure.
3. **Rewrite flagged pitches only.** Use the edit tool on exactly the
   pitch ids the checker names, keeping the good ones untouched. Changed
   titles get fresh slugs (still unique). Re-run the checker.
4. **Critic pass.** Reread the full batch as a hostile editor: if two
   pitches feel like the same show, if one ignores the brief, if a title
   is flat — rewrite the weaker one and re-run the checker.
5. **Finish.** Copy the `board-finish` command from your task prompt
   EXACTLY — character for character. Its script path ends in
   `anime\studio.py`. There is no `tools\studio.py`; do not invent one by
   mixing the helper paths with the finish path. Finish `verified` with a
   one-line summary, only when the checker passes. If 4 rounds still leave
   failures, finish `retake` with the remaining failure list in the notes
   — never finish verified on a failing batch.
