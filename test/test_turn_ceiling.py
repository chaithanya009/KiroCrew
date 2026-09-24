"""The channel turn ceiling, and the tripwire that keeps it wired everywhere.

Two halves, and the second is the one that matters over time.

The behaviour half pins what the ceiling does: it counts, it latches, it clears
on a key rotation, it stays bounded in memory, and it tells an observer once.

The discovery half pins WHERE it is. The loss this guard closes is a channel
whose runaway is unbounded, so a channel that silently misses the guard is the
bug returning with a new name. So this file enumerates every place in the package
that opens a channel turn and requires each one to be classified on purpose --
gated, or exempt with a reason. Adding a channel, or a second turn site to an
existing one, fails here until someone says which it is.
"""

from __future__ import annotations

import ast
import asyncio
import time
from unittest.mock import MagicMock

import pytest
from source_corpus import candidate_sources, src_root

from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging.turn_ceiling import (
    DEFAULT_MAX_TURNS,
    DEFAULT_WINDOW_SECS,
    ENV_MAX_TURNS,
    ENV_WINDOW_SECS,
    MAX_TRACKED_CONVERSATIONS,
    REFUSAL_TEXT,
    ConversationTurnCeiling,
    TurnCeilingExceeded,
    gate,
    set_notification_sink,
    shared_ceiling,
    surface_of,
)
from kiro_crew.session import SessionManager

# ───────────────────────── behaviour ─────────────────────────


def _ceiling(**kw: object) -> ConversationTurnCeiling:
    kw.setdefault("max_turns", 3)
    kw.setdefault("window_secs", 60.0)
    return ConversationTurnCeiling(**kw)  # type: ignore[arg-type]


def test_turns_up_to_the_ceiling_are_allowed() -> None:
    ceiling = _ceiling()
    for _ in range(3):
        ceiling.check("slack:C1:T1")
    assert not ceiling.is_latched("slack:C1:T1")


def test_the_turn_past_the_ceiling_is_refused() -> None:
    ceiling = _ceiling()
    for _ in range(3):
        ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")


def test_the_refusal_carries_the_user_facing_text() -> None:
    ceiling = _ceiling(max_turns=1)
    ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded) as caught:
        ceiling.check("slack:C1:T1")
    assert str(caught.value) == REFUSAL_TEXT


def test_conversations_are_counted_independently() -> None:
    """One runaway conversation must not refuse turns in an unrelated one."""
    ceiling = _ceiling(max_turns=1)
    ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")
    ceiling.check("telegram:999")  # different conversation, unaffected
    assert not ceiling.is_latched("telegram:999")


def test_turns_older_than_the_window_do_not_count() -> None:
    """The window rolls, so a slow conversation is never refused."""
    ceiling = _ceiling(max_turns=2, window_secs=0.05)
    ceiling.check("slack:C1:T1")
    ceiling.check("slack:C1:T1")
    time.sleep(0.08)
    ceiling.check("slack:C1:T1")  # both earlier turns have aged out
    assert not ceiling.is_latched("slack:C1:T1")


def test_the_latch_outlives_the_window() -> None:
    """The discriminating test for latching.

    A rolling window alone bounds the burn RATE but never ends a loop: it
    resumes the moment the window slides. Once latched, waiting is not a way
    back in.
    """
    ceiling = _ceiling(max_turns=1, window_secs=0.05)
    ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")
    time.sleep(0.08)  # long enough that the window would have cleared
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")


def test_a_reset_clears_the_latch() -> None:
    """The resume path. Reachable by resetting the conversation, not by waiting."""
    ceiling = _ceiling(max_turns=1)
    ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")
    ceiling.reset("slack:C1:T1")
    ceiling.check("slack:C1:T1")
    assert not ceiling.is_latched("slack:C1:T1")


def test_an_unrelated_key_has_its_own_window() -> None:
    """The count and the latch are per key, with no shared state between them."""
    ceiling = _ceiling(max_turns=1)
    ceiling.check("slack:C1:T1")
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T1")
    ceiling.check("slack:C1:T2")  # a different key is counted from zero
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check("slack:C1:T2")


def test_an_empty_session_key_is_not_counted() -> None:
    """Nothing to attribute a count to, and refusing it would be a silent drop."""
    ceiling = _ceiling(max_turns=1)
    for _ in range(5):
        ceiling.check("")


def test_tracking_is_bounded() -> None:
    """A host meeting many conversations must not turn a loop guard into a leak."""
    ceiling = _ceiling(max_turns=10, max_tracked=4)
    for index in range(50):
        ceiling.check(f"slack:C{index}")
    assert len(ceiling._windows) <= 4


