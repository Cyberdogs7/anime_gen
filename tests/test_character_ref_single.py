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


def test_character_ref_prompt_is_positive_only():
    p = prompts.character_ref_prompt(
        "Sable Voss",
        "23-year-old woman, 175cm sprinter-duelist build, black barbell in her left ear.")
    _assert_clean(p)
    assert p.startswith("Solo full-body portrait photo of one woman, Sable Voss,")
    assert "23-year-old woman" in p  # canon kept verbatim


def test_character_ref_prompt_gender_forms():
    assert "one woman" in prompts.character_ref_prompt("A", "tall athletic woman")
    assert "one man" in prompts.character_ref_prompt("B", "lean 5-foot-10 man")
    # pronouns in the canon must not decide gender; no noun -> person
    assert "one person" in prompts.character_ref_prompt("C", "grease in her knuckles")
    assert "one person" in prompts.character_ref_prompt("D", "")


def test_costume_ref_prompt_is_positive_only():
    p = prompts.costume_ref_prompt("Mira Kaine", "battle armor",
                                   "27-year-old tall athletic woman, warm tan skin.")
    _assert_clean(p)
    assert "battle armor" in p
    assert "one woman" in p


def test_clean_canon_rewrites_hedged_wardrobe():
    # "usually wears X" / "signature look is Y" describe several outfits ->
    # several figures. They must collapse to ONE present-tense outfit.
    c1 = prompts._clean_canon_for_image(
        "21-year-old lean man, usually in an unzipped oversized navy plug suit.")
    assert "usually" not in c1.lower()
    assert "wearing an unzipped oversized navy plug suit" in c1

    c2 = prompts._clean_canon_for_image(
        "23-year-old woman, signature look is a cut-down crimson plug suit.")
    assert "signature" not in c2.lower()
    assert "wearing a cut-down crimson plug suit" in c2


def test_clean_canon_drops_alternate_outfit_clause():
    canon = ("She normally wears the standard-issue Vixen Flight suit: sleek "
             "black leather. When not in uniform, she opts for casual attire, "
             "flaunting low-cut tops, paired with denim jeans or shorts.")
    out = prompts._clean_canon_for_image(canon)
    low = out.lower()
    assert "wearing the standard-issue vixen flight suit" in low
    # the second, alternate outfit is gone
    assert "when not in uniform" not in low
    assert "casual attire" not in low
    assert "denim" not in low
    # no leftover subject pronoun before the rewritten verb
    assert "she wearing" not in low


def test_ref_prompt_has_no_wardrobe_words():
    canon = ("A woman, usually in a black plug suit. When not in uniform, she "
             "opts for casual attire, low-cut tops and denim.")
    p = prompts.character_ref_prompt("Sable", canon)
    low = p.lower()
    for w in ("usually", "signature look", "when not in uniform", "casual attire"):
        assert w not in low, f"{w!r} survived into the image prompt: {p}"
    assert "wearing a black plug suit" in low


class _FakeLLM:
    def __init__(self, figures=None, explode=False):
        self.figures = figures
        self.explode = explode

    def chat(self, messages, **kwargs):
        assert kwargs.get("role") == "describer"
        if self.explode:
            raise RuntimeError("VLM down")
        return '{"figures": %d, "detail": "test"}' % self.figures


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
