"""
Layer: Handlers — scan flow (Playbook Part VIII Step 7; Holder Engine
wired in during the Step 8 integration pass; Momentum Engine wired in
during the Step 9 integration pass; Social Engine wired in during
Step 14).

Wires `AwaitingAddress` -> `Scanning` -> `ResultReady` -> `ResultDetail`
through the real engines. The actual engine-orchestration logic lives in
`scan_orchestration.py` (pure, tested separately); this file is the
Telegram-I/O shell around it — sending/editing messages, reading
callback params, transitioning FSM state, and tracking per-engine
progress across Core, the concurrently-run Security/Holder/Social trio,
and Momentum (which runs immediately after that trio, not concurrently
with it — see `scan_orchestration.run_scan`'s docstring for why).
"""

from __future__ import annotations

import asyncio
import logging

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from analysis.core_engine import CoreEngine
from analysis.holder_engine import HolderEngine
from analysis.momentum_engine import MomentumEngine
from analysis.security_engine import SecurityEngine
from analysis.social_engine import SocialEngine
from bot.constants import Chain, FSMState
from bot.types import EngineStatus
from handlers.base import TelegramEvent, reply, send_new_rendered, send_rendered
from handlers.callback_parser import parse_address, parse_callback
from handlers.scan_orchestration import ScoredResult, run_scan
from rendering.error_renderer import RECOVERY_HOME, render_error
from rendering.menus import render_plain, render_scanning_progress
from rendering.result_renderer import render_result_detail, render_result_list
from scoring.pipeline import ScoringPipeline
from state.fsm import FSMContext, FSMEngine
from state.session_store import SessionStore

logger = logging.getLogger(__name__)

_PASTE_PROMPT_HTML = (
    "\u2328\ufe0f <b>Paste a contract address</b>\n\n"
    "Send the token's contract address as a message - Solana, Ethereum, "
    "BNB Chain, Base, Arbitrum, or TON."
)
_INVALID_ADDRESS_MESSAGE = (
    "That doesn't look like a valid contract address. Double-check it and "
    "send it again, or tap Home."
)
_SCAN_ALREADY_RUNNING_HTML = "<b>A scan is already running.</b> Please wait for it to finish or cancel it first."
_SCAN_CANCELLED_HTML = "\u274c <b>Scan cancelled.</b>"

# Callback commands this handler owns. Kept as a set (not a dict of
# renderers, unlike MenuNavigationHandler's _NAV_TARGETS) since each one
# needs a materially different handler method, not just a different
# render_* call.
_OWNED_CALLBACK_COMMANDS = frozenset(
    {"scan_paste", "result_view", "result_back_list", "scan_rescan", "result_page", "scan_cancel"}
)


