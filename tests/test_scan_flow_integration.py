"""
Playbook reference: Unified Developer Playbook, Part VIII Step 7 - Unit
Testing Requirements: "Integration test: full fixture-driven scan flow
from AwaitingAddress to ResultDetail state, asserting the FSM ends in the
correct state and the cached ScoredResult matches what was rendered."
Integration Requirement: "the first real integration test of the whole
stack" (Steps 2+3+4+5+6+7 chained together).

Uses aiogram message types with `MagicMock(spec=...)` and fixture-backed
provider fakes, so the complete handler chain runs without live APIs.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message, User

from analysis.core_engine import CoreEngine
from analysis.holder_engine import HolderEngine
from analysis.momentum_engine import MomentumEngine
from analysis.providers.dexscreener_parser import parse_pairs_response
from analysis.providers.rugcheck_parser import parse_report
from analysis.security_engine import SecurityEngine
from analysis.social_engine import SocialEngine
from bot.constants import FSMState
from handlers.dispatcher import Dispatcher
from handlers.navigation import build_navigation_handlers
from handlers.scan_handler import ScanHandler
from handlers.scan_orchestration import ScoredResult
from scoring.pipeline import ScoringPipeline
from state.fsm import FSMEngine
from state.session_store import SessionStore

_DEXSCREENER_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "dexscreener"
_RUGCHECK_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "rugcheck"


class _FakeMarketProvider:
    def __init__(self, pairs: list) -> None:
        self._pairs = pairs

    async def get_pairs(self, address: str) -> list:
        return self._pairs


class _SlowMarketProvider(_FakeMarketProvider):
    def __init__(self, pairs: list) -> None:
        super().__init__(pairs)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = 0

    async def get_pairs(self, address: str) -> list:
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return await super().get_pairs(address)


class _FakeSecurityProvider:
    def __init__(self, report) -> None:
        self._report = report

    async def scan(self, address: str, chain) -> object:
        return self._report


class _SlowSecurityProvider(_FakeSecurityProvider):
    def __init__(self, report) -> None:
        super().__init__(report)
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.release = asyncio.Event()

    async def scan(self, address: str, chain) -> object:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return await super().scan(address, chain)


class _FakeHolderProvider:
    """Empty holders/funding - a valid, non-degraded HolderResult, not an
    error path. This integration test's own scope (Steps 2+3+4+5+6+7 in
    the original docstring, extended to include Step 8's wiring here) is
    the FSM/dispatch/render chain, not Holder Engine's own formula
    behavior - that's test_holder_engine.py's job."""

    async def get_holders(self, address: str, chain) -> list:
        return []

    async def get_launch_block_funding(self, address: str, chain) -> list:
        return []


class _FakeSocialProvider:
    """Same scope note as _FakeHolderProvider above, extended to Social
    (Step 14) - always-degrades is enough; this test's own job is the
    FSM/dispatch/render chain, not Social Engine's own behavior."""

    async def lookup_user(self, handle: str) -> object:
        raise RuntimeError("fake social provider - lookup always fails")

    async def list_followers(self, handle: str, limit: int) -> list:
        return []

    async def search_mentions(self, ticker: str) -> list:
        return []


def _build_stack(dex_fixture: str, rc_fixture: str, market_provider=None, security_provider=None):
    pairs = parse_pairs_response(json.loads((_DEXSCREENER_FIXTURES / dex_fixture).read_text()))
    report = parse_report(json.loads((_RUGCHECK_FIXTURES / rc_fixture).read_text()), chain_supported=True)

    session_store = SessionStore()
    fsm = FSMEngine(session_store)
    core_engine = CoreEngine(market_provider or _FakeMarketProvider(pairs))
    security_engine = SecurityEngine(security_provider or _FakeSecurityProvider(report))
    holder_engine = HolderEngine(_FakeHolderProvider())
    # No fake needed: MomentumEngine is synchronous and provider-free
    # (Step 9's own Scope note) - the real class is used directly.
    momentum_engine = MomentumEngine()
    social_engine = SocialEngine(_FakeSocialProvider())
    scoring_pipeline = ScoringPipeline()

    scan_handler = ScanHandler(
        fsm, core_engine, security_engine, holder_engine, momentum_engine, social_engine,
        scoring_pipeline, session_store,
    )
    dispatcher = Dispatcher(fsm)
    for handler in build_navigation_handlers(fsm, session_store, extra_handlers=[scan_handler]):
        dispatcher.register(handler)

    return dispatcher, fsm, session_store


