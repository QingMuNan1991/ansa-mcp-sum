# -*- coding: utf-8 -*-
"""ansa-mcp-sum — AUTO mode autoload for ANSA GUI.

Zero-click bridge: registers toolbar buttons (MCP-Sum / Run Once, Auto Poll,
Probe Caps, Stop) and a window-anchored repeating timer on ANSA's MAIN thread so
queued commands from the ansa-mcp-sum MCP server are executed inside the live
(GUI) ANSA session. This is the GUI-capable file-IPC transport that the
migrated tools use.

Loaded by ANSA via ANSA_TRANSL.py (GUI mode only; skipped under -b).

Environment:
  ANSA_MCP_SUM_HOME        bridge directory (default C:\\Users\\Admin\\.ansa-mcp-sum)
  ANSA_MCP_SUM_AUTO_POLL   "0" = load the buttons but do NOT start the timer;
                           you then drive ANSA with the MCP-Sum toolbar buttons.
                           Default "1" (poll from startup).

Console output policy (learned the hard way):
  * The timer fires every 200ms. Printing anything per tick floods ANSA's
    console — the original "[ansa-mcp-sum] heartbeat ticks=N" every 5s produced
    ~700 lines/hour of "nothing happened". Now: one line at startup, one line
    per *executed command*, and a heartbeat only every 15 minutes.
  * status.json is likewise throttled (see plugin.write_idle_status).

The bridge window is NOT a switch
---------------------------------
``_bridge_window`` exists only because ``guitk.BCTimerCreate(p)`` needs a parent
widget. There is no "window on/off" feature: ``Auto Poll`` and the window
appearing at startup run the *same* function (``_init_bridge``).

The window's title-bar Close (X) button is **hidden** (see FIX 1 in
``_ensure_window``). Closing it would destroy the window and its child timer,
leaving ``_poll_timer`` a dangling C++ handle that the next Auto Poll would
dereference and segfault on (the 2026-09-13 crash, EXCEPTION 0xC0000005). With
the Close button gone the window cannot be destroyed by the user, so the timer
lives for the whole ANSA session. The window can still be minimised or moved and
never blocks any other ANSA GUI work — you keep full manual control of ANSA.

Three ways the timer dies without saying so
-------------------------------------------
``_busy``           Re-entrancy guard. If one ``_poll_once`` never returns, every
                    later tick returns immediately, ``status.json`` freezes and
                    commands pile up — while ``Run Once`` still works, because it
                    calls ``plugin.ansa_mcp_process_one()`` directly and bypasses
                    the guard. This is the failure that looks like "every command
                    times out".
``_stop_requested`` Latched by the Stop button. ``_poll_once`` then returns before
                    writing anything, so the freeze is silent.
``_poll_timer``     Alive-but-not-firing (GUI thread busy in a modal dialog, or
                    the window minimised). Its health is now judged purely from
                    the Python-side heartbeat ``_last_tick_at`` (no native probe
                    of the handle), so a frozen timer is rebuilt instead of
                    short-circuited into a no-op.

``_init_bridge`` therefore watches ``_last_tick_at``: if the timer claims to be
active but has not ticked for ``STALL_SECONDS``, it rebuilds instead of
returning True. Every branch prints why.

Recovery when the GUI thread is wedged: ``scripts/bridge_doctor.py`` driven from
``MCP-Sum > Run Once``.

Editing this file does not change a running ANSA
------------------------------------------------
``ansa.ImportCode`` executes the file body **once**, at startup; the code then
lives in ANSA's own namespace for the life of the process. Nothing on disk is
re-read afterwards — not by ``Auto Poll``, not by ``importlib.reload(plugin)``
(that reloads ``ansa_mcp_sum.plugin``, a different file). So a fix written here
silently does nothing until either ANSA is restarted or the file is re-executed.

``bridge_doctor.py`` does the latter: it locates this namespace, then
``exec``-s the current file into it with ``ansa.session.defbutton`` temporarily
swallowed so the four toolbar buttons are not registered a second time. The
buttons keep working across that reload because their functions resolve globals
(``_init_bridge`` and friends) out of the very namespace being refreshed.
"""
import os
import sys
import time

