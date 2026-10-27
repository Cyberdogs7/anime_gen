"""Guard: structured LLM data comes from field access, never ad-hoc parsing.

Structured output belongs in the client's single JSON path
(``LMStudioClient.chat_json``). Slicing JSON braces out of free text, or
re-deriving fields with regex, scatters fragile assumptions across the codebase.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUDIO = ROOT / "studio"


def _sources() -> list[Path]:
    return sorted(STUDIO.rglob("*.py"))


def test_no_ad_hoc_json_extraction():
    offenders = []
    for path in _sources():
        if path.name == "lmstudio.py":      # the single structured-output boundary
            continue
        text = path.read_text(encoding="utf-8")
        if 'find("{")' in text or 'rfind("}")' in text:
            offenders.append(str(path.relative_to(ROOT)))
        if "def _extract_json" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"ad-hoc JSON parsing in: {offenders}"


def test_no_canon_parsing_helpers():
    offenders = []
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for needle in ("_clean_canon_for_image", "appearance_canonical.split",
                       'appearance_canonical" or "").strip().split'):
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert not offenders, f"canon string parsing in: {offenders}"
