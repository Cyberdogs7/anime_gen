"""Base-ref prompts stay single-figure: positive-only wording + VLM retry gate."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio import prompts
from studio.consistency import ref_figure_count, render_until_single

# Words that must NEVER appear in a ref prompt — the Turbo operating point
# (8 steps, cfg 1, zeroed negative) draws every banned noun, even inside a
# "no ..." clause, and every meta noun triggers a character sheet.
BANNED = ["reference", "design", "sheet", "turnaround", "panel", "grid",
          "duplicate", "twin", "clone", "mirror", "crowd", "views",
          "two-panel", "side-by-side", "inset", "multiple", "no second",
          "no duplicate", "exactly one head", "one pair of legs"]


def _assert_clean(p: str) -> None:
    low = p.lower()
    for w in BANNED:
        assert w not in low, f"banned token {w!r} in prompt: {p[:120]}"


def test_ref_style_clause_appends_style_guide_verbatim():
    # style_guide is already the structured rendering/linework field; refs use
    # it exactly as stored, with no parsing.
    style = "Modern anime rendering with crisp cel-shaded linework"
    assert prompts.ref_style_clause(style) == style + "."
    p = prompts.character_ref_prompt("Riku", "16-year-old lean pilot",
                                     style_guide=style, gender="male")
    assert p.endswith(style + ".")
    assert prompts.ref_style_clause("") == ""


def test_character_ref_prompt_is_positive_only():
    p = prompts.character_ref_prompt(
        "Sable Voss",
        "23-year-old tall sprinter-duelist build, black barbell in her left ear",
        gender="female")
    _assert_clean(p)
    assert p.startswith("Solo full-body portrait of one woman, Sable Voss,")
    assert "23-year-old tall sprinter-duelist build" in p


def test_character_ref_prompt_gender_forms():
    assert "one woman" in prompts.character_ref_prompt("A", gender="female")
    assert "one man" in prompts.character_ref_prompt("B", gender="male")
    # No structured gender -> neutral wording, never inferred from prose.
    assert "one person" in prompts.character_ref_prompt("C", gender="")
    assert "one person" in prompts.character_ref_prompt("D", gender="none")


def test_costume_ref_prompt_is_positive_only():
    p = prompts.costume_ref_prompt("Mira Kaine", "battle armor",
                                   "27-year-old tall athletic woman, warm tan skin",
                                   gender="female")
    _assert_clean(p)
    assert "battle armor" in p
    assert "one woman" in p


def test_ref_prompt_uses_structured_body_and_wardrobe():
    p = prompts.character_ref_prompt("Sable", "black bob, amber eyes",
                                     "black plug suit", gender="female")
    assert "black bob, amber eyes" in p
    assert "wearing black plug suit" in p


class _FakeLLM:
    def __init__(self, figures=None, explode=False):
        self.figures = figures
        self.explode = explode

    def chat(self, messages, **kwargs):
        assert kwargs.get("role") == "describer"
        if self.explode:
            raise RuntimeError("VLM down")
        return '{"figures": %d, "detail": "test"}' % self.figures

    def chat_json(self, messages, **kwargs):
        assert kwargs.get("role") == "describer"
        if self.explode:
            raise RuntimeError("VLM down")
        return {"figures": self.figures, "detail": "test"}


def _img(tmp_path: Path) -> Path:
    # Only existence matters: the VLM is faked and _downscale falls back to
    # the original bytes when ffmpeg cannot decode them.
    p = tmp_path / "ref.png"
    p.write_bytes(b"fake-image-bytes")
    return p


def test_ref_figure_count_single_multi_unknown(tmp_path):
    img = _img(tmp_path)
    assert ref_figure_count(_FakeLLM(figures=1), "m", img)["verdict"] == "single"
    r = ref_figure_count(_FakeLLM(figures=2), "m", img)
    assert r["verdict"] == "multi" and r["figures"] == 2
    # turnaround counts as two even though it is "the same character"
    assert ref_figure_count(_FakeLLM(figures=2), "m", img)["verdict"] == "multi"
    # VLM failure never blocks: unknown accepts the image
    assert ref_figure_count(_FakeLLM(explode=True), "m", img)["verdict"] == "unknown"
    assert ref_figure_count(_FakeLLM(figures=1), "m", tmp_path / "nope.png")["verdict"] == "unknown"


def test_render_until_single_retries_doubles(tmp_path):
    img = _img(tmp_path)
    calls = []

    def _render():
        calls.append(1)
        return True

    # first attempt double, second single -> 2 tries, accepted as single
    seq = [{"verdict": "multi", "figures": 2, "detail": "two"},
           {"verdict": "single", "figures": 1, "detail": "one"}]
    import studio.consistency as cons
    orig = cons.ref_figure_count
    cons.ref_figure_count = lambda llm, model, path: seq.pop(0)
    try:
        r = render_until_single(_render, img, llm=object(), model="m", label="t")
    finally:
        cons.ref_figure_count = orig
    assert r == {"ok": True, "single": True, "tries": 2, "figures": 1}
    assert len(calls) == 2


def test_render_until_single_accepts_stubborn_double(tmp_path):
    img = _img(tmp_path)
    import studio.consistency as cons
    orig = cons.ref_figure_count
    cons.ref_figure_count = lambda llm, model, path: {"verdict": "multi",
                                                      "figures": 2, "detail": "x"}
    try:
        r = render_until_single(lambda: True, img, llm=object(), model="m",
                                max_tries=3, label="t")
    finally:
        cons.ref_figure_count = orig
    # pipeline liveness wins: image accepted for human review after 3 tries
    assert r["ok"] is True and r["single"] is False and r["tries"] == 3


def test_render_until_single_no_vlm_accepts_first(tmp_path):
    img = _img(tmp_path)
    r = render_until_single(lambda: True, img, llm=None, model="")
    assert r == {"ok": True, "single": True, "tries": 1, "figures": 0}