os.environ.setdefault("ANSA_MCP_SUM_HOME", r"C:\Users\Admin\.ansa-mcp-sum")

PROJECT_SRC = r"D:\ansa-mcp-sum\src"
if PROJECT_SRC not in sys.path:
    sys.path.insert(0, PROJECT_SRC)

import ansa  # noqa: E402
from ansa import base, guitk  # noqa: E402

try:
    from ansa_mcp_sum import plugin
except Exception as exc:  # surface a clear error in ANSA
    print("[ansa-mcp-sum] FAILED to import ansa_mcp_sum.plugin: %s" % exc)
    raise

AUTO_START_POLL = os.environ.get("ANSA_MCP_SUM_AUTO_POLL", "1").strip() != "0"
POLL_MS = 200
#: 200ms * 4500 = 15 minutes. Silence is the point; this is just proof of life.
HEARTBEAT_EVERY_TICKS = 4500
MODEL_GRACE_SECONDS = 30.0
#: A healthy timer ticks every 200ms, so 10s of silence means it is not firing.
#: Deliberately loose: a long command holds ``_busy`` and freezes ticks too, and
#: we must not call that a stalled timer.
STALL_SECONDS = 10.0
#: ``_busy`` frozen for this long stops being "slow command" and starts being
#: "wedged". A full quality check on 66k shells takes ~20 min, so 30 min is the
#: first point at which clearing the guard is safer than leaving the bridge dead.
BUSY_LIMIT_SECONDS = 1800.0

_busy = False
_busy_since = 0.0
_busy_reported = False
_tick_count = 0
_last_tick_at = time.time()
_poll_timer = None
#: NOT None by default. ``bridge_doctor`` re-executes this file into its own live
#: namespace (``exec(code, g)``), so a plain module global reassigned here would
#: be wiped to None on every reload and ``_init_bridge`` would then build a fresh
#: ``ANSA_MCP_SUM_Bridge`` window on top of the old one (there is no
#: ``guitk.BCDestroyWindow`` in 25.x, so the old one is orphaned, not freed). That
#: is exactly how a looping doctor command stacked dozens of windows. The handle
#: therefore lives in ``plugin.GUI_HANDLES`` - a module that is NEVER re-executed -
#: and is re-read here so it survives a reload.
_bridge_window = plugin.GUI_HANDLES.get("bridge_window")
_stop_requested = False
_start_time = time.time()
_error_streak = 0


def _db_is_empty():
    try:
        deck = base.CurrentDeck()
        faces = base.CollectEntities(deck, None, "FACE", False)
        solids = base.CollectEntities(deck, None, "SOLID", False)
        return (not faces) and (not solids)
    except Exception:
        return False


