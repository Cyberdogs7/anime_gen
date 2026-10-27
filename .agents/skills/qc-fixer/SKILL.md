# Skill: qc-fixer

Fix the shots that failed QC in one episode. Your prompt contains the card's
failure list, the scene-file paths, and the skill. Use the `read` tool for those
files. Do not use `board-tool` or `board-wait`. Do not commit or push.

All characters are fictional adults aged 18 or older. Never write minors.

## Fix

1. Read the scene file for each failed shot id.
2. Rewrite that shot's `action`, `camera`, `references`, `dialogue`, or
   `soundscape` to address the reported QC/VLM issue. Keep the shot id, scene
   id, story beat, and every other shot intact.
3. Write the full revised scene file back to the same path.
4. Run the assembler so the next script revision contains the fixes:

```
python "<absolute>/tools/assemble_script.py" --card <episode script card id>
```

5. Requeue the failed renders:

```
python "<absolute>/tools/requeue_qc_fixes.py" --card <this fixer card id>
```

The requeue helper creates the new render cards without any LLM call.

## Finish

Use the exact finish command printed in your prompt. If a shot cannot be fixed,
finish `--status blocked` with the reason.
