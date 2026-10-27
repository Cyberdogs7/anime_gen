"""Role/lane config: every role resolves its own model, lane and tools."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.board.roles import get_role, lane_config, load_roles, role_for_phase
from studio.config import Config


def test_configured_roles_have_models_lanes_and_tools():
    cfg = Config(ROOT)
    roles = load_roles(cfg)
    assert {"showrunner", "outline_reviewer", "script_reviewer",
            "storyboard_planner", "image_exec", "render_exec",
            "qc_exec", "assembler_exec"} <= set(roles)
    for role in roles.values():
        assert role.model, f"{role.name} has no model"
        assert role.lane in lane_config(cfg)
        if role.name != "human" and not role.skill:
            assert role.tools
    assert roles["render_exec"].lane == "video"
    assert roles["render_exec"].kind == "exec"
    assert roles["qc_exec"].kind == "exec"
    assert "render_shot" in roles["render_exec"].tools
    assert "run_episode_qc" in roles["qc_exec"].tools


def test_role_model_override_wins_then_falls_back():
    cfg = Config(ROOT)
    roles = load_roles(cfg)
    before = roles["render_exec"].model
    cfg.data["agents"]["roles"]["render_exec"]["model"] = "special-model"
    assert get_role(cfg, "render_exec").model == "special-model"
    cfg.data["agents"]["roles"]["render_exec"]["model"] = ""
    cfg.data["llm"]["roles"]["render_exec"] = "llm-role-model"
    assert get_role(cfg, "render_exec").model == "llm-role-model"
    cfg.data["agents"]["roles"]["render_exec"]["model"] = before


def test_lane_capacity_is_configurable():
    cfg = Config(ROOT)
    cfg.data["agents"]["lanes"]["llm"]["capacity"] = 5
    assert lane_config(cfg)["llm"]["capacity"] == 5


def test_role_for_phase():
    card = {"role": "renderer", "reviewer": "render_reviewer"}
    assert role_for_phase(card, "produce") == "renderer"
    assert role_for_phase(card, "review") == "render_reviewer"


def test_outline_roles_are_skill_agents_not_board_tool_wrappers():
    roles = load_roles(Config(ROOT))
    stage = roles["outline_writer"]
    assert stage.kind == "exec"
    assert stage.model == "dolphin-2.9.3-mistral-nemo-12b"
    assert "outline_skeleton" in stage.tools and "outline_beats" in stage.tools

    writer = roles["outline_writer_agent"]
    assert writer.kind == "producer"
    assert writer.run_as_exec is False
    assert writer.agent == "studio-writer"
    assert writer.skill == "outline-writer"
    assert writer.tools == ()

    reviewer = roles["outline_reviewer"]
    assert reviewer.kind == "reviewer"
    assert reviewer.run_as_exec is False
    assert reviewer.agent == "studio-reviewer"
    assert reviewer.skill == "outline-reviewer"
    assert reviewer.tools == ()

    scene = roles["scene_writer"]
    assert scene.agent == "studio-writer"
    assert scene.skill == "scene-rewriter"