class ScanHandler:
    def __init__(
        self,
        fsm: FSMEngine,
        core_engine: CoreEngine,
        security_engine: SecurityEngine,
        holder_engine: HolderEngine,
        momentum_engine: MomentumEngine,
        social_engine: SocialEngine,
        scoring_pipeline: ScoringPipeline,
        session_store: SessionStore,
    ) -> None:
        self._fsm = fsm
        self._core_engine = core_engine
        self._security_engine = security_engine
        self._holder_engine = holder_engine
        self._momentum_engine = momentum_engine
        self._social_engine = social_engine
        self._scoring_pipeline = scoring_pipeline
        self._session_store = session_store

    async def can_handle(self, event: TelegramEvent, ctx: FSMContext) -> bool:
        if isinstance(event, CallbackQuery):
            parsed = parse_callback(event.data or "")
            return parsed.command in _OWNED_CALLBACK_COMMANDS
        if isinstance(event, Message):
            if ctx.state is FSMState.AWAITING_ADDRESS:
                return True
            if ctx.state is FSMState.IDLE and parse_address((event.text or "").strip()).is_valid_shape:
                return True
            if event.from_user is not None:
                task = self._session_store.get_scan_task(event.from_user.id)
                return task is not None and not task.done()
        return False

    async def handle(self, event: TelegramEvent, ctx: FSMContext) -> None:
        if isinstance(event, CallbackQuery):
            await self._handle_callback(event, ctx)
            return
        assert isinstance(event, Message)
        await self._handle_pasted_address(event, ctx)

    # -- callback branches ---------------------------------------------

    async def _handle_callback(self, event: CallbackQuery, ctx: FSMContext) -> None:
        assert event.from_user is not None
        user_id = event.from_user.id
        parsed = parse_callback(event.data or "")

        if parsed.command == "scan_paste":
            if await self._reject_if_scan_active(event, user_id):
                return
            self._fsm.transition(user_id, FSMState.AWAITING_ADDRESS)
            await send_rendered(event, render_plain(_PASTE_PROMPT_HTML, back_callback="nav_scan"))
            return

        if parsed.command == "result_view":
            await self._show_result_detail(event, user_id, parsed.params)
            return

        if parsed.command == "result_back_list":
            await self._show_result_back_list(event, user_id, parsed.params)
            return

        if parsed.command == "result_page":
            await self._show_result_page(event, user_id, parsed.params)
            return

        if parsed.command == "scan_rescan":
            await self._rescan(event, user_id, parsed.params)
            return

        if parsed.command == "scan_cancel":
            task = self._session_store.get_scan_task(user_id)
            source = self._session_store.get_scan_task_source(user_id)
            if task is not None and not task.done() and source == "auto_watch":
                await send_rendered(
                    event,
                    render_plain(
                        "This scan is part of Auto-Watch and was not cancelled.",
                        back_callback="nav_scan",
                    ),
                )
                return
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Scan task failed while being cancelled", extra={"user_id": user_id})
            task = self._session_store.get_scan_task(user_id)
            if task is not None and task.done():
                self._session_store.pop_scan_task(user_id)
            self._fsm.transition(user_id, FSMState.IDLE)
            await send_rendered(event, render_plain(_SCAN_CANCELLED_HTML, back_callback="nav_scan"))
            return

    async def _show_result_detail(self, event: CallbackQuery, user_id: int, params: list[str]) -> None:
        scored = self._lookup_result(params)
        if scored is None:
            await self._stale_result(event)
            return
        self._fsm.transition(user_id, FSMState.RESULT_DETAIL)
        back_callback = "nav_watch" if len(params) > 1 and params[1] == "watch" else None
        await send_new_rendered(event, render_result_detail(scored, back_callback=back_callback))

    async def _show_result_back_list(self, event: CallbackQuery, user_id: int, params: list[str]) -> None:
        """Return from a result detail to the list and page it came from."""
        scored = self._lookup_result(params)
        if scored is None:
            await self._stale_result(event)
            return
        ctx = self._fsm.get_state(user_id)
        batch_id = ctx.payload.get("result_list_batch_id")
        page = ctx.payload.get("result_list_page", 0)
        if isinstance(batch_id, str) and isinstance(page, int):
            await self._show_result_page(event, user_id, [batch_id, str(page)])
            return
        self._fsm.transition(
            user_id,
            FSMState.RESULT_READY,
            result_list_batch_id=None,
            result_list_page=0,
        )
        await send_rendered(event, render_result_list([scored]))

    async def _show_result_page(self, event: CallbackQuery, user_id: int, params: list[str]) -> None:
        """Render a page from a cached batch without rescanning candidates."""
        if len(params) != 2 or not params[0] or not params[1].isdigit():
            await self._stale_result(event)
            return

        batch_id, page_text = params
        batch = self._session_store.cache_get_batch(batch_id)
        if batch is None:
            await self._stale_result(event)
            return

        result_ids, meta = batch
        results: list[ScoredResult] = []
        for result_id in result_ids:
            cached = self._session_store.cache_get(result_id)
            if not isinstance(cached, ScoredResult):
                await self._stale_result(event)
                return
            results.append(cached)

        summary_keys = ("discovered", "analyzed", "rejected", "displayed", "failed")
        summary_values = {key: meta.get(key) for key in summary_keys}
        summary = (
            {key: value for key, value in summary_values.items() if isinstance(value, int)}
            if all(isinstance(value, int) for value in summary_values.values())
            else None
        )
        source_note = meta.get("source_note")
        if not isinstance(source_note, str):
            source_note = None
        heading = meta.get("heading")
        if not isinstance(heading, str):
            heading = "Results"
        empty_message = meta.get("empty_message")
        if not isinstance(empty_message, str):
            empty_message = None
        scope_note = meta.get("scope_note")
        if not isinstance(scope_note, str):
            scope_note = None
        back_callback = meta.get("back_callback")
        if not isinstance(back_callback, str):
            back_callback = "nav_scan"

        self._fsm.transition(
            user_id,
            FSMState.RESULT_READY,
            result_list_batch_id=batch_id,
            result_list_page=int(page_text),
        )
        await send_rendered(
            event,
            render_result_list(
                results,
                page=int(page_text),
                summary=summary,
                source_note=source_note,
                batch_id=batch_id,
                heading=heading,
                empty_message=empty_message,
                scope_note=scope_note,
                back_callback=back_callback,
            ),
        )

    async def _rescan(self, event: CallbackQuery, user_id: int, params: list[str]) -> None:
        previous = self._lookup_result(params)
        if previous is None:
            await self._stale_result(event)
            return
        await self._run_full_scan(event, user_id, previous.core.address, chain_hint=previous.core.chain)

    def _lookup_result(self, params: list[str]) -> ScoredResult | None:
        if not params:
            return None
        cached = self._session_store.cache_get(params[0])
        return cached if isinstance(cached, ScoredResult) else None

    async def _stale_result(self, event: CallbackQuery) -> None:
        """A cached result can be missing if the process restarted (Part
        I.3: in-memory only, by design) or the key was never valid -
        Part II.5's stale-session recovery, applied to a specific result
        rather than the whole session. STEP 15: now routes through the
        unified error renderer (was a bare `render_plain` string before
        this pass) - same Home recovery action, now with a consistent
        "\u26a0\ufe0f" framing and a technical-detail slot for consistency
        with every other error screen in this codebase."""
        show_technical = False
        if event.from_user is not None:
            show_technical = self._session_store.get_settings(event.from_user.id).show_technical_errors
        await send_rendered(
            event,
            render_error(
                "That result has expired.",
                technical_detail="Cached ScoredResult or result batch not found (process restart, or an unknown key).",
                show_technical=show_technical,
                recovery_action=RECOVERY_HOME,
            ),
        )

    # -- message branch (the pasted address itself) ---------------------

    async def _handle_pasted_address(self, event: Message, ctx: FSMContext) -> None:
        assert event.from_user is not None
        user_id = event.from_user.id
        if await self._reject_if_scan_active(event, user_id):
            return
        parsed_address = parse_address((event.text or "").strip())

        if not parsed_address.is_valid_shape:
            # STEP 15 FIX: previously `event.answer(_INVALID_ADDRESS_MESSAGE)`
            # with no `reply_markup` at all - the text said "or tap Home"
            # but there was no button, a real Part II.5 dead-end caught
            # while building this step's unified error handling. Stays a
            # plain reply (not a full render_error screen replacement) on
            # purpose: the user is meant to stay in AwaitingAddress and
            # paste again immediately, which a full screen swap would
            # actually make worse, not better - but the promised Home
            # button now really exists, using the same RECOVERY_HOME
            # label/callback every other error screen in this codebase
            # uses, via a real keyboard rather than only text.
            home_label, home_callback = RECOVERY_HOME
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=home_label, callback_data=home_callback)]]
            )
            await event.answer(_INVALID_ADDRESS_MESSAGE, reply_markup=keyboard)
            return  # stay in AwaitingAddress - let them try again

        await self._run_full_scan(event, user_id, parsed_address.raw)

    # -- shared scan runner ----------------------------------------------

    async def _run_full_scan(
        self, event: TelegramEvent, user_id: int, address: str, chain_hint: Chain | None = None
    ) -> None:
        """
        Shared by both entry points (a freshly pasted address, and
        Result Detail's Rescan button) — both just need "run the scan
        against this address and show the result," differing only in how
        they got the address.
        """
        if await self._reject_if_scan_active(event, user_id):
            return
        self._fsm.transition(user_id, FSMState.SCANNING)

        # Mutable progress snapshot, updated by whichever hook fires and
        # re-rendered in full each time. Security and Holder run
        # concurrently via `asyncio.gather` (scan_orchestration.py) -
        # either can finish first, so the screen has to reflect whichever
        # combination of engine states is actually true at the moment a
        # hook fires, not a single hardcoded snapshot. Momentum starts
        # out PENDING and stays there through the concurrent phase - it
        # genuinely can't start until Holder resolves (it takes
        # HolderResult as a required argument, not an optional one; see
        # scan_orchestration.run_scan's docstring) - then jumps straight
        # to DONE once computed, since it's a synchronous, instant
        # computation with no real "in progress" moment to show.
        progress_state: dict[str, EngineStatus] = {
            "core": EngineStatus.RUNNING,
            "security": EngineStatus.PENDING,
            "holder": EngineStatus.PENDING,
            "social": EngineStatus.PENDING,
            "momentum": EngineStatus.PENDING,
        }

        initial = render_scanning_progress(dict(progress_state))
        if isinstance(event, CallbackQuery):
            await send_rendered(event, initial)
            progress_message = event.message
        else:
            progress_message = await event.answer(initial.html, parse_mode="HTML", reply_markup=initial.keyboard)

        async def _update_progress() -> None:
            if progress_message is not None:
                rendered = render_scanning_progress(dict(progress_state))
                await progress_message.edit_text(rendered.html, parse_mode="HTML", reply_markup=rendered.keyboard)

        async def on_core_complete() -> None:
            progress_state["core"] = EngineStatus.DONE
            progress_state["security"] = EngineStatus.RUNNING
            progress_state["holder"] = EngineStatus.RUNNING
            progress_state["social"] = EngineStatus.RUNNING
            await _update_progress()

        async def on_security_complete() -> None:
            progress_state["security"] = EngineStatus.DONE
            await _update_progress()

        async def on_holder_complete() -> None:
            progress_state["holder"] = EngineStatus.DONE
            await _update_progress()

        async def on_social_complete() -> None:
            progress_state["social"] = EngineStatus.DONE
            await _update_progress()

        async def on_momentum_complete() -> None:
            progress_state["momentum"] = EngineStatus.DONE
            await _update_progress()

        scan_task = asyncio.create_task(
            run_scan(
                address,
                self._core_engine,
                self._security_engine,
                self._holder_engine,
                self._momentum_engine,
                self._social_engine,
                self._scoring_pipeline,
                self._session_store,
                chain_hint=chain_hint,
                on_core_complete=on_core_complete,
                on_security_complete=on_security_complete,
                on_holder_complete=on_holder_complete,
                on_social_complete=on_social_complete,
                on_momentum_complete=on_momentum_complete,
                user_id=user_id,
            )
        )
        self._session_store.set_scan_task(user_id, scan_task)
        try:
            scored = await scan_task
        except asyncio.CancelledError:
            self._fsm.transition(user_id, FSMState.IDLE)
            return
        except Exception:
            logger.exception("Unexpected failure during scan", extra={"address": address, "user_id": user_id})
            self._fsm.transition(user_id, FSMState.IDLE)
            if progress_message is not None:
                await progress_message.edit_text(
                    "Something went wrong running that scan. Send /start for the Main Menu.",
                    parse_mode="HTML",
                )
            return
        finally:
            if self._session_store.get_scan_task(user_id) is scan_task:
                self._session_store.pop_scan_task(user_id)

        self._fsm.transition(
            user_id,
            FSMState.RESULT_READY,
            result_list_batch_id=None,
            result_list_page=0,
        )
        final = render_result_list([scored])
        if progress_message is not None:
            await progress_message.edit_text(final.html, parse_mode="HTML", reply_markup=final.keyboard)
        else:
            await reply(event, final.html, keyboard=final.keyboard)

    async def _reject_if_scan_active(self, event: TelegramEvent, user_id: int) -> bool:
        task = self._session_store.get_scan_task(user_id)
        if task is not None and not task.done():
            await send_rendered(event, render_plain(_SCAN_ALREADY_RUNNING_HTML, back_callback="nav_scan"))
            return True
        if task is not None:
            self._session_store.pop_scan_task(user_id)
        return False