def _poll_once():
    global _busy, _busy_since, _busy_reported, _tick_count, _error_streak
    global _last_tick_at

    if _stop_requested:
        # Stop is a decision, not a fault — no status write, no stall report.
        return False

    if _busy:
        # A previous tick has not returned. Do NOT clear the guard: the
        # outstanding command is usually just slow, and clearing it would let
        # two commands run through the handler at once. But say it once on
        # disk, otherwise "bridge looks running, consumes nothing" is only
        # diagnosable by guessing. _last_tick_at stays frozen, which is exactly
        # what lets Auto Poll notice the stall and rebuild.
        if not _busy_reported and (time.time() - _last_tick_at) > STALL_SECONDS:
            _busy_reported = True
            plugin.write_status(
                "running",
                "poll callback re-entered while a command is still in flight "
                "(idle %.0fs) — Auto Poll cannot help until it returns"
                % (time.time() - _last_tick_at))
        return False

    _busy = True
    _busy_since = time.time()
    #: Only a tick that actually did work refreshes this. That is the point: a
    #: frozen _last_tick_at is the one signal that separates "timer alive but
    #: not firing" from "timer fine".
    _last_tick_at = _busy_since
    try:
        _tick_count += 1
        pending = []
        try:
            pending = list(plugin.CONFIG.commands_dir.glob("cmd_*.json"))
            # ANSA loads its own database slightly after this script runs. A
            # command that arrives in that window would run against an empty
            # model and silently "succeed" while doing nothing.
            if (pending and (time.time() - _start_time) < MODEL_GRACE_SECONDS
                    and _db_is_empty()):
                plugin.write_idle_status(
                    "model not loaded yet; deferring %d cmd(s)" % len(pending))
                return False
        except Exception:
            pass

        processed = plugin.ansa_mcp_process_one()
        if processed:
            # One line per real command — this is the log a human actually wants.
            print("[ansa-mcp-sum] executed %s (queue had %d, ticks=%d)"
                  % (plugin.BRIDGE.get("last_command_type") or "command",
                     len(pending), _tick_count))
        elif _tick_count % HEARTBEAT_EVERY_TICKS == 0:
            print("[ansa-mcp-sum] idle heartbeat ticks=%d (~%.0f min up)"
                  % (_tick_count, (time.time() - _start_time) / 60.0))
        _error_streak = 0
        return bool(processed)
    except Exception as exc:
        _error_streak += 1
        # Never spam: report the first failure, then every 50th.
        if _error_streak == 1 or _error_streak % 50 == 0:
            print("[ansa-mcp-sum] poll error x%d: %s" % (_error_streak, exc))
        return False
    finally:
        _busy = False
        _busy_reported = False


def _timer_cb(timer, data):
    _poll_once()
    return 0


def _noop_accept(window, data):
    return 0


BRIDGE_WINDOW_NAME = "ANSA_MCP_SUM_Bridge"


def _ensure_window(force_new=False):
    """Return a parent widget for the poll timer, creating it at most once.

    ``BCTimerCreate`` needs a parent; that parent is the *only* reason
    ``ANSA_MCP_SUM_Bridge`` exists. Reusing it keeps recovery cheap and avoids
    the duplicate-name problem documented on ``BCWindowCreate``.

    The live handle is also mirrored into ``plugin.GUI_HANDLES`` so a hot reload
    (``bridge_doctor`` re-executing this file) preserves the SAME window instead
    of stacking a second one under the same xml tag.

    FIX 1 (ansa-mcp-sum 1.0.7): the window's title-bar Close (X) button is
    hidden. Destroying the window would also free its child timer (a C++ object),
    leaving ``_poll_timer`` a dangling handle that the next Auto Poll would
    dereference and segfault on (the 2026-09-13 crash). Hiding Close removes that
    whole failure mode; the window can still be minimised / moved and never
    blocks any other ANSA GUI interaction. If the hide call is unavailable on a
    build, we degrade gracefully (window stays closeable) rather than failing.
    """
    global _bridge_window
    if force_new or _bridge_window is None:
        _bridge_window = guitk.BCWindowCreate(BRIDGE_WINDOW_NAME,
                                              guitk.constants.BCOnExitHide)
        guitk.BCLabelCreate(
            _bridge_window,
            "ANSA MCP Sum - background poll timer (200 ms). This window only "
            "hosts the timer; it cannot be closed so the poll never dies. "
            "If polling stops, run scripts/bridge_doctor.py.")
        guitk.BCSpacerCreate(_bridge_window)
        guitk.BCWindowSetAcceptFunction(_bridge_window, _noop_accept, None)
        # FIX 1: hide the Close (X) title-bar button only. Keep Min/Max so the
        # window can be parked out of the way. We pass the runtime bitmask
        # constants (not hardcoded ints) so the value resolves from this build.
        try:
            guitk.BCWindowShowTitleBarButtons(
                _bridge_window,
                guitk.constants.BCMinimizeButton | guitk.constants.BCMaximizeButton,
            )
        except Exception as exc:
            print("[ansa-mcp-sum] NOTE: could not hide bridge-window Close button "
                  "(%s) - window remains closeable." % exc)
        plugin.GUI_HANDLES["bridge_window"] = _bridge_window
    return _bridge_window


