"""The client must not impose an output cap by default.

max_output_tokens / max_tokens are optional; the server/model already caps output
and unused budget is free, so a client cap only truncates reasoning models.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import studio.config as config
from studio.clients.lmstudio import _effective_output_cap


def test_no_cap_by_default():
    # Repo config sets llm.max_output_tokens: 0 -> the field is omitted entirely.
    assert _effective_output_cap(200) is None
    assert _effective_output_cap(4096) is None
    assert _effective_output_cap(None) is None


def test_optional_ceiling_when_configured(monkeypatch):
    class FakeCfg:
        def get(self, *args, **kwargs):
            return 16384

    monkeypatch.setattr(config, "get_config", lambda: FakeCfg())
    # A low caller hint is floored to the configured ceiling...
    assert _effective_output_cap(200) == 16384
    # ...but a caller asking for more keeps its larger value.
    assert _effective_output_cap(40000) == 40000
