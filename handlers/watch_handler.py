"""
Layer: Handlers — Auto-Watch, Filter Presets, and on-demand discovery
scans (Playbook Part VIII Step 12; landed as custom-roadmap Step 13).

The Telegram-I/O shell around `handlers.auto_watch.AutoWatchManager` and
`analysis.filter_presets` — same split as `scan_handler.py`/
`scan_orchestration.py` (Part V.2): the manager and the preset logic
stay aiogram-free and independently testable; this file is what actually
sends/edits messages, reads callback params, and transitions FSM state.

Owns Auto-Watch lifecycle and visibility callbacks, `rule_preset:*`
(Filter Config's presets), and `scan_trending`/`scan_new` (Scan Menu's
two discovery buttons — Paste was already `ScanHandler`'s). Also takes
over `nav_watch`/`nav_filters` from `handlers/navigation.py`'s
`_NAV_TARGETS`, since both need real per-user data that lookup table
cannot supply.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram.types import CallbackQuery

from analysis.core_engine import CoreEngine
from analysis.filter_presets import (
    FilterProfile,
    NAMED_PRESETS,
    cycle_numeric_field,
    default_custom_profile,
    matches,
    set_bool_field,
)
from analysis.holder_engine import HolderEngine
from analysis.momentum_engine import MomentumEngine
from analysis.security_engine import SecurityEngine
from analysis.social_engine import SocialEngine
from bot.constants import EVM_CHAINS, Chain, FSMState
from bot.types import EngineStatus
from handlers.auto_watch import AutoWatchManager
from handlers.base import TelegramEvent, reply, send_new_rendered, send_rendered
from handlers.callback_parser import parse_callback
from handlers.scan_orchestration import ScoredResult, run_scan
from rendering.error_renderer import RECOVERY_HOME
from rendering.error_renderer import render_error
from rendering.menus import (
    render_advanced_rules,
    render_auto_watch,
    render_filter_config,
    render_my_filters,
    render_plain,
    render_scanning_progress,
)
from rendering.result_renderer import render_result_list, render_watch_alert
from scoring.pipeline import ScoringPipeline
from state.fsm import FSMContext, FSMEngine
from state.session_store import SessionStore

logger = logging.getLogger(__name__)

_OWNED_COMMANDS = frozenset(
    {
        "nav_watch", "nav_filters", "watch_start", "watch_stop",
        "watch_stop_confirm", "watch_stop_cancel", "watch_view_results",
        "rule_preset", "scan_trending", "scan_new",
        # Step 12 revalidation additions: Advanced Rules / Custom Filter,
        # previously unhandled placeholders (rule_advanced had no owner
        # at all; rule_tgl/rule_num were anticipated in the callback
        # parser's own docstring/tests but never wired to a real screen).
        "rule_advanced", "rule_tgl", "rule_num", "rule_save",
    }
)

# Auto-Watch's own reasonable default when a user taps Start without ever
# visiting Filter Config first (Part VI "on ambiguity": most reasonable
# default, documented, not a silent guess) — Balanced, not the loosest or
# strictest option, so an un-configured watch doesn't surprise anyone in
# either direction.
_DEFAULT_INTERVAL_MIN = 10
_MAX_DISCOVERY_SCAN_CANDIDATES = 10  # bounds an on-demand Trending/New tap's own latency + provider cost

_DISCOVERY_FETCH_FAILED_ERROR = "Couldn't reach the discovery feed just now."
_SCAN_ALREADY_RUNNING_HTML = "<b>A scan is already running.</b> Please wait for it to finish or cancel it first."


class WatchHandler:
    def __init__(
        self,
        fsm: FSMEngine,
        auto_watch_manager: AutoWatchManager,
        discovery_provider,  # TokenDiscoveryProvider (Protocol - no runtime import needed)
        core_engine: CoreEngine,
        security_engine: SecurityEngine,
        holder_engine: HolderEngine,
        momentum_engine: MomentumEngine,
        social_engine: SocialEngine,
        scoring_pipeline: ScoringPipeline,
        session_store: SessionStore,
    ) -> None:
        self._fsm = fsm
        self._auto_watch_manager = auto_watch_manager
        self._discovery_provider = discovery_provider
        self._core_engine = core_engine
        self._security_engine = security_engine
        self._holder_engine = holder_engine
        self._momentum_engine = momentum_engine
        self._social_engine = social_engine
        self._scoring_pipeline = scoring_pipeline
        self._session_store = session_store

    async def can_handle(self, event: TelegramEvent, ctx: FSMContext) -> bool:
        if not isinstance(event, CallbackQuery):
            return False
        parsed = parse_callback(event.data or "")
        return parsed.command in _OWNED_COMMANDS

    async def handle(self, event: TelegramEvent, ctx: FSMContext) -> None:
        assert isinstance(event, CallbackQuery) and event.from_user is not None
        user_id = event.from_user.id
        parsed = parse_callback(event.data or "")

        if parsed.command == "nav_watch":
            status = self._auto_watch_manager.status(user_id)
            # Auto-Watch's FSM state must reflect whether a watch is
            # ACTUALLY running, not just "the user navigated here" - a
            # real bug caught by tests.test_watch_flow_integration while
            # building this handler: transitioning to IDLE unconditionally
            # (matching every other nav_* jump's pattern) made the screen
            # show "stopped" even while a watch was genuinely active,
            # since render_auto_watch's button is driven by FSM state,
            # not by `status` alone (rendering/menus.py's own docstring).
            target_state = FSMState.AUTO_WATCH_ACTIVE if status is not None else FSMState.IDLE
            new_ctx = self._fsm.transition(user_id, target_state)
            await send_rendered(event, render_auto_watch(new_ctx, status=status))
            return

        if parsed.command == "nav_filters":
            self._fsm.transition(user_id, FSMState.IDLE)
            await send_rendered(event, render_my_filters())
            return

        if parsed.command == "rule_preset":
            await self._handle_preset_selected(event, user_id, parsed.params)
            return

        if parsed.command == "rule_advanced":
            await self._handle_advanced_rules_entry(event, user_id, ctx)
            return

        if parsed.command == "rule_tgl":
            await self._handle_toggle_field(event, user_id, ctx, parsed.params)
            return

        if parsed.command == "rule_num":
            await self._handle_cycle_field(event, user_id, ctx, parsed.params)
            return

        if parsed.command == "rule_save":
            await self._handle_save_custom(event, user_id, ctx)
            return

        if parsed.command == "watch_start":
            await self._handle_watch_start(event, user_id, ctx)
            return

        if parsed.command == "watch_stop":
            await self._handle_watch_stop_request(event, user_id, ctx)
            return

        if parsed.command == "watch_stop_confirm":
            await self._handle_watch_stop_confirm(event, user_id)
            return

        if parsed.command == "watch_stop_cancel":
            await self._handle_watch_stop_cancel(event, user_id, ctx)
            return

        if parsed.command == "watch_view_results":
            await self._handle_watch_view_results(event, user_id)
            return

        if parsed.command in ("scan_trending", "scan_new"):
            await self._handle_discovery_scan(event, user_id, source=parsed.command, ctx=ctx)
            return

    async def _handle_preset_selected(self, event: CallbackQuery, user_id: int, params: list[str]) -> None:
        """Stores the choice in the FSM's own payload
        (`state.fsm.FSMEngine.transition`'s `**payload` merge) rather
        than a new dedicated store — this is exactly the kind of small,
        per-user, session-scoped value that payload already exists for
        (Part I.3: no persistence beyond a session either way).
        `_handle_watch_start` reads it back via `ctx.payload.get(...)`."""
        preset_key = params[0] if params else "balanced"
        if preset_key not in NAMED_PRESETS and preset_key != "custom":
            preset_key = "balanced"
        new_ctx = self._fsm.transition(user_id, FSMState.IDLE, selected_filter_preset=preset_key)
        await send_rendered(event, render_filter_config(selected_preset=new_ctx.payload.get("selected_filter_preset")))

    async def _handle_advanced_rules_entry(self, event: CallbackQuery, user_id: int, ctx: FSMContext) -> None:
        """rule_advanced: Filter Config -> Advanced Rules (Part II.5 site
        map). Resumes this user's in-progress draft if one already exists
        in payload, or seeds a fresh one from Balanced otherwise - same
        payload-as-session-scoped-store pattern _handle_preset_selected
        already uses for `selected_filter_preset`."""
        draft = ctx.payload.get("custom_filter_draft") or default_custom_profile()
        new_ctx = self._fsm.transition(user_id, FSMState.CONFIGURING_FILTER, custom_filter_draft=draft)
        await send_rendered(event, render_advanced_rules(new_ctx.payload["custom_filter_draft"]))

    async def _handle_toggle_field(
        self, event: CallbackQuery, user_id: int, ctx: FSMContext, params: list[str]
    ) -> None:
        """rule_tgl:<field_key>:<on|off>. Sets the field to the explicit
        value the tapped button encoded (see filter_presets.set_bool_field
        for why that's safer than a blind flip against a stale button).
        A malformed callback (missing params) re-renders the current
        draft unchanged rather than raising - the tap simply has no
        effect, which is the correct behavior for input this app itself
        generated and a client redraw will immediately correct anyway."""
        draft = ctx.payload.get("custom_filter_draft") or default_custom_profile()
        if len(params) >= 2:
            draft = set_bool_field(draft, params[0], value=(params[1] == "on"))
        new_ctx = self._fsm.transition(user_id, FSMState.CONFIGURING_FILTER, custom_filter_draft=draft)
        await send_rendered(event, render_advanced_rules(new_ctx.payload["custom_filter_draft"]))

    async def _handle_cycle_field(
        self, event: CallbackQuery, user_id: int, ctx: FSMContext, params: list[str]
    ) -> None:
        """rule_num:<field_key>. Mirrors _handle_toggle_field one layer
        down (cycle_numeric_field instead of set_bool_field)."""
        draft = ctx.payload.get("custom_filter_draft") or default_custom_profile()
        if params:
            draft = cycle_numeric_field(draft, params[0])
        new_ctx = self._fsm.transition(user_id, FSMState.CONFIGURING_FILTER, custom_filter_draft=draft)
        await send_rendered(event, render_advanced_rules(new_ctx.payload["custom_filter_draft"]))

    async def _handle_save_custom(self, event: CallbackQuery, user_id: int, ctx: FSMContext) -> None:
        """rule_save: commits the in-progress draft as this user's
        selected preset. Sets *both* `selected_filter_preset="custom"`
        and keeps `custom_filter_draft` in payload - `_handle_watch_start`
        reads the draft object directly for "custom" rather than doing a
        NAMED_PRESETS lookup that has no "custom" key, which was this
        pass's whole bug. Falls back to a fresh Balanced-seeded draft if
        somehow reached with none in payload (defensive; every real path
        here goes through rule_advanced first, which always sets one)."""
        draft = ctx.payload.get("custom_filter_draft") or default_custom_profile()
        self._fsm.transition(
            user_id, FSMState.IDLE, selected_filter_preset="custom", custom_filter_draft=draft
        )
        await send_rendered(event, render_filter_config(selected_preset="custom"))

    async def _handle_watch_start(self, event: CallbackQuery, user_id: int, ctx: FSMContext) -> None:
        """Reads whichever preset `rule_preset:*` last stored for this
        user (Filter Config and Auto-Watch are separate screens per Part
        II.5's site map — this is what connects the two without a new
        FSM state), defaulting to Balanced (this module's own documented
        assumption) if the user starts a watch without ever visiting
        Filter Config first.

        `AutoWatchManager.on_match` is bound once, at construction, by
        `main.py`'s composition root — not re-supplied per call here.
        This handler only ever calls `start`/`stop`/`status` on an
        already-fully-wired manager, exactly the same relationship
        `ScanHandler` has with the already-constructed engines it calls
        `run_scan` against.

        Step 12 revalidation fix: `preset_key == "custom"` used to fall
        through to `NAMED_PRESETS.get("custom", NAMED_PRESETS["balanced"])`
        — "custom" is never a NAMED_PRESETS key, so every custom-configured
        watch silently ran as Balanced instead. Now reads the actual draft
        `rule_advanced`/`rule_tgl`/`rule_num`/`rule_save` built, with the
        same Balanced fallback only for the edge case of a "custom"
        selection with no draft ever built (e.g. picked from Filter
        Config's preset row without ever opening Advanced Rules)."""
        if await self._reject_if_scan_active(event, user_id):
            return
        profile = self._selected_filter_profile(ctx)

        new_ctx = self._fsm.transition(user_id, FSMState.AUTO_WATCH_ACTIVE)
        await self._auto_watch_manager.start(user_id, profile, interval_min=_DEFAULT_INTERVAL_MIN)
        status = self._auto_watch_manager.status(user_id)
        await send_rendered(event, render_auto_watch(new_ctx, status=status))

    async def _handle_watch_stop_request(
        self, event: CallbackQuery, user_id: int, ctx: FSMContext
    ) -> None:
        status = self._auto_watch_manager.status(user_id)
        if status is None:
            new_ctx = self._fsm.transition(user_id, FSMState.IDLE) if ctx.state is not FSMState.IDLE else ctx
            await send_rendered(event, render_auto_watch(new_ctx))
            return
        active_ctx = ctx
        if ctx.state is not FSMState.AUTO_WATCH_ACTIVE:
            active_ctx = self._fsm.transition(user_id, FSMState.AUTO_WATCH_ACTIVE)
        await send_rendered(event, render_auto_watch(active_ctx, status=status, confirm_stop=True))

    async def _handle_watch_stop_confirm(self, event: CallbackQuery, user_id: int) -> None:
        await self._auto_watch_manager.stop(user_id)
        current_state = self._fsm.get_state(user_id).state
        new_ctx = self._fsm.transition(user_id, FSMState.IDLE) if current_state is not FSMState.IDLE else self._fsm.get_state(user_id)
        await send_rendered(event, render_auto_watch(new_ctx, status=None))

    async def _handle_watch_stop_cancel(
        self, event: CallbackQuery, user_id: int, ctx: FSMContext
    ) -> None:
        status = self._auto_watch_manager.status(user_id)
        if status is None:
            new_ctx = self._fsm.transition(user_id, FSMState.IDLE) if ctx.state is not FSMState.IDLE else ctx
            await send_rendered(event, render_auto_watch(new_ctx))
            return
        active_ctx = ctx
        if ctx.state is not FSMState.AUTO_WATCH_ACTIVE:
            active_ctx = self._fsm.transition(user_id, FSMState.AUTO_WATCH_ACTIVE)
        await send_rendered(event, render_auto_watch(active_ctx, status=status))

    async def _handle_watch_view_results(self, event: CallbackQuery, user_id: int) -> None:
        status = self._auto_watch_manager.status(user_id)
        if status is None:
            await send_rendered(
                event,
                render_error(
                    "Auto-Watch is no longer active.",
                    technical_detail=None,
                    recovery_action=RECOVERY_HOME,
                ),
            )
            return

        results: list[ScoredResult] = []
        for result_id in status.matched_result_ids:
            cached = self._session_store.cache_get(result_id)
            if isinstance(cached, ScoredResult):
                results.append(cached)
            else:
                logger.warning(
                    "Auto-Watch match has no cached scan result",
                    extra={"user_id": user_id, "result_id": result_id},
                )

        if not results:
            current_ctx = self._fsm.get_state(user_id)
            if current_ctx.state is not FSMState.AUTO_WATCH_ACTIVE:
                current_ctx = self._fsm.transition(user_id, FSMState.AUTO_WATCH_ACTIVE)
            await send_rendered(event, render_auto_watch(current_ctx, status=status))
            return

        batch_id = None
        if results:
            batch_id = self._session_store.cache_put_batch(
                user_id,
                [result.result_id for result in results],
                {
                    "heading": "Auto-Watch Matches",
                    "source_note": "Matches found by Auto-Watch this session.",
                    "scope_note": "This session only; clears when the bot restarts.",
                    "back_callback": "nav_watch",
                },
            )
        self._fsm.transition(
            user_id,
            FSMState.RESULT_READY,
            result_list_batch_id=batch_id,
            result_list_page=0,
        )
        await send_rendered(
            event,
            render_result_list(
                results,
                batch_id=batch_id,
                heading="Auto-Watch Matches",
                source_note="Matches found by Auto-Watch this session.",
                scope_note="This session only; clears when the bot restarts.",
                back_callback="nav_watch",
            ),
        )

    def _selected_filter_profile(self, ctx: FSMContext) -> FilterProfile:
        preset_key = ctx.payload.get("selected_filter_preset", "balanced")
        if preset_key == "custom":
            draft = ctx.payload.get("custom_filter_draft")
            return draft if isinstance(draft, FilterProfile) else NAMED_PRESETS["balanced"]
        return NAMED_PRESETS.get(preset_key, NAMED_PRESETS["balanced"])

    async def _handle_discovery_scan(
        self, event: CallbackQuery, user_id: int, source: str, ctx: FSMContext
    ) -> None:
        """On-demand version of what Auto-Watch does on a timer: fetch
        candidates from the discovery feed named by this specific button
        (Trending -> boosted, New -> new_listings — see
        `TokenDiscoveryProvider`'s own docstring for what each actually
        means), filter through the user's selected Auto-Watch profile
        (Balanced until one is selected), and show only matches. The
        candidate count is bounded to `_MAX_DISCOVERY_SCAN_CANDIDATES`
        so one tap cannot trigger an unbounded number of scans."""
        if await self._reject_if_scan_active(event, user_id):
            return
        self._fsm.transition(user_id, FSMState.SCANNING)
        profile = self._selected_filter_profile(ctx)
        task = asyncio.create_task(self._run_discovery_scan(event, user_id, source, profile))
        self._session_store.set_scan_task(user_id, task)
        try:
            await task
        except asyncio.CancelledError:
            self._fsm.transition(user_id, FSMState.IDLE)
        finally:
            if self._session_store.get_scan_task(user_id) is task:
                self._session_store.pop_scan_task(user_id)

    async def _run_discovery_scan(
        self, event: CallbackQuery, user_id: int, source: str, profile: FilterProfile
    ) -> None:
        progress_state: dict[str, EngineStatus] = {
            "core": EngineStatus.RUNNING,
            "security": EngineStatus.PENDING,
            "holder": EngineStatus.PENDING,
            "social": EngineStatus.PENDING,
            "momentum": EngineStatus.PENDING,
        }
        await send_rendered(event, render_scanning_progress(progress_state))
        try:
            if source == "scan_trending":
                candidates = await self._discovery_provider.get_trending(limit=_MAX_DISCOVERY_SCAN_CANDIDATES)
            else:
                candidates = await self._discovery_provider.get_new_listings(limit=_MAX_DISCOVERY_SCAN_CANDIDATES)
        except Exception as exc:
            logger.exception("Discovery feed fetch failed", extra={"user_id": user_id, "source": source})
            self._fsm.transition(user_id, FSMState.IDLE)
            show_technical = self._session_store.get_settings(user_id).show_technical_errors
            await send_rendered(
                event,
                render_error(
                    _DISCOVERY_FETCH_FAILED_ERROR,
                    technical_detail=f"{type(exc).__name__}: {exc}",
                    show_technical=show_technical,
                    recovery_action=("\U0001f504 Try Again", "nav_scan"),
                ),
            )
            return

        unique_candidates = []
        seen_addresses: set[tuple[Chain, str]] = set()
        for candidate in candidates:
            address = candidate.token_address.strip()
            normalized_address = address.lower() if candidate.chain in EVM_CHAINS else address
            dedupe_key = (candidate.chain, normalized_address)
            if dedupe_key in seen_addresses:
                continue
            seen_addresses.add(dedupe_key)
            unique_candidates.append(candidate)

        counts = {
            "discovered": len(unique_candidates),
            "analyzed": 0,
            "rejected": 0,
            "displayed": 0,
            "failed": 0,
        }
        results = []
        for candidate in unique_candidates:
            try:
                scored = await run_scan(
                    candidate.token_address,
                    self._core_engine, self._security_engine, self._holder_engine,
                    self._momentum_engine, self._social_engine, self._scoring_pipeline, self._session_store,
                    chain_hint=candidate.chain,
                    user_id=user_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                counts["failed"] += 1
                logger.warning(
                    "Discovery scan failed for one candidate, skipping it",
                    extra={"user_id": user_id, "address": candidate.token_address, "error": str(exc)},
                )
                continue
            counts["analyzed"] += 1
            if matches(profile, scored):
                results.append(scored)
            else:
                counts["rejected"] += 1

        counts["displayed"] = len(results)
        source_note = (
            "Trending includes paid boosts; a boost is paid promotion, not evidence of organic interest."
            if source == "scan_trending"
            else "New Listings reflects recently updated token profiles, not necessarily newly created pools."
        )
        batch_meta = {**counts, "source_note": source_note}
        batch_id = self._session_store.cache_put_batch(
            user_id,
            [result.result_id for result in results],
            batch_meta,
        )
        self._fsm.transition(
            user_id,
            FSMState.RESULT_READY,
            result_list_batch_id=batch_id,
            result_list_page=0,
        )
        await reply(event, "<b>Discovery scan complete.</b> Results are shown in the next message.")
        await send_new_rendered(
            event,
            render_result_list(results, summary=counts, source_note=source_note, batch_id=batch_id),
        )

    async def _reject_if_scan_active(self, event: CallbackQuery, user_id: int) -> bool:
        task = self._session_store.get_scan_task(user_id)
        if task is not None and not task.done():
            await send_rendered(event, render_plain(_SCAN_ALREADY_RUNNING_HTML, back_callback="nav_scan"))
            return True
        if task is not None:
            self._session_store.pop_scan_task(user_id)
        return False
