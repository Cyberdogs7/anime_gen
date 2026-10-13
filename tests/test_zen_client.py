"""Zen remote-first routing: remote by default, local on refusal/failure.

Local dolphin stays for the highly explicit scenes remote models refuse.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from studio.clients.lmstudio import (
    LMStudioClient, RefusalError, _has_images, is_refusal,
)


def _zen_client(**kw):
    kw.setdefault("api_key", "test-key")
    kw.setdefault("remote_url", "https://opencode.ai/zen/v1")
    kw.setdefault("remote_model", "kimi-k2.5")
    return LMStudioClient("http://127.0.0.1:1234/v1", **kw)


def _zen_client_with(roles=None, local_roles=None, vision=None, model="kimi-k2.5"):
    c = _zen_client()
    c._zen = {
        "url": "https://opencode.ai/zen/v1", "api_key": "test-key",
        "model": model, "model_map": {},
        "roles": roles or {},
        "local_roles": local_roles or {},
        "responses_models": ["muse-spark-*", "gpt-*", "grok-*"],
        "vision": {"enabled": True, "model": "deepseek-v4-flash-vision-exp",
                   "frames_per_video": 6, "frame_dim": 512, **(vision or {})},
        "timeout": 180,
    }
    return c


RESP_OK = {"output": [{"type": "reasoning", "summary": []},
                      {"type": "message", "content": [
                          {"type": "output_text", "text": '{"ok": true}'},
                          {"type": "output_text", "text": ""}]}]}


# -- refusal detection -------------------------------------------------------

@pytest.mark.parametrize("text", [
    "I'm sorry, but I can't help with that request.",
    "I can't comply with this request.",
    "I'm unable to help with explicit sexual content.",
    "I must decline to generate that scene.",
    "This violates our content policy.",
    "I'm sorry, I can't create sexually explicit material.",
    "Content was blocked due to the moderation filter.",
])
def test_refusals_detected(text):
    assert is_refusal(text)


@pytest.mark.parametrize("text", [
    '{"episode": 1, "title": "A Hot Spring"}',
    "I can't believe it was you all along, Ryou said.",
    "The plan cannot wait until morning.",
    "Ryou felt unable to look away as the festival lights rose.",
    "",
    "The battle was intense and explicit in its choreography.",
])
def test_non_refusals_pass_through(text):
    assert not is_refusal(text)


def test_has_images():
    assert _has_images([{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}}]}])
    assert not _has_images([{"role": "user", "content": "just text"}])


# -- routing -----------------------------------------------------------------

def test_no_key_means_local_only(monkeypatch, tmp_path):
    # No env key AND no auth.json (point it at a nonexistent file) => local-only.
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("OPENCODE_AUTH_FILE", str(tmp_path / "absent.json"))
    c = LMStudioClient("http://127.0.0.1:1234/v1")
    assert c._zen_settings() is None


# -- auth.json key resolution -------------------------------------------------

def _write_auth(tmp_path, key="oc_sk_test", account="opencode-go"):
    p = tmp_path / "auth.json"
    p.write_text(json.dumps({account: {"type": "api", "key": key}}),
                 encoding="utf-8")
    return p


def test_key_resolves_from_auth_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    from studio.config import Config, read_zen_key_from_auth_file, resolve_zen_api_key
    p = _write_auth(tmp_path)
    assert read_zen_key_from_auth_file(str(p)) == "oc_sk_test"
    cfg = Config(tmp_path)
    cfg.get("llm", "zen", {})["auth_file"] = str(p)
    assert resolve_zen_api_key(cfg) == "oc_sk_test"


def test_env_var_is_ignored(tmp_path, monkeypatch):
    # The env var must NEVER be used, even when set: the auth file wins.
    from studio.config import Config, resolve_zen_api_key
    p = _write_auth(tmp_path, key="file-key")
    cfg = Config(tmp_path)
    cfg.get("llm", "zen", {})["auth_file"] = str(p)
    monkeypatch.setenv("OPENCODE_API_KEY", "env-key")
    assert resolve_zen_api_key(cfg) == "file-key"


def test_auth_file_missing_account_returns_empty(tmp_path):
    from studio.config import read_zen_key_from_auth_file
    p = _write_auth(tmp_path, account="openrouter")
    assert read_zen_key_from_auth_file(str(p), account="opencode-go") == ""


def test_zen_client_uses_auth_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setenv("OPENCODE_AUTH_FILE", str(_write_auth(tmp_path)))
    c = LMStudioClient("http://127.0.0.1:1234/v1",
                       remote_url="https://opencode.ai/zen/go/v1")
    z = c._zen_settings()
    assert z is not None and z["api_key"] == "oc_sk_test"


def test_remote_success_uses_mapped_model_and_bearer(monkeypatch):
    monkeypatch.setattr("studio.clients.lmstudio.time.sleep", lambda s: None)
    c = _zen_client(remote_model_map={"local-model": "deepseek-v4-pro"})
    seen = {}

    def fake_post(url, path, body, headers=None, timeout=None):
        seen.update(url=url, path=path, body=body, headers=headers)
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    c._post = fake_post
    out = c.chat([{"role": "user", "content": "hi"}], model="local-model")
    assert out == '{"ok": true}'
    assert seen["url"] == "https://opencode.ai/zen/v1"
    assert seen["path"] == "/chat/completions"
    assert seen["body"]["model"] == "deepseek-v4-pro"
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    assert seen["headers"]["User-Agent"] == "anime-studio/1.0"
    # GO rejects requests without a stable session id (MissingSessionID).
    assert seen["headers"]["x-opencode-session"] == c._session_id
    assert c.last_provider == "zen"


def test_refusal_falls_back_to_local(monkeypatch):
    monkeypatch.setattr("studio.clients.lmstudio.time.sleep", lambda s: None)
    c = _zen_client()

    def fake_post(url, path, body, headers=None, timeout=None):
        return {"choices": [{"message": {"content":
            "I'm sorry, but I can't help with that explicit scene."}}]}

    c._post = fake_post
    c._chat_local = lambda *a, **k: "local wrote it"
    assert c.chat([{"role": "user", "content": "write it"}]) == "local wrote it"
    assert c.last_provider == "local"


def test_transport_error_falls_back_to_local(monkeypatch):
    import httpx
    monkeypatch.setattr("studio.clients.lmstudio.time.sleep", lambda s: None)
    c = _zen_client()

    def fake_post(url, path, body, headers=None, timeout=None):
        raise httpx.ConnectError("down")

    c._post = fake_post
    c._chat_local = lambda *a, **k: "local wrote it"
    assert c.chat([{"role": "user", "content": "hi"}]) == "local wrote it"


def test_prefer_local_skips_remote(monkeypatch):
    c = _zen_client()
    c._post = lambda *a, **k: (_ for _ in ()).throw(AssertionError("remote hit"))
    c._chat_local = lambda *a, **k: "local!"
    assert c.chat([{"role": "user", "content": "hi"}], prefer_local=True) == "local!"


def test_vision_calls_go_remote_by_default():
    # Remote vision is ON by default: image calls use the vision model.
    c = _zen_client_with()
    seen, c._post = _capture_post()
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": "http://x/y.png"}}]}]
    c.chat(msgs, model="qwen/qwen3-vl-8b")
    assert seen["body"]["model"] == "deepseek-v4-flash-vision-exp"


# -- per-role remote model selection -----------------------------------------

def _capture_post(content='{"ok": true}'):
    seen = {}

    def fake_post(url, path, body, headers=None, timeout=None):
        seen.update(url=url, path=path, body=body, headers=headers)
        return {"choices": [{"message": {"content": content}}]}

    return seen, fake_post


def test_role_kwarg_selects_remote_model():
    c = _zen_client_with(roles={"script": "kimi-k3"})
    seen, c._post = _capture_post()
    c.chat([{"role": "user", "content": "hi"}], model="dolphin-x", role="script")
    assert seen["body"]["model"] == "kimi-k3"


def test_reverse_lookup_unique_role_match():
    c = _zen_client_with(
        roles={"script": "kimi-k3"},
        local_roles={"showrunner": "dolphin-x", "script": "dolphin-x"})
    seen, c._post = _capture_post()
    # No role= passed, but only "script" is configured remotely.
    c.chat([{"role": "user", "content": "hi"}], model="dolphin-x")
    assert seen["body"]["model"] == "kimi-k3"


def test_reverse_lookup_ambiguous_falls_back_to_default():
    c = _zen_client_with(
        roles={"script": "kimi-k3", "showrunner": "deepseek-v4-pro"},
        local_roles={"showrunner": "dolphin-x", "script": "dolphin-x"})
    seen, c._post = _capture_post()
    c.chat([{"role": "user", "content": "hi"}], model="dolphin-x")
    assert seen["body"]["model"] == "kimi-k2.5"


def test_model_map_still_applies_without_role_config():
    c = _zen_client_with()
    c._zen["model_map"] = {"local-model": "glm-5.3-flash"}
    seen, c._post = _capture_post()
    c.chat([{"role": "user", "content": "hi"}], model="local-model")
    assert seen["body"]["model"] == "glm-5.3-flash"


# -- remote vision ------------------------------------------------------------

def _img_msgs():
    return [{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}]}]


def test_vision_images_go_to_vision_model():
    c = _zen_client_with()
    seen, c._post = _capture_post()
    c.chat(_img_msgs(), model="qwen/qwen3-vl-8b", role="describer")
    assert seen["body"]["model"] == "deepseek-v4-flash-vision-exp"
    # Image parts pass through untouched.
    assert seen["body"]["messages"][0]["content"][1]["type"] == "image_url"


def test_vision_disabled_stays_local():
    c = _zen_client_with(vision={"enabled": False})
    c._post = lambda *a, **k: (_ for _ in ()).throw(AssertionError("remote hit"))
    c._chat_local = lambda *a, **k: "local vision"
    assert c.chat(_img_msgs(), model="qwen/qwen3-vl-8b") == "local vision"


def test_video_resampled_to_frames_for_remote(monkeypatch, tmp_path):
    import base64
    import studio.vcheck as vcheck
    frame = tmp_path / "frame_0.jpg"
    frame.write_bytes(b"\xff\xd8\xff fake-jpeg")
    monkeypatch.setattr(vcheck, "sample_frames", lambda *a, **k: [frame])
    c = _zen_client_with()
    seen, c._post = _capture_post()
    clip_b64 = base64.b64encode(b"fake-mp4-bytes").decode()
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "check"},
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + clip_b64}}]}]
    c.chat(msgs, model="Qwen3-VL-8B-Instruct-FP8")
    assert seen["body"]["model"] == "deepseek-v4-flash-vision-exp"
    types = [p["type"] for p in seen["body"]["messages"][0]["content"]]
    assert "video_url" not in types
    assert "image_url" in types


def test_undecodable_video_stays_local(monkeypatch):
    import studio.vcheck as vcheck
    monkeypatch.setattr(vcheck, "sample_frames", lambda *a, **k: [])
    c = _zen_client_with()
    c._post = lambda *a, **k: (_ for _ in ()).throw(AssertionError("remote hit"))
    c._chat_local = lambda *a, **k: "local video"
    msgs = [{"role": "user", "content": [
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,AAA"}}]}]
    assert c.chat(msgs, model="Qwen3-VL-8B-Instruct-FP8") == "local video"


def test_vision_refusal_falls_back_to_local():
    c = _zen_client_with()

    def fake_post(url, path, body, headers=None, timeout=None):
        return {"choices": [{"message": {"content":
            "I'm sorry, but I can't help with that."}}]}

    c._post = fake_post
    c._chat_local = lambda *a, **k: "local uncensored"
    assert c.chat(_img_msgs(), model="qwen/qwen3-vl-8b") == "local uncensored"


# -- responses API (Spark) ------------------------------------------------------

def test_responses_input_conversion():
    from studio.clients.lmstudio import _to_responses_input
    out = _to_responses_input([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,BBB"}}]},
        {"role": "user", "content": "plain"},
    ])
    assert out[0] == {"role": "system",
                      "content": [{"type": "input_text", "text": "sys"}]}
    assert out[1]["content"] == [
        {"type": "input_text", "text": "hi"},
        {"type": "input_image", "image_url": "data:image/jpeg;base64,AAA"},
        {"type": "input_video", "video_url": "data:video/mp4;base64,BBB"}]
    assert out[2]["content"] == [{"type": "input_text", "text": "plain"}]


def test_responses_text_extraction():
    from studio.clients.lmstudio import _extract_responses_text, LMStudioError
    assert _extract_responses_text(RESP_OK) == '{"ok": true}'
    with pytest.raises(LMStudioError):
        _extract_responses_text({"output": []})


def test_endpoint_routing_per_model():
    c = _zen_client_with()
    assert c._uses_responses_api(c._zen, "muse-spark-1.3-contributor")
    assert c._uses_responses_api(c._zen, "gpt-6-luna")
    assert not c._uses_responses_api(c._zen, "kimi-k2.6")
    assert not c._uses_responses_api(c._zen, "deepseek-v4-flash-vision-exp")


def test_spark_posts_to_responses_endpoint():
    c = _zen_client_with(model="muse-spark-1.3-contributor")
    seen = {}

    def fake_post(url, path, body, headers=None, timeout=None):
        seen.update(path=path, body=body)
        return dict(RESP_OK)

    c._post = fake_post
    assert c.chat_json([{"role": "user", "content": "hi"}]) == {"ok": True}
    assert seen["path"] == "/responses"
    assert seen["body"]["model"] == "muse-spark-1.3-contributor"
    # no client cap by default: the field is omitted, the server decides
    assert "max_output_tokens" not in seen["body"]
    assert seen["body"]["input"][0]["content"][0] == {"type": "input_text", "text": "hi"}
    assert c.last_provider == "zen"


def test_spark_vision_passes_video_natively(monkeypatch):
    import studio.vcheck as vcheck

    def explode(*a, **k):
        raise AssertionError("frame resampling must not run for Spark")

    monkeypatch.setattr(vcheck, "sample_frames", explode)
    c = _zen_client_with(model="muse-spark-1.3-contributor",
                         vision={"model": "muse-spark-1.3-contributor"})
    seen = {}

    def fake_post(url, path, body, headers=None, timeout=None):
        seen.update(path=path, body=body)
        return dict(RESP_OK)

    c._post = fake_post
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "check"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,BBB"}}]}]
    c.chat(msgs, model="Qwen3-VL-8B-Instruct-FP8")
    assert seen["path"] == "/responses"
    types = [p["type"] for p in seen["body"]["input"][0]["content"]]
    assert types == ["input_text", "input_image", "input_video"]


def test_spark_refusal_falls_back_to_local():
    c = _zen_client_with(model="muse-spark-1.3-contributor")

    def fake_post(url, path, body, headers=None, timeout=None):
        return {"output": [{"type": "message", "content": [
            {"type": "output_text",
             "text": "I'm sorry, but I can't help with that."}]}]}

    c._post = fake_post
    c._chat_local = lambda *a, **k: "local uncensored"
    assert c.chat([{"role": "user", "content": "write it"}]) == "local uncensored"


def test_default_config_is_spark(tmp_path):
    from studio.config import Config
    cfg = Config(tmp_path)
    assert cfg.get("llm", "zen", {}).get("model") == "muse-spark-1.3-contributor"
    assert cfg.get("llm", "zen", {}).get("vision", {}).get("model") == \
        "muse-spark-1.3-contributor"


def test_chat_json_refusal_recovers_locally(monkeypatch):
    monkeypatch.setattr("studio.clients.lmstudio.time.sleep", lambda s: None)
    c = _zen_client()
    calls = {"n": 0}

    def fake_post(url, path, body, headers=None, timeout=None):
        calls["n"] += 1
        return {"choices": [{"message": {"content":
            "I can't comply with this request."}}]}

    c._post = fake_post
    c._chat_local = lambda *a, **k: '{"episode": 1}'
    assert c.chat_json([{"role": "user", "content": "write it"}]) == {"episode": 1}
    assert calls["n"] == 1  # no pointless remote retries after a refusal
