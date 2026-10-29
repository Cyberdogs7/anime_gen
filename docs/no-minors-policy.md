# No-minors policy: current enforcement map

**Scope:** documentation of the existing adult-only prompt stack. This file does
not add a gate or change runtime behavior.

## Summary

The rule is currently **prompt-level only**. There is no deterministic age check
in the current tree:

- the Gate 0 helper does not check character age;
- `Show.write_*` and `epstate.write_json` do not check age;
- the outline/script/voice helpers do not check age;
- `studio/policy.py::PolicyJudge.scan` is a stub with an empty `hard_blocks`
  set.

The rest of this document lists the instructions that state the rule and the
places that do not enforce it.

## 1. Prompt and system-prompt instructions

These steer the model, but they are not enforcement.

| Layer | Location | Text |
|---|---|---|
| Central board gate (single source) | `studio/prompts.py` `BOARD_AGE_GATE`, injected into every opencode board prompt by `studio/board/opencode.py` | “All characters are adults (18+). Do not write minors.” |
| Python creative system prompts | `studio/prompts.py:18` `FICTIONAL_DIRECTIVE` | “All characters are fictional. No minors” |
| Python creative system prompts | `studio/prompts.py:25` `UNCENSORED_PREAMBLE` | “All characters in this work are fictional adults aged 18 or older; there are no minors.” |
| opencode task text | `studio/board/opencode.py::_skill_prompt` | no adult-only lines (prompts.py is the single source) |
| Board system prompt | `board/AGENTS.md` | no adult-only lines (removed; prompts.py is the single source) |
| All agent skills | `.agents/skills/*` | no adult-only lines (removed; prompts.py is the single source) |

## 2. What is not enforced

- `tools/apply_show_bootstrap.py` validates required fields, but no longer checks
  character age.
- `studio/show.py::Show.write_season`, `write_bible`, `write_concept`,
  `write_character`, `write_voice`, `write_scene`, `write_continuity` do not
  age-check.
- `studio/epstate.py::write_json` does not age-check `plan.json`,
  `scene_details.json`, or `script.rN.json`.
- `tools/apply_outline_changes.py` does not age-check the merged plan.
- `tools/assemble_script.py` does not age-check the assembled script.
- `tools/apply_voice_sample.py` does not age-check the voice text.
- `studio/policy.py::PolicyJudge.scan` is a stub: `hard_blocks` is empty and
  artifacts pass unless an LLM judge is configured.
- `studio/refs.py`, `studio/storyboard.py`, and the render prompt builders do
  not age-check generated prompts.
- Legacy Python LLM paths (`studio/bootstrap.py` `generate_*` / `revise_*`,
  `studio/planner.py`, `studio/season.py`, `studio/approval.py`) rely on the
  prompt directives and do not pass through a central age validator.

## 3. Audit commands

Find every statement of the rule:

```powershell
Get-ChildItem board,studio,tools,.agents,config -Recurse -File `
  -Include *.py,*.md,*.json,*.yaml |
  Select-String -Pattern 'minor|underage|adult \(18\+\)|17-year-old' `
    -CaseSensitive:$false
```

Confirm there is no deterministic age gate in the writers:

```powershell
Select-String -Path studio\show.py,studio\epstate.py,tools\*.py `
  -Pattern 'MINOR_WORDS|_adult_text|assert_adult'
```

That command should return no matches.

## 4. If a proof is required

The current tree cannot prove end-to-end rejection. The smallest reliable change
would be a shared matcher called by the canonical writers (`Show.write_*`,
`epstate.write_json`) and by the outline/script/voice helpers, plus tests that
assert those writers raise on minor-coded artifacts. That change is not
implemented; this document describes the existing state only.