def test_latches_are_bounded_too() -> None:
    ceiling = _ceiling(max_turns=1, max_tracked=3)
    for index in range(20):
        key = f"slack:C{index}"
        ceiling.check(key)
        with pytest.raises(TurnCeilingExceeded):
            ceiling.check(key)
    assert len(ceiling._latched) <= 3


# ───────────────────────── the notice ─────────────────────────


def test_the_observer_is_told_once_at_the_latch() -> None:
    """Once, at the latch -- not again by every turn queued behind it."""
    seen: list[tuple[str, str]] = []
    set_notification_sink(lambda key, surface: seen.append((key, surface)))
    try:
        ceiling = _ceiling(max_turns=1)
        ceiling.check("slack:C1:T1")
        for _ in range(4):
            with pytest.raises(TurnCeilingExceeded):
                ceiling.check("slack:C1:T1")
    finally:
        set_notification_sink(None)
    assert seen == [("slack:C1:T1", "slack")]


def test_a_failing_observer_does_not_change_the_refusal() -> None:
    """The sink runs inside a gate whose only job is to refuse the turn."""

    def _explode(key: str, surface: str) -> None:
        raise RuntimeError("notification feed is down")

    set_notification_sink(_explode)
    try:
        ceiling = _ceiling(max_turns=1)
        ceiling.check("slack:C1:T1")
        with pytest.raises(TurnCeilingExceeded):
            ceiling.check("slack:C1:T1")
    finally:
        set_notification_sink(None)


def test_surface_is_read_from_the_key() -> None:
    assert surface_of("slack:C1:T1") == "slack"
    assert surface_of("telegram:42") == "telegram"


def test_a_key_without_a_surface_degrades_instead_of_raising() -> None:
    assert surface_of("") == "channel"
    assert surface_of("   ") == "channel"


def test_the_refusal_quotes_no_command() -> None:
    """In a self-chat the agent's own text returns as inbound.

    A refusal that quoted the command for resuming would hand the loop its own
    way out, so the text names the surface and quotes no command.
    """
    assert "/" not in REFUSAL_TEXT
    assert "dashboard" in REFUSAL_TEXT.lower()


# ───────────────────────── composition ─────────────────────────


def test_the_gate_runs_the_channels_own_gate_first() -> None:
    """Load-bearing in both directions.

    A shutdown refusal keeps behaving exactly as it does without a ceiling, and
    a turn the channel was never going to run is not counted against the
    conversation.
    """

    class Closing(Exception):
        pass

    def _inner() -> None:
        raise Closing

    ceiling = _ceiling(max_turns=1)
    composed = gate("slack:C1:T1", _inner, ceiling=ceiling)
    for _ in range(5):
        with pytest.raises(Closing):
            composed()
    # None of those refused turns were counted, so the conversation is untouched.
    assert not ceiling.is_latched("slack:C1:T1")
    ceiling.check("slack:C1:T1")


def test_the_gate_counts_when_the_channels_gate_passes() -> None:
    ceiling = _ceiling(max_turns=1)
    composed = gate("slack:C1:T1", lambda: None, ceiling=ceiling)
    composed()
    with pytest.raises(TurnCeilingExceeded):
        composed()


def test_the_gate_needs_no_inner() -> None:
    ceiling = _ceiling(max_turns=1)
    composed = gate("slack:C1:T1", ceiling=ceiling)
    composed()
    with pytest.raises(TurnCeilingExceeded):
        composed()


def test_the_gate_is_yield_free() -> None:
    """The gate it replaces must not await: the gate, monitor acceptance and the
    stream's turn registration are one event-loop span."""
    composed = gate("slack:C1:T1", ceiling=_ceiling())
    assert composed() is None  # a coroutine would be returned, not None


# ───────────────────────── configuration ─────────────────────────


def test_the_shipped_default_separates_a_loop_from_a_person() -> None:
    """A loop sustains 180+ turns an hour; a fast human in a busy thread is in
    the tens. The default sits between them and latches a loop inside roughly
    half a window."""
    assert DEFAULT_MAX_TURNS == 90
    assert DEFAULT_WINDOW_SECS == 3600.0


def test_an_operator_can_retune_without_a_code_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_MAX_TURNS, "5")
    monkeypatch.setenv(ENV_WINDOW_SECS, "120")
    ceiling = ConversationTurnCeiling()
    assert ceiling.max_turns == 5
    assert ceiling.window_secs == 120.0


@pytest.mark.parametrize("bad", ["0", "-4", "banana", ""])
def test_an_unusable_override_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setenv(ENV_MAX_TURNS, bad)
    assert ConversationTurnCeiling().max_turns == DEFAULT_MAX_TURNS


@pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_a_non_finite_window_is_refused(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    """A positivity test alone admits both non-finite values.

    ``inf > 0`` is True and every comparison against ``nan`` is False, so either
    slips through a bare ``value <= 0`` guard. Once accepted, no timestamp ever
    ages out and the rolling window silently becomes a lifetime counter.
    """
    monkeypatch.setenv(ENV_WINDOW_SECS, bad)
    assert ConversationTurnCeiling().window_secs == DEFAULT_WINDOW_SECS


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 0.0, -1.0])
def test_an_explicit_non_finite_window_raises(bad: float) -> None:
    """The env reader falls back for an operator; a caller in this codebase raises."""
    with pytest.raises(ValueError):
        ConversationTurnCeiling(window_secs=bad)


def test_a_non_finite_window_would_have_made_the_window_a_lifetime_counter() -> None:
    """Names the consequence the guard prevents, so the guard's reason is testable.

    With an infinite window the expiry horizon is ``-inf``, so nothing is ever
    pruned. The guard is what stops that state existing at all.
    """
    horizon = 0.0 - float("inf")
    assert not (1.0 <= horizon)  # no timestamp would ever be pruned


def test_an_explicit_non_positive_ceiling_raises() -> None:
    with pytest.raises(ValueError):
        ConversationTurnCeiling(max_turns=0)


def test_the_store_is_shared_across_channels() -> None:
    """One conversation is one count however many dispatch objects a host builds."""
    assert shared_ceiling() is shared_ceiling()


def test_clearing_the_store_releases_every_conversation() -> None:
    """``clear()`` forgets the whole process-global store, counts and latches.

    One counter serves the whole process, so a count outlives whatever drove it.
    A test worker shares one interpreter across unrelated tests, and the suite's
    autouse fixture calls this between them: without a working clear, a worker
    that drives more gated turns under one key than the ceiling allows leaves that
    conversation latched and every later turn on the key refused.
    """
    ceiling = shared_ceiling()
    first, second = "slack:C-clear:T-a", "slack:C-clear:T-b"
    for _ in range(ceiling.max_turns):
        ceiling.check(first)
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check(first)
    ceiling.check(second)
    assert ceiling.is_latched(first), "precondition: nothing was latched to clear"

    ceiling.clear()

    assert not ceiling.is_latched(first)
    ceiling.check(first)  # the latched conversation answers again
    # The unlatched conversation's count is gone too, so it gets a whole window
    # again: a count that survived the clear would refuse inside this loop.
    for _ in range(ceiling.max_turns):
        ceiling.check(second)
    with pytest.raises(TurnCeilingExceeded):
        ceiling.check(second)
    ceiling.clear()


def test_the_tracking_cap_is_set() -> None:
    assert MAX_TRACKED_CONVERSATIONS > 0


# ───────────────────── the resume path is real ─────────────────────


def _provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, cwd=None, **kwargs):
        provider = MagicMock()
        provider.start = MagicMock(return_value=asyncio.sleep(0))
        provider.shutdown = MagicMock(return_value=asyncio.sleep(0))
        provider.cwd = cwd or "/unset"
        provider.context_usage_pct = MagicMock(return_value=0.0)
        provider.is_alive = MagicMock(return_value=True)
        provider.is_process_alive = MagicMock(return_value=True)
        provider.has_active_turn = MagicMock(return_value=False)
        provider.runtime_info = MagicMock(return_value=(None, None))
        return provider

    return factory


class TestDiscardingTheConversationReleasesTheLatch:
    """The refusal text tells the user to reset the conversation, so the reset has
    to be what clears the latch.

    These drive the REAL ``discard_conversation``, not a stub. The channel key
    does NOT change on a discard -- channel linkage is retained by design -- so
    nothing about the key clears a latch, and only an explicit reset does. A stub
    would assert the wire the test itself wrote.
    """

    @pytest.mark.asyncio
    async def test_a_discard_clears_a_latched_conversation(self) -> None:
        key = "slack:C-ceiling:T-latched"
        ceiling = shared_ceiling()
        ceiling.reset(key)
        try:
            for _ in range(ceiling.max_turns):
                ceiling.check(key)
            with pytest.raises(TurnCeilingExceeded):
                ceiling.check(key)
            assert ceiling.is_latched(key), "precondition: nothing was latched to clear"

            manager = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
            await manager.discard_conversation(key)

            assert not ceiling.is_latched(key)
            ceiling.check(key)  # the conversation answers again
        finally:
            ceiling.reset(key)

    @pytest.mark.asyncio
    async def test_a_discard_clears_the_latch_under_the_channels_own_key(self) -> None:
        """``_fold_key`` resolves an alias onto the live key, so the key the
        channel counted under and the key the discard folds to can differ.
        Clearing only the folded one would leave the latch standing."""
        key = "slack:C-ceiling:T-alias"
        ceiling = shared_ceiling()
        ceiling.reset(key)
        try:
            for _ in range(ceiling.max_turns):
                ceiling.check(key)
            with pytest.raises(TurnCeilingExceeded):
                ceiling.check(key)

            manager = SessionManager(KiroCrewConfig(), provider_factory=_provider_factory())
            folded = manager._fold_key(key)
            await manager.discard_conversation(key)

            assert not ceiling.is_latched(
                key
            ), f"latch survived under the channel's own key (folded to {folded!r})"
        finally:
            ceiling.reset(key)