def _fake_user(user_id: int = 1) -> User:
    return MagicMock(spec=User, id=user_id)


def _fake_callback(data: str, user_id: int = 1) -> CallbackQuery:
    cq = MagicMock(spec=CallbackQuery)
    cq.data = data
    cq.from_user = _fake_user(user_id)
    cq.answer = AsyncMock()
    cq.message = MagicMock(spec=Message)
    cq.message.edit_text = AsyncMock()
    cq.message.answer = AsyncMock()
    return cq


def _fake_message(text: str, user_id: int = 1) -> Message:
    msg = MagicMock(spec=Message)
    msg.text = text
    msg.from_user = _fake_user(user_id)
    reply_message = MagicMock(spec=Message)
    reply_message.edit_text = AsyncMock()
    msg.answer = AsyncMock(return_value=reply_message)
    return msg


@pytest.mark.asyncio
async def test_full_scan_flow_awaiting_address_to_result_ready() -> None:
    """Scan Menu's paste button -> user pastes a valid address -> ends
    in ResultReady, per Part VIII Step 7's stated flow (this test stops
    short of tapping into ResultDetail - the next test covers that leg
    separately, matching the Unit Testing Requirement's explicit
    "AwaitingAddress to ResultDetail" wording as two connected legs)."""
    dispatcher, fsm, session_store = _build_stack("solana_valid.json", "fully_safe.json")
    user_id = 1

    # Step 1: tap "Paste Contract Address" on Scan Menu
    start_cb = _fake_callback("scan_paste", user_id)
    await dispatcher.dispatch(start_cb)
    assert fsm.get_state(user_id).state is FSMState.AWAITING_ADDRESS

    # Step 2: user sends the address as a plain message
    address_msg = _fake_message("So11111111111111111111111111111111111111112", user_id)
    await dispatcher.dispatch(address_msg)

    assert fsm.get_state(user_id).state is FSMState.RESULT_READY
    # The reply message should have been progressively edited at least
    # twice (initial progress -> final result), never left un-edited.
    reply_message = address_msg.answer.return_value
    assert reply_message.edit_text.await_count >= 1


@pytest.mark.asyncio
async def test_invalid_pasted_address_stays_in_awaiting_address() -> None:
    dispatcher, fsm, session_store = _build_stack("solana_valid.json", "fully_safe.json")
    user_id = 2

    await dispatcher.dispatch(_fake_callback("scan_paste", user_id))
    assert fsm.get_state(user_id).state is FSMState.AWAITING_ADDRESS

    bad_msg = _fake_message("definitely not an address", user_id)
    await dispatcher.dispatch(bad_msg)

    # Must NOT have advanced to Scanning/ResultReady - shape validation
    # rejects before any engine call.
    assert fsm.get_state(user_id).state is FSMState.AWAITING_ADDRESS
    bad_msg.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_valid_address_from_idle_starts_scan_without_paste_prompt() -> None:
    dispatcher, fsm, session_store = _build_stack("solana_valid.json", "fully_safe.json")
    user_id = 10
    address_message = _fake_message("So11111111111111111111111111111111111111112", user_id)

    await dispatcher.dispatch(address_message)

    assert fsm.get_state(user_id).state is FSMState.RESULT_READY
    assert address_message.answer.await_count == 1
    assert address_message.answer.return_value.edit_text.await_count >= 1
    assert len(session_store._uuid_cache) == 1


@pytest.mark.asyncio
async def test_non_address_from_idle_still_uses_catch_all() -> None:
    dispatcher, fsm, session_store = _build_stack("solana_valid.json", "fully_safe.json")
    user_id = 11
    message = _fake_message("hello DexScan", user_id)

    await dispatcher.dispatch(message)

    assert fsm.get_state(user_id).state is FSMState.IDLE
    assert "expired" in message.answer.await_args.args[0].lower()
    assert session_store._uuid_cache == {}


@pytest.mark.asyncio
async def test_valid_address_is_not_intercepted_from_trade_staging() -> None:
    dispatcher, fsm, session_store = _build_stack("solana_valid.json", "fully_safe.json")
    user_id = 12
    for state in (
        FSMState.AWAITING_ADDRESS,
        FSMState.SCANNING,
        FSMState.RESULT_READY,
        FSMState.RESULT_DETAIL,
        FSMState.TRADE_STAGING,
    ):
        fsm.transition(user_id, state)

    message = _fake_message("So11111111111111111111111111111111111111112", user_id)
    await dispatcher.dispatch(message)

    assert session_store._uuid_cache == {}
    assert fsm.get_state(user_id).state is FSMState.IDLE


