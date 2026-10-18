# SPDX-License-Identifier: GPL-3.0-only
# The tasks on one asyncio loop (architecture.md, "Tasks"): control (app), net, http, watchdog, housekeeping.
# run() is the top level on the Pico: an uncaught exception, at boot or in any task, goes to crash.txt and resets.
import asyncio
import gc
import io
import sys

from smartac.board.base import CRASH
from smartac.core.types import EV_HEAP, EV_LOW_MEMORY, ticks_since
from smartac.shell import api
from smartac.shell.net import NetSupervisor

WDT_MS = 8000
WATCHDOG_PERIOD_MS = 1000
NET_STALE_MS = 30 * 1000
HOUSEKEEPING_MS = 60 * 1000
HEAP_LOG_EVERY = 60  # housekeeping runs: one heap line an hour in events.log, a soak record without a Mac
LOW_FREE = 64 * 1024
LOW_BLOCK = 24 * 1024  # a 16 KB AC reply plus its parse must still fit


async def watchdog(board, app, net):
    # Feed the hardware watchdog only while control and net both beat, so a stalled task resets the board.
    board.wdt_start(WDT_MS)
    while True:
        now = board.ticks_ms()
        if ticks_since(now, app.beat) < 2 * app.poll_ms() and ticks_since(now, net.beat) < NET_STALE_MS:
            board.wdt_feed()
        await board.sleep_ms(WATCHDOG_PERIOD_MS)


async def housekeeping(board, app):
    # Heap stats for /api/state (the soak watches them), a low_memory event when a threshold is crossed, and an
    # hourly heap line in events.log, so a long soak's trend survives on the Pico.
    st = app.stats
    st["low_memory"] = 0
    low = False
    runs = 0
    while True:
        gc.collect()
        free, block = board.heap()
        st["heap_free"] = free
        st["heap_max_block"] = block
        st["heap_min_free"] = min(st.get("heap_min_free", free), free)
        if block is not None:
            st["heap_min_block"] = min(st.get("heap_min_block", block), block)
        now_low = free < LOW_FREE or (block is not None and block < LOW_BLOCK)
        if now_low and not low:
            st["low_memory"] += 1
            app.event(EV_LOW_MEMORY, "free %d, largest block %s" % (free, block))
        low = now_low
        runs += 1
        if runs % HEAP_LOG_EVERY == 0:
            app.log(EV_HEAP, "free %d block %s" % (free, block))
        app.events.prune(board.ticks_ms())
        await board.sleep_ms(HOUSEKEEPING_MS)


async def _main(board, app, port, www, token):
    net = NetSupervisor(board, app)
    web = api.make(app, www, token=token)
    try:
        await asyncio.gather(
            app.control(), net.run(), web.start_server(port=port), watchdog(board, app, net), housekeeping(board, app)
        )
    finally:
        if web.server is not None:
            web.shutdown()
            await asyncio.sleep_ms(0)  # let the server task close its socket


def crash_text(e):
    gc.collect()
    try:
        buf = io.StringIO()
        sys.print_exception(e, buf)
        return buf.getvalue()
    except Exception:  # e.g. no memory for the traceback
        return "%s: %s\n" % (type(e).__name__, e)


def run(board, store, make_app, port=80, www="/www", token=None):
    # make_app() -> App, built here so a failure at boot is a crash like any other. token: API_TOKEN for POSTs.
    try:
        asyncio.run(_main(board, make_app(), port, www, token))
    except Exception as e:
        text = crash_text(e)
        print(text)
        try:
            store.save_crash(text)
        except Exception:
            pass
        board.reset(CRASH)