def _reset_bridge_refs():
    """Drop the timer/window references WITHOUT dereferencing them.

    If the window was ever destroyed, both ``_poll_timer`` and ``_bridge_window``
    point at freed C++ objects. Calling any guitk function on them
    (``BCTimerStop``, ``BCTimerIsActive``, even ``BCTimerCreate(parent=dead)``)
    is a native call that segfaults uncatchably - that is the 2026-09-13 crash.
    So we only null the Python references here and let the next ``_ensure_window``
    create brand-new, valid objects. Fix 1 keeps the window un-closeable, so this
    path is the defensive backstop rather than the common case.
    """
    global _poll_timer, _bridge_window
    _poll_timer = None
    _bridge_window = None
    try:
        plugin.GUI_HANDLES.pop("bridge_window", None)
    except Exception:
        pass


def _init_bridge(force=False):
    """Make sure a live 200ms poll timer exists. Idempotent unless it stalled.

    FIX 2 (ansa-mcp-sum 1.0.7): timer health is decided purely from the
    Python-side heartbeat ``_last_tick_at`` - we NEVER call a native guitk
    function on the retained timer handle just to check it. The old code opened
    with ``guitk.BCTimerIsActive(_poll_timer)``; on a dangling handle that
    segfaulted. Now a stale timer is rebuilt by dropping the old reference and
    creating a fresh timer on the (Fix-1-guaranteed-alive) window.
    """
    global _poll_timer, _bridge_window, _last_tick_at
    global _busy, _busy_reported

    if _poll_timer is not None and not force:
        stalled = time.time() - _last_tick_at
        if _busy and stalled < BUSY_LIMIT_SECONDS:
            print("[ansa-mcp-sum] a command is in flight (%.0fs); timer left "
                  "alone - rebuilding would not help."
                  % (time.time() - _busy_since))
            return True
        if stalled < STALL_SECONDS:
            print("[ansa-mcp-sum] auto-poll already running (last tick %.1fs "
                  "ago) - nothing to do." % stalled)
            return True
        # Stale: drop the old timer reference WITHOUT dereferencing it. The
        # window is still alive thanks to Fix 1, so BCTimerCreate below is safe.
        print("[ansa-mcp-sum] timer has not ticked for %.0fs -> rebuilding timer "
              "on existing window (no native probe of stale handle)."
              % stalled)
        _poll_timer = None
        if _busy:
            # Only reachable past BUSY_LIMIT_SECONDS: the guard has been latched
            # that long, so every future tick would be a no-op. Clearing it can
            # let a second command run concurrently, which is still better than
            # a permanently dead bridge.
            print("[ansa-mcp-sum] WARNING: clearing _busy after %.0fs - the "
                  "previous command never returned." % stalled)
            _busy = False
            _busy_reported = False

    # (Re)create the timer on the (Fix-1-alive) bridge window.
    last_error = None
    for attempt in (1, 2):
        try:
            window = _ensure_window(force_new=False)
            _poll_timer = guitk.BCTimerCreate(window)
            guitk.BCTimerSetTimeoutFunction(_poll_timer, _timer_cb, None)
            guitk.BCTimerStart(_poll_timer, POLL_MS, False)
            guitk.BCShow(window)
            _last_tick_at = time.time()
            print("[ansa-mcp-sum] auto-poll timer started (main thread, %dms, "
                  "window-parented%s)."
                  % (POLL_MS, ", window recreated" if attempt == 2 else ""))
            return True
        except Exception as exc:
            last_error = exc
            _poll_timer = None
            if attempt == 1:
                # The only plausible reason BCTimerCreate failed here is that the
                # existing window handle is no longer usable. Discard it and try
                # once with a freshly created window (rare under Fix 1).
                print("[ansa-mcp-sum] could not attach a timer to the existing "
                      "bridge window (%s) - recreating it." % exc)
                _reset_bridge_refs()
                continue
            break
    # The window reference is intentionally NOT cleared on failure: it is the
    # only handle we have, and dropping it would make the next rebuild create a
    # second window under the same name.
    print("[ansa-mcp-sum] FAILED to start auto-poll timer: %s" % last_error)
    return False


