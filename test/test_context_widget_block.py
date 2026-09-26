"""Tests for `{{WIDGET_BLOCK}}` placeholder resolution in context.py.

Before the widgets skill was introduced, `_resolve_prompt_templates` injected
~200 words of theme-variable rules + a `[WIDGETS]` per-message append that
duplicated the same content. Both have been replaced with a short pointer at
the system-prompt level and a bundled `widgets` skill the agent cats on
demand. These tests lock the new shape so we don't accidentally reintroduce
the bloat.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def _resolve(prompt: str, session_key: str, density: str = "more") -> str:
    """Call the private template resolver with a canned config."""
    from kiro_crew.context import ContextBuilder

    fake_cfg = SimpleNamespace(dashboard=SimpleNamespace(widget_density=density))
    with patch("kiro_crew.context.KiroCrewConfig.load", return_value=fake_cfg):
        return ContextBuilder._resolve_prompt_templates(prompt, session_key)


class TestMaxSubagentsPlaceholder:
    """`{{MAX_SUBAGENTS}}` expands to the concurrent cap IN FORCE on every transport."""

    @staticmethod
    def _resolve_cap(prompt, session_key, *, cap=None, raises=False, live=0):
        from kiro_crew.context import ContextBuilder

        fake_cfg = SimpleNamespace(dashboard=SimpleNamespace(widget_density="more"))
        if raises:
            sub = patch(
                "kiro_crew.subagent.resolve_max_subagents",
                side_effect=RuntimeError("boom"),
            )
        else:
            sub = patch("kiro_crew.subagent.resolve_max_subagents", return_value=cap)
        with (
            patch("kiro_crew.context.KiroCrewConfig.load", return_value=fake_cfg),
            patch("kiro_crew.resource_status.adaptive_exec_cap", return_value=live),
            sub,
        ):
            return ContextBuilder._resolve_prompt_templates(prompt, session_key)

    def test_the_cap_in_force_wins_over_the_configured_ceiling(self):
        # ``max_subagents`` is a ceiling; the adaptive controller can be
        # dispatching far fewer under it, and a model sized to the ceiling
        # queues work it believes is running. The live figure is what is used,
        # with no ceiling label, and the configured number does not appear.
        result = self._resolve_cap("up to {{MAX_SUBAGENTS}} run", "dashboard:abc", cap=64, live=8)
        assert "up to 8 run" in result
        assert "64" not in result and "ceiling" not in result

    def test_token_replaced_with_labelled_ceiling_on_every_transport(self):
        # No controller in this process: the configured number is still given,
        # but LABELLED as a ceiling so the model does not read it as the cap in
        # force. It must reach dashboard, Slack, CLI, and empty-key sessions
        # alike — delegation guidance is transport-agnostic.
        for key in ("dashboard:abc", "slack:C1:1.2", "cli:local", ""):
            result = self._resolve_cap("up to {{MAX_SUBAGENTS}} agents", key, cap=12)
            assert "{{MAX_SUBAGENTS}}" not in result
            assert "up to 12 (configured ceiling) agents" in result

    def test_zero_cap_falls_back_to_several(self):
        # cap==0 (auto-size failed / unreadable host) keeps the sentence grammatical.
        result = self._resolve_cap("up to {{MAX_SUBAGENTS}} agents", "slack:C1:1.2", cap=0)
        assert "up to several agents" in result

    def test_resolver_error_falls_back_to_several(self):
        # A raising resolver must never break prompt assembly.
        result = self._resolve_cap("up to {{MAX_SUBAGENTS}} agents", "dashboard:abc", raises=True)
        assert "up to several agents" in result

    def test_absent_token_skips_resolver(self):
        # No token → the (heavier) sub-agent resolver is never invoked.
        from kiro_crew.context import ContextBuilder

        fake_cfg = SimpleNamespace(dashboard=SimpleNamespace(widget_density="more"))
        with (
            patch("kiro_crew.context.KiroCrewConfig.load", return_value=fake_cfg),
            patch("kiro_crew.subagent.resolve_max_subagents") as resolver,
        ):
            ContextBuilder._resolve_prompt_templates(
                "no token here {{WIDGET_BLOCK}}", "dashboard:abc"
            )
        resolver.assert_not_called()