# ───────────────────── the discovery tripwire ─────────────────────

#: Channel turn sites that MUST compose the ceiling, by path relative to the
#: package root. These are the inbound arms: a message arrived from a chat
#: surface and is about to drive a model turn.
GATED = {
    "messaging/dispatch.py",
    "slack/transport_dispatch.py",
    "slack/handler.py",
    "telegram/transport_dispatch.py",
    "discord/transport_dispatch.py",
}

#: Turn sites that must NOT compose the ceiling, each with the reason. A reason
#: is required because an unexplained exemption is how a channel goes unbounded.
EXEMPT = {
    "dashboard/chat_runner.py": (
        "dashboard turns are driven by a human watching and clicking through them"
    ),
    "slack/gateway.py": (
        "the autonudge monitor arm, already bounded by its own cycle cap and "
        "runtime budget; counting its cycles would let a long legitimate watch "
        "latch the conversation and refuse the human's next message"
    ),
    "session.py": "forwards to the gate's definition rather than opening a channel turn",
}


def _calls_begin_turn(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "begin_turn"
        ):
            return True
    return False


def _turn_site_files() -> set[str]:
    root = src_root()
    found: set[str] = set()
    for path, text in candidate_sources(require_any=("begin_turn",)):
        if "_vendor" in path.parts:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:  # pragma: no cover - a parse gate owns this
            continue
        if _calls_begin_turn(tree):
            found.add(path.relative_to(root).as_posix())
    return found


def test_every_turn_site_is_classified() -> None:
    """The tripwire.

    A new channel, or a second turn site in an existing one, lands here first.
    Deciding it is gated or exempt is a one-line edit; forgetting to decide is
    what this test refuses.
    """
    found = _turn_site_files()
    classified = GATED | set(EXEMPT)
    unclassified = found - classified
    assert not unclassified, (
        "these files open a channel turn but are neither gated nor exempt: "
        f"{sorted(unclassified)} -- add each to GATED (and compose "
        "turn_ceiling.gate at the site) or to EXEMPT with a reason"
    )


def test_the_classification_has_no_stale_entries() -> None:
    """An entry naming a site that is absent is a tripwire that cannot fire."""
    found = _turn_site_files()
    stale = (GATED | set(EXEMPT)) - found
    assert not stale, f"classified but absent from the tree: {sorted(stale)}"


@pytest.mark.parametrize("relative", sorted(GATED))
def test_a_gated_site_composes_the_ceiling(relative: str) -> None:
    text = (src_root() / relative).read_text(encoding="utf-8")
    assert "turn_ceiling.gate(" in text


@pytest.mark.parametrize("relative", sorted(GATED))
def test_a_gated_site_catches_the_refusal(relative: str) -> None:
    """Composing the gate without catching its refusal would turn a bounded
    pause into an unhandled error, which is a worse outcome than the loop."""
    text = (src_root() / relative).read_text(encoding="utf-8")
    assert "except TurnCeilingExceeded" in text


@pytest.mark.parametrize("relative", sorted(GATED))
def test_a_gated_site_does_not_spool_the_refused_message(relative: str) -> None:
    """The spool replays a message our own restart dropped. A message refused on
    purpose must not be replayed, or it is answered after all."""
    text = (src_root() / relative).read_text(encoding="utf-8")
    start = text.index("except TurnCeilingExceeded")
    end = text.index("except ", start + 1)
    assert "spool_refused_turn" not in text[start:end]


@pytest.mark.parametrize("relative", sorted(EXEMPT))
def test_an_exempt_site_stays_ungated(relative: str) -> None:
    text = (src_root() / relative).read_text(encoding="utf-8")
    assert "turn_ceiling.gate(" not in text, (
        f"{relative} is listed EXEMPT ({EXEMPT[relative]}) but composes the "
        "ceiling; move it to GATED or remove the composition"
    )


def test_every_exemption_states_a_reason() -> None:
    for relative, reason in EXEMPT.items():
        assert reason.strip(), relative