@ansa.session.defbutton('MCP-Sum', 'Run Once', '主线程手动处理队列里的所有命令（轮询停摆时的应急通道）')
def mcp_sum_run_once():
    plugin.write_status("running", "MCP-Sum Run Once triggered")
    n = 0
    while plugin.ansa_mcp_process_one():
        n += 1
    plugin.write_status("running", "idle (processed %d)" % n)
    print("[ansa-mcp-sum] Run Once processed %d command(s)." % n)
    # Run Once calls plugin.ansa_mcp_process_one() directly, so it keeps working
    # while the timer is dead — which is exactly why a dead timer can go
    # unnoticed. Say so instead of letting the user believe polling is healthy.
    stalled = time.time() - _last_tick_at
    if _stop_requested:
        print("[ansa-mcp-sum] note: polling is stopped (_stop_requested). "
              "Click Auto Poll to resume.")
    elif stalled > STALL_SECONDS:
        print("[ansa-mcp-sum] note: the 200ms timer has not ticked for %.0fs. "
              "Click Auto Poll to rebuild it." % stalled)


@ansa.session.defbutton('MCP-Sum', 'Auto Poll', '启动自动轮询；若定时器停摆会自动重建')
def mcp_sum_auto_poll():
    global _stop_requested
    _stop_requested = False
    if not _init_bridge():
        print("[ansa-mcp-sum] auto-poll FAILED to start. If it keeps failing, "
              "run scripts/bridge_doctor.py through Run Once for a full report.")


@ansa.session.defbutton('MCP-Sum', 'Probe Caps', '采集本机 ANSA 能力清单，供 MCP 侧裁剪工具表')
def mcp_sum_probe_caps():
    """Run probe_ansa_capabilities.py in this session and report where it landed.

    This is the one-click equivalent of
        ansa.ImportCode(r"D:\\ansa-mcp-sum\\scripts\\probe_ansa_capabilities.py")
    """
    path = r"D:\ansa-mcp-sum\scripts\probe_ansa_capabilities.py"
    try:
        if not os.path.isfile(path):
            print("[ansa-mcp-sum] probe script not found: %s" % path)
            return
        ansa.ImportCode(path)
        out = os.path.join(plugin.CONFIG.home, "capabilities", "capabilities.json")
        print("[ansa-mcp-sum] capability probe written: %s" % out)
    except Exception as exc:
        print("[ansa-mcp-sum] capability probe failed: %s: %s"
              % (type(exc).__name__, exc))


@ansa.session.defbutton('MCP-Sum', 'Stop', '停止轮询 / 写停止标志')
def mcp_sum_stop():
    global _stop_requested
    _stop_requested = True
    try:
        if _poll_timer is not None:
            guitk.BCTimerStop(_poll_timer)
    except Exception:
        pass
    try:
        plugin.ansa_mcp_stop()
    except Exception:
        pass
    print("[ansa-mcp-sum] stop requested; background polling halted. "
          "Run Once still works; Auto Poll resumes.")


# A stale stop.flag from a previous session must not gate this one.
try:
    os.remove(os.path.join(os.environ["ANSA_MCP_SUM_HOME"], "stop.flag"))
except Exception:
    pass

plugin.write_status("running", "bridge loaded (%s)"
                    % ("AUTO" if AUTO_START_POLL else "ON-DEMAND"))
print("[ansa-mcp-sum] bridge loaded. home=%s buttons=[MCP-Sum] mode=%s"
      % (plugin.CONFIG.home, "AUTO" if AUTO_START_POLL else "ON-DEMAND"))

if AUTO_START_POLL:
    if not _init_bridge():
        # The GUI may not be ready this early in startup; retry on the next
        # main-loop slot instead of giving up (this is not an error condition).
        # ANSA calls timer callbacks as fn(timer, data), so wrap it.
        def _deferred_init(timer=None, data=None):
            _init_bridge()
            return 0

        try:
            guitk.BCTimerSingleShot(0, _deferred_init, None)
        except Exception as exc:
            print("[ansa-mcp-sum] deferred init failed: %s" % exc)
