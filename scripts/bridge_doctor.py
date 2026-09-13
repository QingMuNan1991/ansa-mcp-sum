# -*- coding: utf-8 -*-
"""Bridge doctor - diagnose, heal, and HOT-RELOAD a stalled ansa-mcp-sum bridge.

Run it from inside ANSA through **MCP-Sum > Run Once**. That path calls
``plugin.ansa_mcp_process_one()`` directly, so it works even when the 200ms
timer that normally drains the queue has stopped firing.

What it fixes, in order
-----------------------
1. ``_busy``           re-entrancy guard. If one poll callback never returns,
                       every later tick returns immediately - the bridge then
                       reports "running" forever while consuming nothing, and
                       ``Run Once`` still works (it bypasses the guard), which
                       makes the failure look like "commands silently time out".
2. ``_stop_requested`` latched by the Stop button; never cleared by the window.
3. ``_poll_timer``     a timer that is "active" but no longer delivering
                       callbacks. ``Auto Poll`` is a **no-op** in that state
                       (``_init_bridge`` used to return True on
                       ``BCTimerIsActive`` alone), so the toolbar cannot recover
                       from it at all.
4. **Stale code.** ``ansa.ImportCode`` executes ``ansa_mcp_sum_autoload.py``
   once, at startup; the body then lives in ANSA's namespace for the life of
   the process and the file is never re-read. A fix edited on disk does nothing
   until either ANSA restarts or the file is re-executed - which is what
   ``_reload_autoload`` does here, so **no ANSA restart is needed**.

Two API facts this file depends on
----------------------------------
* there is no ``guitk.BCTimerDestroy`` in 25.x - only ``BCTimerStop``; the timer
  object is left to the GC.
* there is no ``guitk.BCDestroyWindow`` either. The doc for ``BCDestroy`` says
  to close a window with ``BCWindowAccept``/``BCWindowReject``. The bridge
  window is therefore *reused*, never recreated: it is the timer's parent, and
  ``BCWindowCreate`` documents ``name`` as the xml tag its gui data is stored
  under, asking for a unique string.
"""
import json
import os
import sys
import time

HOME = os.environ.get("ANSA_MCP_SUM_HOME") or r"C:\Users\Admin\.ansa-mcp-sum"
AUTOLOAD = (os.environ.get("ANSA_MCP_SUM_AUTOLOAD")
            or r"D:\ansa-mcp-sum\scripts\ansa_mcp_sum_autoload.py")

#: A private symbol that exists ONLY once the current autoload source has been
#: exec'd into the live namespace - it is how the doctor proves the reload
#: landed. Keep it as the single source of truth: v1.0.7 renamed the old
#: ``_teardown_timer`` to ``_reset_bridge_refs`` and every stale copy of the old
#: name made the doctor claim "code NOT current" no matter what really happened.
RELOAD_MARKER = "_reset_bridge_refs"


def _namespaces():
    """Every module dict that looks like the autoload namespace.

    ``ansa.ImportCode`` executes the file body somewhere we do not control
    (``__main__`` when driven from ANSA_TRANSL.py), so scanning sys.modules for
    the module's own private globals is more reliable than guessing a name.
    """
    found = []
    for name, mod in list(sys.modules.items()):
        try:
            d = getattr(mod, "__dict__", None)
        except Exception:
            continue
        if isinstance(d, dict) and "_poll_timer" in d and "_init_bridge" in d:
            found.append((name, d))
    return found


def _snapshot(g, guitk):
    timer = g.get("_poll_timer")
    snap = {
        "_tick_count": g.get("_tick_count"),
        "_busy": bool(g.get("_busy")),
        "_stop_requested": bool(g.get("_stop_requested")),
        "_error_streak": g.get("_error_streak"),
        "_poll_timer_is_none": timer is None,
        "_bridge_window_is_none": g.get("_bridge_window") is None,
    }
    started = g.get("_start_time")
    if started:
        snap["session_uptime_s"] = round(time.time() - started, 1)
    tick = g.get("_last_tick_at")
    if tick:
        snap["seconds_since_last_tick"] = round(time.time() - tick, 1)
    if timer is not None:
        try:
            snap["BCTimerIsActive"] = bool(guitk.BCTimerIsActive(timer))
        except Exception as exc:
            snap["BCTimerIsActive"] = "%s: %s" % (type(exc).__name__, exc)
    return snap


