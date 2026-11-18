"""
Playbook reference: Unified Developer Playbook, Part VIII Step 12.
Integration test mirroring test_scan_flow_integration.py's own stated
purpose, applied to this step: proves the WatchHandler wiring itself
(FSM transitions, callback routing, preset selection persisting across
screens) through the real dispatcher chain, not just AutoWatchManager's
own already-tested internal logic (test_auto_watch.py) or
filter_presets.matches (test_filter_presets.py) in isolation.

Requires aiogram (mocks CallbackQuery via `MagicMock(spec=...)`, same
technique as test_scan_flow_integration.py) and aiohttp (AutoWatchManager
itself doesn't need it, but importing handlers.watch_handler pulls in
rendering.menus -> aiogram, and this test's own discovery/engine fakes
mirror test_auto_watch.py's, not real providers - no real network
anywhere in this file either way).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message, User

from analysis.api_abstraction import DiscoveryCandidate, PairData, SecurityReport
from analysis.core_engine import CoreEngine
from analysis.holder_engine import HolderEngine
from analysis.momentum_engine import MomentumEngine
from analysis.security_engine import SecurityEngine
from analysis.social_engine import SocialEngine
from bot.constants import Chain, FSMState
from handlers import auto_watch as auto_watch_module
from handlers.auto_watch import AutoWatchManager
from handlers.dispatcher import Dispatcher
from handlers.navigation import build_navigation_handlers
from handlers.scan_handler import ScanHandler
from handlers.watch_handler import WatchHandler
from scoring.pipeline import ScoringPipeline
from state.fsm import FSMEngine
from state.session_store import SessionStore

_TEST_INTERVAL_SECONDS = 0.02


class _FakeDiscoveryProvider:
    def __init__(self, candidates: list[DiscoveryCandidate] | None = None) -> None:
        self._candidates = candidates or []

    async def get_new_listings(self, limit: int = 30) -> list[DiscoveryCandidate]:
        return self._candidates[:limit]

    async def get_trending(self, limit: int = 30) -> list[DiscoveryCandidate]:
        return self._candidates[:limit]


class _SlowDiscoveryProvider(_FakeDiscoveryProvider):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_new_listings(self, limit: int = 30) -> list[DiscoveryCandidate]:
        self.started.set()
        await self.release.wait()
        return []

    async def get_trending(self, limit: int = 30) -> list[DiscoveryCandidate]:
        self.started.set()
        await self.release.wait()
        return []


class _FailingDiscoveryProvider(_FakeDiscoveryProvider):
    async def get_new_listings(self, limit: int = 30) -> list[DiscoveryCandidate]:
        raise RuntimeError("simulated discovery outage")


class _FakeMarketProvider:
    def __init__(self, pairs_by_address: dict[str, list[PairData]] | None = None) -> None:
        self._pairs_by_address = pairs_by_address or {}
        self.calls = 0

    async def get_pairs(self, address: str) -> list[PairData]:
        self.calls += 1
        return self._pairs_by_address.get(address, [])


class _FakeSecurityProvider:
    async def scan(self, address: str, chain) -> SecurityReport:
        return SecurityReport(
            trust_score=90.0, mint_authority_active=False, freeze_authority_active=False,
            buy_tax_pct=0.0, sell_tax_pct=0.0, lp_lock_ratio_pct=100.0, lp_lock_duration_days=100.0,
            ownership_renounced=True, raw_risk_flags=[], chain_supported=True,
        )


class _FakeHolderProvider:
    async def get_holders(self, address: str, chain) -> list:
        return []

    async def get_launch_block_funding(self, address: str, chain) -> list:
        return []


class _FakeSocialProvider:
    """This file's tests exercise Auto-Watch/Filters, not social scoring
    - always-degrades is enough (same fake as test_scan_orchestration.py
    and test_trading_integration.py each carry their own copy of)."""

    async def lookup_user(self, handle: str) -> object:
        raise RuntimeError("fake social provider - lookup always fails")

    async def list_followers(self, handle: str, limit: int) -> list:
        return []

    async def search_mentions(self, ticker: str) -> list:
        return []


def _fake_user(user_id: int) -> User:
    return MagicMock(spec=User, id=user_id)


def _fake_callback(data: str, user_id: int) -> CallbackQuery:
    cq = MagicMock(spec=CallbackQuery)
    cq.data = data
    cq.from_user = _fake_user(user_id)
    cq.answer = AsyncMock()
    cq.message = MagicMock(spec=Message)
    cq.message.edit_text = AsyncMock()
    cq.message.answer = AsyncMock()
    return cq


def _build_stack(
    discovery_provider=None,
    market_provider=None,
    poll_interval_seconds_override: float = _TEST_INTERVAL_SECONDS,
):
    session_store = SessionStore()
    fsm = FSMEngine(session_store)
    discovery_provider = discovery_provider or _FakeDiscoveryProvider()
    core_engine = CoreEngine(market_provider or _FakeMarketProvider())
    security_engine = SecurityEngine(_FakeSecurityProvider())
    holder_engine = HolderEngine(_FakeHolderProvider())
    momentum_engine = MomentumEngine()
    social_engine = SocialEngine(_FakeSocialProvider())
    scoring_pipeline = ScoringPipeline()

    alerts: list[tuple[int, object]] = []

    async def on_match(user_id: int, scored) -> None:
        alerts.append((user_id, scored))

    auto_watch_manager = AutoWatchManager(
        session_store=session_store,
        discovery_provider=discovery_provider,
        core_engine=core_engine, security_engine=security_engine, holder_engine=holder_engine,
        momentum_engine=momentum_engine, social_engine=social_engine, scoring_pipeline=scoring_pipeline,
        on_match=on_match,
        poll_interval_seconds_override=poll_interval_seconds_override,
    )
    scan_handler = ScanHandler(
        fsm, core_engine, security_engine, holder_engine, momentum_engine, social_engine,
        scoring_pipeline, session_store,
    )
    watch_handler = WatchHandler(
        fsm, auto_watch_manager, discovery_provider,
        core_engine, security_engine, holder_engine, momentum_engine, social_engine,
        scoring_pipeline, session_store,
    )
    dispatcher = Dispatcher(fsm)
    for handler in build_navigation_handlers(fsm, session_store, extra_handlers=[scan_handler, watch_handler]):
        dispatcher.register(handler)

    return dispatcher, fsm, auto_watch_manager, session_store, alerts


async def _wait_until(predicate, timeout_seconds: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("Timed out waiting for Auto-Watch status.")
        await asyncio.sleep(0.005)


def _candidate(index: int, source: str = "boosted") -> DiscoveryCandidate:
    address = f"{'A' * 31}{'123456789A'[index % 10]}"
    return DiscoveryCandidate(chain=Chain.SOL, token_address=address, source=source)


def _pair(address: str, liquidity_usd: float = 50_000.0) -> PairData:
    return PairData(
        chain=Chain.SOL,
        dex_id="raydium",
        pair_address=f"pair-{address}",
        base_token_address=address,
        base_token_symbol=f"T{address[-1]}",
        base_token_name="Test Token",
        quote_token_symbol="SOL",
        price_usd=1.0,
        liquidity_usd=liquidity_usd,
        fdv=1_000_000.0,
        market_cap=1_000_000.0,
        volume_5m=100.0,
        volume_1h=1_000.0,
        volume_6h=5_000.0,
        volume_24h=100_000.0,
        price_change_5m=0.5,
        price_change_1h=1.0,
        price_change_6h=2.0,
        price_change_24h=5.0,
        buys_24h=200,
        sells_24h=150,
        pair_created_at_ms=1_700_000_000_000,
    )


@pytest.mark.asyncio
async def test_selecting_a_preset_then_starting_watch_uses_that_preset() -> None:
    """Full flow through the real dispatcher: tap a preset on Filter
    Config, then tap Start on Auto-Watch - the preset chosen on the
    FIRST screen must be what the SECOND screen's watch actually uses,
    proving the FSM-payload handoff between the two screens
    (WatchHandler._handle_watch_start's own docstring) works for real,
    not just in isolated unit tests of each half."""
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack()
    user_id = 42

    await dispatcher.dispatch(_fake_callback("rule_preset:aggressive", user_id))
    ctx_after_preset = fsm.get_state(user_id)
    assert ctx_after_preset.payload.get("selected_filter_preset") == "aggressive"

    await dispatcher.dispatch(_fake_callback("watch_start", user_id))

    assert fsm.get_state(user_id).state is FSMState.AUTO_WATCH_ACTIVE
    status = auto_watch_manager.status(user_id)
    assert status is not None
    assert status.profile_name == "aggressive"

    task = session_store.get_watch_task(user_id)
    assert task is not None and not task.done()

    await auto_watch_manager.stop(user_id)  # cleanup - no orphaned task left after this test


@pytest.mark.asyncio
async def test_custom_filter_full_flow_actually_customizes_the_watch() -> None:
    """Step 12 revalidation's core regression test. Under the bug this
    fixes, `NAMED_PRESETS.get("custom", NAMED_PRESETS["balanced"])`
    returned the BALANCED FilterProfile object itself - whose own `.name`
    is "balanced" - so `status.profile_name` would read "balanced" here
    even though the user picked Custom. That's the one symptom this test
    checks for directly, plus the FSM payload actually holding the
    specific values configured, not just Balanced's own numbers under a
    different label."""
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack()
    user_id = 44

    await dispatcher.dispatch(_fake_callback("rule_advanced", user_id))
    ctx = fsm.get_state(user_id)
    assert ctx.state is FSMState.CONFIGURING_FILTER
    draft = ctx.payload["custom_filter_draft"]
    assert draft.min_liquidity_usd == 15_000.0  # Balanced's own seed value

    # Liquidity ladder is (3_000, 15_000, 50_000, 150_000) - one cycle
    # from Balanced's 15_000 lands on 50_000.
    await dispatcher.dispatch(_fake_callback("rule_num:liquidity", user_id))
    draft = fsm.get_state(user_id).payload["custom_filter_draft"]
    assert draft.min_liquidity_usd == 50_000.0

    # Balanced defaults reject_high_concentration to False; turn it on.
    await dispatcher.dispatch(_fake_callback("rule_tgl:concentration:on", user_id))
    draft = fsm.get_state(user_id).payload["custom_filter_draft"]
    assert draft.reject_high_concentration is True
    assert draft.min_liquidity_usd == 50_000.0  # earlier change survived this second tap

    await dispatcher.dispatch(_fake_callback("rule_save", user_id))
    assert fsm.get_state(user_id).payload.get("selected_filter_preset") == "custom"

    await dispatcher.dispatch(_fake_callback("watch_start", user_id))
    status = auto_watch_manager.status(user_id)
    assert status is not None
    assert status.profile_name == "custom"  # NOT "balanced" - the bug this fixes

    saved_draft = fsm.get_state(user_id).payload["custom_filter_draft"]
    assert saved_draft.min_liquidity_usd == 50_000.0
    assert saved_draft.reject_high_concentration is True

    await auto_watch_manager.stop(user_id)


@pytest.mark.asyncio
async def test_advanced_rules_stale_tap_from_a_different_screen_does_not_crash() -> None:
    """The dispatcher's own broad `except Exception` (Part IV.4 zero-
    dead-ends net) should turn a stale rule_tgl tap into the generic
    recovery reply, not an unhandled InvalidTransitionError - e.g. a user
    who opened Advanced Rules, then separately started a watch (now
    AUTO_WATCH_ACTIVE, which has no edge to CONFIGURING_FILTER), then
    scrolled back up and tapped an old Advanced Rules button still
    sitting in their chat history. Deliberately uses AUTO_WATCH_ACTIVE
    rather than IDLE for the stale state - IDLE *can* legitimately reach
    CONFIGURING_FILTER, so a stale tap from IDLE wouldn't raise anything
    at all, and would silently prove nothing. This does not depend on
    today's CONFIGURING_FILTER self-loop fix; it exercises the
    dispatcher's outermost safety net that already existed for every
    other handler."""
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack()
    user_id = 45

    await dispatcher.dispatch(_fake_callback("watch_start", user_id))
    assert fsm.get_state(user_id).state is FSMState.AUTO_WATCH_ACTIVE

    cq = _fake_callback("rule_tgl:concentration:on", user_id)
    await dispatcher.dispatch(cq)  # must not raise

    cq.message.edit_text.assert_awaited()
    (sent_text,), _ = cq.message.edit_text.call_args
    assert "went wrong" in sent_text.lower() or "advanced rules" in sent_text.lower()
    await auto_watch_manager.stop(user_id)  # cleanup - watch_start above left a task running


@pytest.mark.asyncio
async def test_start_without_ever_selecting_a_preset_defaults_to_balanced() -> None:
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack()
    user_id = 43

    await dispatcher.dispatch(_fake_callback("watch_start", user_id))

    status = auto_watch_manager.status(user_id)
    assert status is not None
    assert status.profile_name == "balanced"

    await auto_watch_manager.stop(user_id)


@pytest.mark.asyncio
async def test_stop_requires_confirmation_before_cancelling_the_watch_task() -> None:
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack()
    user_id = 44

    await dispatcher.dispatch(_fake_callback("watch_start", user_id))
    assert fsm.get_state(user_id).state is FSMState.AUTO_WATCH_ACTIVE
    active_task = session_store.get_watch_task(user_id)
    assert active_task is not None

    confirmation = _fake_callback("watch_stop", user_id)
    # The first stop tap only opens the confirmation prompt; it does not
    # cancel the task.
    await dispatcher.dispatch(confirmation)
    assert fsm.get_state(user_id).state is FSMState.AUTO_WATCH_ACTIVE
    assert session_store.get_watch_task(user_id) is active_task
    confirm_markup = confirmation.message.edit_text.await_args.kwargs["reply_markup"]
    confirm_callbacks = {
        button.callback_data
        for row in confirm_markup.inline_keyboard
        for button in row
        if button.callback_data is not None
    }
    assert "watch_stop_confirm" in confirm_callbacks
    assert "watch_stop_cancel" in confirm_callbacks

    await dispatcher.dispatch(_fake_callback("watch_stop_cancel", user_id))
    assert session_store.get_watch_task(user_id) is active_task
    assert auto_watch_manager.status(user_id) is not None

    await dispatcher.dispatch(_fake_callback("watch_stop", user_id))
    await dispatcher.dispatch(_fake_callback("watch_stop_confirm", user_id))

    assert fsm.get_state(user_id).state is FSMState.IDLE
    assert session_store.get_watch_task(user_id) is None
    assert auto_watch_manager.status(user_id) is None
    assert active_task.cancelled() or active_task.done()


@pytest.mark.asyncio
async def test_auto_watch_distinguishes_empty_feed_failures_and_nonmatching_scans(monkeypatch) -> None:
    empty_stack = _build_stack(poll_interval_seconds_override=2.0)
    empty_dispatcher, _, empty_manager, _, _ = empty_stack
    await empty_dispatcher.dispatch(_fake_callback("watch_start", 46))
    empty_screen = _fake_callback("nav_watch", 46)
    await empty_dispatcher.dispatch(empty_screen)
    assert "no qualifying results yet" in empty_screen.message.edit_text.await_args.args[0].lower()
    await empty_manager.stop(46)

    failed_dispatcher, _, failed_manager, _, _ = _build_stack(
        discovery_provider=_FailingDiscoveryProvider(),
        poll_interval_seconds_override=2.0,
    )
    await failed_dispatcher.dispatch(_fake_callback("watch_start", 47))
    await _wait_until(
        lambda: failed_manager.status(47) is not None
        and failed_manager.status(47).last_cycle_discovery_failed
    )
    failed_screen = _fake_callback("nav_watch", 47)
    await failed_dispatcher.dispatch(failed_screen)
    assert "couldn't be reached last cycle" in failed_screen.message.edit_text.await_args.args[0].lower()
    await failed_manager.stop(47)

    candidate = _candidate(6)
    address = candidate.token_address
    nonmatch_dispatcher, _, nonmatch_manager, _, _ = _build_stack(
        discovery_provider=_FakeDiscoveryProvider([candidate]),
        market_provider=_FakeMarketProvider({address: [_pair(address, liquidity_usd=500.0)]}),
        poll_interval_seconds_override=2.0,
    )
    await nonmatch_dispatcher.dispatch(_fake_callback("watch_start", 48))
    await _wait_until(
        lambda: nonmatch_manager.status(48) is not None
        and nonmatch_manager.status(48).last_cycle_candidates_scanned == 1
    )
    nonmatch_screen = _fake_callback("nav_watch", 48)
    await nonmatch_dispatcher.dispatch(nonmatch_screen)
    assert "1 candidate was scanned but none matched" in nonmatch_screen.message.edit_text.await_args.args[0].lower()
    await nonmatch_manager.stop(48)

    async def fail_scan(*args, **kwargs):
        raise RuntimeError("simulated scan failure")

    monkeypatch.setattr(auto_watch_module, "run_scan", fail_scan)
    failing_candidate = _candidate(8)
    scan_failure_dispatcher, _, scan_failure_manager, _, _ = _build_stack(
        discovery_provider=_FakeDiscoveryProvider([failing_candidate]),
        poll_interval_seconds_override=2.0,
    )
    await scan_failure_dispatcher.dispatch(_fake_callback("watch_start", 50))
    await _wait_until(
        lambda: scan_failure_manager.status(50) is not None
        and scan_failure_manager.status(50).last_cycle_scan_failures == 1
    )
    scan_failure_screen = _fake_callback("nav_watch", 50)
    await scan_failure_dispatcher.dispatch(scan_failure_screen)
    failure_text = scan_failure_screen.message.edit_text.await_args.args[0].lower()
    assert "no candidate scans completed last cycle" in failure_text
    assert "candidate scan failures: 1" in failure_text
    await scan_failure_manager.stop(50)


@pytest.mark.asyncio
async def test_auto_watch_matches_are_browsable_and_return_to_watch_screen() -> None:
    candidate = _candidate(7)
    address = candidate.token_address
    dispatcher, fsm, manager, _, _ = _build_stack(
        discovery_provider=_FakeDiscoveryProvider([candidate]),
        market_provider=_FakeMarketProvider({address: [_pair(address)]}),
        poll_interval_seconds_override=2.0,
    )
    user_id = 49
    await dispatcher.dispatch(_fake_callback("watch_start", user_id))
    await _wait_until(
        lambda: manager.status(user_id) is not None
        and bool(manager.status(user_id).matched_result_ids)
    )

    watch_screen = _fake_callback("nav_watch", user_id)
    await dispatcher.dispatch(watch_screen)
    watch_markup = watch_screen.message.edit_text.await_args.kwargs["reply_markup"]
    assert any(
        button.callback_data == "watch_view_results"
        for row in watch_markup.inline_keyboard
        for button in row
    )

    results_screen = _fake_callback("watch_view_results", user_id)
    await dispatcher.dispatch(results_screen)
    result_text = results_screen.message.edit_text.await_args.args[0]
    result_markup = results_screen.message.edit_text.await_args.kwargs["reply_markup"]
    result_buttons = [
        button
        for row in result_markup.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("result_view:")
    ]
    assert "auto-watch matches" in result_text.lower()
    assert "matches found by auto-watch this session" in result_text.lower()
    assert len(result_buttons) == 1
    assert result_buttons[0].text.startswith(f"T{address[-1]}")
    assert fsm.get_state(user_id).state is FSMState.RESULT_READY

    result_id = result_buttons[0].callback_data.split(":")[1]
    detail = _fake_callback(result_buttons[0].callback_data, user_id)
    await dispatcher.dispatch(detail)
    assert fsm.get_state(user_id).state is FSMState.RESULT_DETAIL
    detail_buttons = [
        button.callback_data
        for row in detail.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert f"result_back_list:{result_id}" in detail_buttons

    return_to_matches = _fake_callback(f"result_back_list:{result_id}", user_id)
    await dispatcher.dispatch(return_to_matches)
    restored_text = return_to_matches.message.edit_text.await_args.args[0]
    restored_buttons = [
        button.callback_data
        for row in return_to_matches.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "auto-watch matches" in restored_text.lower()
    assert f"result_view:{result_id}" in restored_buttons
    assert fsm.get_state(user_id).state is FSMState.RESULT_READY

    back_to_watch = _fake_callback("nav_watch", user_id)
    await dispatcher.dispatch(back_to_watch)
    assert fsm.get_state(user_id).state is FSMState.AUTO_WATCH_ACTIVE
    assert manager.status(user_id) is not None
    await manager.stop(user_id)


@pytest.mark.asyncio
async def test_nav_watch_shows_active_status_after_starting() -> None:
    """The screen a person actually sees after tapping Auto-Watch from
    the Main Menu reflects the real running state - not the FSM-only
    signal test_menus.py's own unit test already covers, but the full
    handler wiring supplying a real AutoWatchStatus to it."""
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack()
    user_id = 45

    await dispatcher.dispatch(_fake_callback("watch_start", user_id))
    cq = _fake_callback("nav_watch", user_id)
    await dispatcher.dispatch(cq)

    rendered_html = cq.message.edit_text.call_args.args[0]
    assert "running" in rendered_html.lower()
    assert "Balanced" in rendered_html

    await auto_watch_manager.stop(user_id)


@pytest.mark.asyncio
async def test_auto_watch_alert_detail_back_returns_to_active_watch() -> None:
    from rendering.result_renderer import render_watch_alert

    candidate = _candidate(9)
    address = candidate.token_address
    dispatcher, fsm, manager, session_store, _alerts = _build_stack(
        discovery_provider=_FakeDiscoveryProvider([candidate]),
        market_provider=_FakeMarketProvider({address: [_pair(address)]}),
        poll_interval_seconds_override=2.0,
    )
    user_id = 60
    await dispatcher.dispatch(_fake_callback("watch_start", user_id))
    await _wait_until(
        lambda: manager.status(user_id) is not None
        and bool(manager.status(user_id).matched_result_ids)
    )
    result_id = manager.status(user_id).matched_result_ids[0]
    scored = session_store.cache_get(result_id)
    assert scored is not None

    alert = render_watch_alert(scored)
    alert_callback = next(
        button.callback_data
        for row in alert.keyboard.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("result_view:")
    )
    detail = _fake_callback(alert_callback, user_id)
    await dispatcher.dispatch(detail)
    assert fsm.get_state(user_id).state is FSMState.RESULT_DETAIL
    detail_buttons = [
        button.callback_data
        for row in detail.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "nav_watch" in detail_buttons

    back_to_watch = _fake_callback("nav_watch", user_id)
    await dispatcher.dispatch(back_to_watch)
    assert fsm.get_state(user_id).state is FSMState.AUTO_WATCH_ACTIVE
    assert manager.status(user_id) is not None
    await manager.stop(user_id)


@pytest.mark.asyncio
async def test_discovery_scan_shows_progress_before_feed_finishes_and_rejects_duplicates() -> None:
    discovery_provider = _SlowDiscoveryProvider()
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack(discovery_provider)
    user_id = 46

    scan_callback = _fake_callback("scan_trending", user_id)
    scan_dispatch = asyncio.create_task(dispatcher.dispatch(scan_callback))
    await discovery_provider.started.wait()

    assert scan_callback.message.edit_text.await_count == 1
    progress_text = scan_callback.message.edit_text.await_args.args[0]
    assert "scan" in progress_text.lower()
    assert fsm.get_state(user_id).state is FSMState.SCANNING

    duplicate_scan = _fake_callback("scan_new", user_id)
    await dispatcher.dispatch(duplicate_scan)
    duplicate_watch = _fake_callback("watch_start", user_id)
    await dispatcher.dispatch(duplicate_watch)

    assert "already running" in duplicate_scan.message.edit_text.await_args.args[0].lower()
    assert "already running" in duplicate_watch.message.edit_text.await_args.args[0].lower()
    assert session_store.get_watch_task(user_id) is None

    discovery_provider.release.set()
    await scan_dispatch
    assert fsm.get_state(user_id).state is FSMState.RESULT_READY
    assert "discovery scan complete" in scan_callback.message.edit_text.await_args.args[0].lower()
    scan_callback.message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_discovery_zero_matches_shows_explicit_empty_state_and_counts() -> None:
    candidates = [_candidate(index) for index in range(10)]
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack(
        _FakeDiscoveryProvider(candidates)
    )
    callback = _fake_callback("scan_new", 50)

    await dispatcher.dispatch(callback)

    html = callback.message.answer.await_args.args[0]
    assert "discovery scan complete" in callback.message.edit_text.await_args.args[0].lower()
    assert "no tokens passed the current screening criteria" in html.lower()
    assert "discovered: 10" in html.lower()
    assert "analyzed: 10" in html.lower()
    assert "rejected: 10" in html.lower()
    assert "displayed: 0" in html.lower()
    assert "scan failures: 0" in html.lower()
    assert "recently updated token profiles" in html.lower()
    assert fsm.get_state(50).state is FSMState.RESULT_READY

    batch_id = next(iter(session_store._batch_cache))
    assert session_store.cache_get_batch(batch_id) == (
        [],
        {
            "discovered": 10,
            "analyzed": 10,
            "rejected": 10,
            "displayed": 0,
            "failed": 0,
            "source_note": "New Listings reflects recently updated token profiles, not necessarily newly created pools.",
        },
    )


@pytest.mark.asyncio
async def test_discovery_screens_three_of_ten_and_reports_paid_boost_disclosure() -> None:
    candidates = [_candidate(index) for index in range(10)]
    qualifying_pairs = {
        candidate.token_address: [_pair(candidate.token_address)]
        for candidate in candidates[:3]
    }
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack(
        _FakeDiscoveryProvider(candidates),
        market_provider=_FakeMarketProvider(qualifying_pairs),
    )
    callback = _fake_callback("scan_trending", 51)

    await dispatcher.dispatch(callback)

    html = callback.message.answer.await_args.args[0]
    assert "discovery scan complete" in callback.message.edit_text.await_args.args[0].lower()
    assert "discovered: 10" in html.lower()
    assert "analyzed: 10" in html.lower()
    assert "rejected: 7" in html.lower()
    assert "displayed: 3" in html.lower()
    assert "paid promotion, not evidence of organic interest" in html.lower()
    result_buttons = [
        button
        for row in callback.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data.startswith("result_view:")
    ]
    assert len(result_buttons) == 3

    batch_id = next(iter(session_store._batch_cache))
    result_ids, meta = session_store.cache_get_batch(batch_id)
    assert len(result_ids) == 3
    assert meta == {
        "discovered": 10,
        "analyzed": 10,
        "rejected": 7,
        "displayed": 3,
        "failed": 0,
        "source_note": "Trending includes paid boosts; a boost is paid promotion, not evidence of organic interest.",
    }
    assert fsm.get_state(51).state is FSMState.RESULT_READY


@pytest.mark.asyncio
async def test_discovery_deduplicates_addresses_within_the_batch() -> None:
    candidates = [_candidate(0), _candidate(0), _candidate(1)]
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack(
        _FakeDiscoveryProvider(candidates)
    )
    callback = _fake_callback("scan_new", 52)

    await dispatcher.dispatch(callback)

    html = callback.message.answer.await_args.args[0]
    assert "discovered: 2" in html.lower()
    assert "analyzed: 2" in html.lower()


@pytest.mark.asyncio
async def test_discovery_scan_failure_is_not_counted_as_rejected(monkeypatch) -> None:
    import handlers.watch_handler as watch_handler_module

    candidates = [_candidate(index) for index in range(3)]
    failing_address = candidates[1].token_address
    market_provider = _FakeMarketProvider({candidates[0].token_address: [_pair(candidates[0].token_address)]})
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack(
        _FakeDiscoveryProvider(candidates), market_provider=market_provider
    )
    real_run_scan = watch_handler_module.run_scan

    async def fail_one(address, *args, **kwargs):
        if address == failing_address:
            raise RuntimeError("fixture scan failure")
        return await real_run_scan(address, *args, **kwargs)

    monkeypatch.setattr(watch_handler_module, "run_scan", fail_one)
    callback = _fake_callback("scan_new", 53)

    await dispatcher.dispatch(callback)

    html = callback.message.answer.await_args.args[0].lower()
    assert "discovered: 3" in html
    assert "analyzed: 2" in html
    assert "rejected: 1" in html
    assert "displayed: 1" in html
    assert "scan failures: 1" in html


@pytest.mark.asyncio
async def test_discovery_uses_selected_autowatch_profile() -> None:
    candidate = _candidate(0)
    market_provider = _FakeMarketProvider({candidate.token_address: [_pair(candidate.token_address, 5_000.0)]})
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack(
        _FakeDiscoveryProvider([candidate]), market_provider=market_provider
    )
    user_id = 54

    await dispatcher.dispatch(_fake_callback("rule_preset:aggressive", user_id))
    selected_callback = _fake_callback("scan_new", user_id)
    await dispatcher.dispatch(selected_callback)
    assert "displayed: 1" in selected_callback.message.answer.await_args.args[0].lower()

    default_callback = _fake_callback("scan_new", 55)
    await dispatcher.dispatch(default_callback)
    assert "displayed: 0" in default_callback.message.answer.await_args.args[0].lower()
    assert fsm.get_state(user_id).state is FSMState.RESULT_READY


@pytest.mark.asyncio
async def test_batch_pagination_renders_cached_second_page_without_rescanning() -> None:
    candidates = [_candidate(index) for index in range(7)]
    market_provider = _FakeMarketProvider(
        {
            candidate.token_address: [_pair(candidate.token_address)]
            for candidate in candidates
        }
    )
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack(
        _FakeDiscoveryProvider(candidates),
        market_provider=market_provider,
    )
    user_id = 56
    initial_callback = _fake_callback("scan_trending", user_id)

    await dispatcher.dispatch(initial_callback)
    batch_id = next(iter(session_store._batch_cache))
    result_ids, _meta = session_store.cache_get_batch(batch_id)
    callback_data = f"result_page:{batch_id}:1"
    initial_callbacks = [
        button.callback_data
        for row in initial_callback.message.answer.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert callback_data in initial_callbacks
    assert len(callback_data.encode("utf-8")) <= 64
    assert len(result_ids) == 7
    calls_before_page_turn = market_provider.calls

    page_callback = _fake_callback(callback_data, user_id)
    await dispatcher.dispatch(page_callback)

    assert market_provider.calls == calls_before_page_turn
    assert fsm.get_state(user_id).state is FSMState.RESULT_READY
    page_buttons = [
        button.callback_data
        for row in page_callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert [value for value in page_buttons if value.startswith("result_view:")] == [
        f"result_view:{result_id}" for result_id in result_ids[5:]
    ]
    assert f"result_page:{batch_id}:0" in page_buttons

    last_result_id = result_ids[-1]
    detail_callback = _fake_callback(f"result_view:{last_result_id}", user_id)
    await dispatcher.dispatch(detail_callback)
    back_callback = _fake_callback(f"result_back_list:{last_result_id}", user_id)
    await dispatcher.dispatch(back_callback)
    restored_html = back_callback.message.edit_text.await_args.args[0]
    restored_markup_buttons = [
        button
        for row in back_callback.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "discovered: 7" in restored_html.lower()
    assert "2/2" in [button.text for button in restored_markup_buttons]
    restored_buttons = [button.callback_data for button in restored_markup_buttons]
    assert f"result_page:{batch_id}:0" in restored_buttons
    assert [value for value in restored_buttons if value.startswith("result_view:")] == [
        f"result_view:{result_id}" for result_id in result_ids[5:]
    ]


@pytest.mark.asyncio
async def test_unknown_or_malformed_batch_page_shows_expired_recovery() -> None:
    dispatcher, fsm, auto_watch_manager, session_store, alerts = _build_stack()

    for callback_data in ("result_page:missing-batch:1", "result_page:batch-without-page"):
        callback = _fake_callback(callback_data, 57)
        await dispatcher.dispatch(callback)
        rendered = callback.message.edit_text.await_args.args[0].lower()
        assert "expired" in rendered