@pytest.mark.asyncio
async def test_recent_results_shows_only_the_current_users_session_history() -> None:
    dispatcher, fsm, session_store = _build_stack("solana_valid.json", "fully_safe.json")
    user_id = 13
    address = "So11111111111111111111111111111111111111112"

    await dispatcher.dispatch(_fake_message(address, user_id))
    result_id = session_store.get_user_history(user_id)[0]
    scored = session_store.cache_get(result_id)
    assert isinstance(scored, ScoredResult)
    symbol = scored.core.primary_pair.base_token_symbol

    for _ in range(5):
        extra_result = dataclasses.replace(scored, result_id="")
        extra_id = session_store.cache_put(extra_result, user_id=user_id)
        extra_result.result_id = extra_id

    own_history = _fake_callback("nav_recent", user_id)
    await dispatcher.dispatch(own_history)
    own_text = own_history.message.edit_text.await_args.args[0]
    own_keyboard = own_history.message.edit_text.await_args.kwargs["reply_markup"]
    own_buttons = [button for row in own_keyboard.inline_keyboard for button in row]
    assert any(symbol in button.text for button in own_buttons)
    assert "this session only" in own_text.lower()
    assert "clears when the bot restarts" in own_text.lower()
    next_page = next(
        (button.callback_data for button in own_buttons if (button.callback_data or "").startswith("result_page:")),
        None,
    )
    assert next_page is not None
    page_callback = _fake_callback(next_page, user_id)
    await dispatcher.dispatch(page_callback)
    page_text = page_callback.message.edit_text.await_args.args[0]
    assert "recent results &amp; stats" in page_text.lower()
    assert "this session only" in page_text.lower()
    assert fsm.get_state(user_id).state is FSMState.RESULT_READY
    page_markup = page_callback.message.edit_text.await_args.kwargs["reply_markup"]
    detail_callback_data = next(
        button.callback_data
        for row in page_markup.inline_keyboard
        for button in row
        if (button.callback_data or "").startswith("result_view:")
    )
    result_id = detail_callback_data.split(":")[1]
    detail = _fake_callback(detail_callback_data, user_id)
    await dispatcher.dispatch(detail)
    back_to_history = _fake_callback(f"result_back_list:{result_id}", user_id)
    await dispatcher.dispatch(back_to_history)
    restored_text = back_to_history.message.edit_text.await_args.args[0]
    restored_buttons = [
        button.callback_data
        for row in back_to_history.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "recent results &amp; stats" in restored_text.lower()
    batch_id = next_page.split(":")[1]
    assert f"result_page:{batch_id}:0" in restored_buttons
    assert f"result_view:{result_id}" in restored_buttons
    assert fsm.get_state(user_id).state is FSMState.RESULT_READY

    other_history = _fake_callback("nav_recent", user_id + 1)
    await dispatcher.dispatch(other_history)
    other_text = other_history.message.edit_text.await_args.args[0]
    other_keyboard = other_history.message.edit_text.await_args.kwargs["reply_markup"]
    other_buttons = [button for row in other_keyboard.inline_keyboard for button in row]
    assert "no scans yet this session" in other_text.lower()
    assert all(not (button.callback_data or "").startswith("result_view:") for button in other_buttons)
    assert fsm.get_state(user_id + 1).state is FSMState.RESULT_READY


@pytest.mark.asyncio
async def test_full_flow_ends_at_result_detail_with_cached_result_matching_render() -> None:
    """The Unit Testing Requirement's literal scenario: AwaitingAddress
    all the way to ResultDetail, asserting the FSM's final state AND that
    the cached ScoredResult is the same one the detail view rendered
    from."""
    dispatcher, fsm, session_store = _build_stack("solana_valid.json", "honeypot.json")
    user_id = 3

    await dispatcher.dispatch(_fake_callback("scan_paste", user_id))
    address_msg = _fake_message("So11111111111111111111111111111111111111112", user_id)
    await dispatcher.dispatch(address_msg)
    assert fsm.get_state(user_id).state is FSMState.RESULT_READY

    # Find the result_id the flow actually cached, the same way a real
    # button tap would carry it: read back what the final render call
    # was given, via the ScoredResult now sitting in the store (there's
    # exactly one entry after a single scan in this test).
    cached_ids = list(session_store._uuid_cache.keys())  # test-only introspection
    assert len(cached_ids) == 1
    result_id = cached_ids[0]
    scored = session_store.cache_get(result_id)
    assert isinstance(scored, ScoredResult)

    detail_cb = _fake_callback(f"result_view:{result_id}", user_id)
    await dispatcher.dispatch(detail_cb)

    assert fsm.get_state(user_id).state is FSMState.RESULT_DETAIL
    detail_cb.message.answer.assert_awaited_once()
    detail_cb.message.edit_text.assert_not_awaited()
    rendered_html = detail_cb.message.answer.await_args.args[0]
    # The cached result's own security tier (Critical Risk, from the
    # honeypot fixture) must appear in what was actually rendered - ties
    # the FSM-state assertion to the actual rendered content, not just
    # "some edit happened."
    assert scored.scoring.tier_label in rendered_html


@pytest.mark.asyncio
async def test_stale_result_id_recovers_gracefully() -> None:
    dispatcher, fsm, session_store = _build_stack("solana_valid.json", "fully_safe.json")
    user_id = 4

    stale_cb = _fake_callback("result_view:does-not-exist", user_id)
    await dispatcher.dispatch(stale_cb)

    stale_cb.message.edit_text.assert_awaited_once()
    rendered_html = stale_cb.message.edit_text.await_args.args[0]
    assert "expired" in rendered_html.lower()


@pytest.mark.asyncio
async def test_rescan_reuses_original_address_and_produces_a_new_cache_entry() -> None:
    """Rescan only exists as a button on Result Detail (rendering/
    result_renderer.py), never on Result List - a correct test reaches it
    the same way a real user does: paste -> ResultReady -> tap the result
    -> ResultDetail -> Rescan. An earlier version of this test skipped
    straight from ResultReady to rescan and passed here on paper, but
    failed the first time it was actually run with real pytest
    (state.fsm.InvalidTransitionError: result_ready -> scanning) -
    RESULT_DETAIL -> SCANNING wasn't in state/fsm.py's adjacency map at
    all (documented addition #5, added once this was found). Both the
    map and this test were wrong in the same direction; fixing only one
    would have left them silently disagreeing again."""
    dispatcher, fsm, session_store = _build_stack("solana_valid.json", "fully_safe.json")
    user_id = 5

    await dispatcher.dispatch(_fake_callback("scan_paste", user_id))
    await dispatcher.dispatch(_fake_message("So11111111111111111111111111111111111111112", user_id))
    first_id = next(iter(session_store._uuid_cache.keys()))

    # Reach Result Detail first - this is the real path to the Rescan button.
    detail_cb = _fake_callback(f"result_view:{first_id}", user_id)
    await dispatcher.dispatch(detail_cb)
    assert fsm.get_state(user_id).state is FSMState.RESULT_DETAIL

    rescan_cb = _fake_callback(f"scan_rescan:{first_id}", user_id)
    await dispatcher.dispatch(rescan_cb)

    assert fsm.get_state(user_id).state is FSMState.RESULT_READY
    assert len(session_store._uuid_cache) == 2  # original + rescanned, both retained (Part I.3: in-memory, no cleanup policy yet)


@pytest.mark.asyncio
async def test_scan_cancel_returns_to_idle_with_honest_message_not_stale_error() -> None:
    """STEP 16 regression test: "scan_cancel" is the ONLY button on the
    live-scanning screen (Playbook Part II.9's Scanning/Progress row) and
    had no handler anywhere - found during this step's FSM/callback
    audit. It used to fall through to UnknownInputHandler's "that menu
    has expired" message, which is misleading (nothing expired; the user
    cancelled on purpose). Placed mid-SCANNING directly (not via a real
    in-flight scan) since this test's job is the cancel handler itself,
    not scan cancellation-token behavior - see this handler's own code
    comment for why interrupting an in-flight scan is a separate,
    out-of-scope concern this fix deliberately does not attempt."""
    dispatcher, fsm, session_store = _build_stack("solana_valid.json", "fully_safe.json")
    user_id = 6
    fsm.transition(user_id, FSMState.SCANNING)

    cancel_cb = _fake_callback("scan_cancel", user_id)
    await dispatcher.dispatch(cancel_cb)

    assert fsm.get_state(user_id).state is FSMState.IDLE
    cancel_cb.message.edit_text.assert_awaited_once()
    rendered_html = cancel_cb.message.edit_text.await_args.args[0]
    assert "expired" not in rendered_html.lower()
    assert "cancelled" in rendered_html.lower()


@pytest.mark.asyncio
async def test_second_scan_paste_is_rejected_while_first_scan_is_in_flight() -> None:
    pairs = parse_pairs_response(json.loads((_DEXSCREENER_FIXTURES / "solana_valid.json").read_text()))
    market_provider = _SlowMarketProvider(pairs)
    dispatcher, fsm, session_store = _build_stack(
        "solana_valid.json", "fully_safe.json", market_provider=market_provider
    )
    user_id = 7
    address = "So11111111111111111111111111111111111111112"

    await dispatcher.dispatch(_fake_callback("scan_paste", user_id))
    first_message = _fake_message(address, user_id)
    first_dispatch = asyncio.create_task(dispatcher.dispatch(first_message))
    await market_provider.started.wait()
    active_task = session_store.get_scan_task(user_id)
    assert active_task is not None and not active_task.done()

    second_prompt = _fake_callback("scan_paste", user_id)
    await dispatcher.dispatch(second_prompt)
    second_message = _fake_message(address, user_id)
    await dispatcher.dispatch(second_message)

    assert fsm.get_state(user_id).state is FSMState.SCANNING
    assert "already running" in second_prompt.message.edit_text.await_args.args[0].lower()
    assert "already running" in second_message.answer.await_args.args[0].lower()
    assert market_provider.calls == 1

    market_provider.release.set()
    await first_dispatch
    assert fsm.get_state(user_id).state is FSMState.RESULT_READY


@pytest.mark.asyncio
async def test_scan_cancel_cancels_the_in_flight_engine_task() -> None:
    pairs = parse_pairs_response(json.loads((_DEXSCREENER_FIXTURES / "solana_valid.json").read_text()))
    market_provider = _SlowMarketProvider(pairs)
    dispatcher, fsm, session_store = _build_stack(
        "solana_valid.json", "fully_safe.json", market_provider=market_provider
    )
    user_id = 8
    address_message = _fake_message("So11111111111111111111111111111111111111112", user_id)

    await dispatcher.dispatch(_fake_callback("scan_paste", user_id))
    first_dispatch = asyncio.create_task(dispatcher.dispatch(address_message))
    await market_provider.started.wait()
    scan_task = session_store.get_scan_task(user_id)
    assert scan_task is not None and not scan_task.done()

    cancel_callback = _fake_callback("scan_cancel", user_id)
    cancel_callback.message = address_message.answer.return_value
    await dispatcher.dispatch(cancel_callback)
    await first_dispatch

    assert scan_task.cancelled()
    assert market_provider.cancelled.is_set()
    assert session_store.get_scan_task(user_id) is None
    assert fsm.get_state(user_id).state is FSMState.IDLE
    assert "cancelled" in cancel_callback.message.edit_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_cancelling_during_parallel_engines_leaves_no_partial_result() -> None:
    report = parse_report(
        json.loads((_RUGCHECK_FIXTURES / "fully_safe.json").read_text()),
        chain_supported=True,
    )
    security_provider = _SlowSecurityProvider(report)
    dispatcher, fsm, session_store = _build_stack(
        "solana_valid.json",
        "fully_safe.json",
        security_provider=security_provider,
    )
    user_id = 9

    await dispatcher.dispatch(_fake_callback("scan_paste", user_id))
    address_message = _fake_message("So11111111111111111111111111111111111111112", user_id)
    first_dispatch = asyncio.create_task(dispatcher.dispatch(address_message))
    await security_provider.started.wait()
    scan_task = session_store.get_scan_task(user_id)
    assert scan_task is not None and not scan_task.done()

    cancel_callback = _fake_callback("scan_cancel", user_id)
    cancel_callback.message = address_message.answer.return_value
    await dispatcher.dispatch(cancel_callback)
    await first_dispatch

    assert scan_task.cancelled()
    assert security_provider.cancelled.is_set()
    assert session_store._uuid_cache == {}
    assert fsm.get_state(user_id).state is FSMState.IDLE