def _reload_autoload(g, report):
    """Re-execute the autoload file into its own live namespace.

    This is what makes an edited file take effect without restarting ANSA. The
    four toolbar buttons are registered by module-level ``@defbutton`` calls, so
    ``ansa.session.defbutton`` is replaced by a no-op for the duration of the
    exec - otherwise every reload would add another copy of each button. The
    existing buttons keep working across the reload because their functions
    look up ``_init_bridge`` and friends in *this* namespace, which the exec is
    refreshing.
    """
    if not os.path.isfile(AUTOLOAD):
        report["reload"] = "skipped: %s not found" % AUTOLOAD
        return False
    try:
        with open(AUTOLOAD, "r", encoding="utf-8") as f:
            source = f.read()
        code = compile(source, AUTOLOAD, "exec")
    except Exception as exc:
        report["reload"] = "compile failed: %s: %s" % (type(exc).__name__, exc)
        return False

    #: The old file did not define the marker, so its arrival is the proof that
    #: the new body actually ran.
    marker_before = RELOAD_MARKER in g

    import ansa
    session = getattr(ansa, "session", None)
    original = getattr(session, "defbutton", None) if session is not None else None
    if session is not None:
        session.defbutton = lambda *a, **k: (lambda fn: fn)
    try:
        exec(code, g)
    except Exception as exc:
        report["reload"] = "exec raised: %s: %s" % (type(exc).__name__, exc)
        return False
    finally:
        if session is not None and original is not None:
            session.defbutton = original

    report["reload"] = ("ok (marker %s %s -> %s)"
                        % (RELOAD_MARKER, marker_before, RELOAD_MARKER in g))
    return True


def main():
    from ansa import guitk

    report = {
        "home": HOME,
        "autoload": AUTOLOAD,
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stop_flag_present": os.path.exists(os.path.join(HOME, "stop.flag")),
    }

    hits = _namespaces()
    report["namespace_hits"] = [n for n, _ in hits]
    if not hits:
        report["verdict"] = ("autoload namespace not reachable from sys.modules; "
                             "cannot heal in place - reload the autoload script")
        return {"doctor": report}

    name, g = hits[0]
    report["namespace"] = name
    report["before"] = _snapshot(g, guitk)

    # ---- clear the soft gates -------------------------------------------
    g["_busy"] = False
    g["_busy_since"] = 0.0
    g["_busy_reported"] = False
    g["_stop_requested"] = False
    g["_error_streak"] = 0
    stop_flag = os.path.join(HOME, "stop.flag")
    if os.path.exists(stop_flag):
        try:
            os.remove(stop_flag)
            report["stop_flag_removed"] = True
        except Exception as exc:
            report["stop_flag_removed"] = "%s: %s" % (type(exc).__name__, exc)

    # ---- stop the old timer; keep the window (it is the timer's parent) --
    timer = g.get("_poll_timer")
    had_timer = timer is not None
    if timer is not None:
        try:
            guitk.BCTimerStop(timer)
        except Exception as exc:
            report["timer_stop_error"] = "%s: %s" % (type(exc).__name__, exc)
    g["_poll_timer"] = None

    # ---- pull the current file back into this live namespace -------------
    report["reload_ok"] = _reload_autoload(g, report)
    report["code_is_current"] = RELOAD_MARKER in g

    # ---- rebuild ---------------------------------------------------------
    if g.get("_poll_timer") is not None:
        # The reloaded module body already started the timer (AUTO_START_POLL).
        report["rebuild_ok"] = "not needed - reload started the timer"
    elif had_timer or g.get("AUTO_START_POLL"):
        try:
            report["rebuild_ok"] = bool(g["_init_bridge"]())
        except Exception as exc:
            report["rebuild_ok"] = False
            report["rebuild_error"] = "%s: %s" % (type(exc).__name__, exc)
    else:
        # On-demand mode (ANSA_MCP_SUM_AUTO_POLL=0) and no timer was running:
        # there is nothing stalled here, so do not silently turn polling on.
        report["rebuild_ok"] = "skipped - on-demand mode, no timer was running"

    report["after"] = _snapshot(g, guitk)
    live = (report["after"].get("_poll_timer_is_none") is False
            and report["after"].get("BCTimerIsActive") is True)
    report["verdict"] = ("timer live - status.json should refresh within ~5s"
                         if live else "timer NOT live - see errors above")

    payload = {"doctor": report}
    #: A copy on disk, so the diagnosis survives a failed/late result write - the
    #: bridge was just dead, and the result file is delivered by that same bridge.
    out = os.path.join(HOME, "logs",
                       "bridge_doctor_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        report["report_file"] = out
    except Exception as exc:
        report["report_file"] = "write failed: %s: %s" % (type(exc).__name__, exc)
    return payload


if __name__ == "__main__":
    print(json.dumps(main(), ensure_ascii=False, indent=2))
