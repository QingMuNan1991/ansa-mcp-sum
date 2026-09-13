"""Minimal MCP bridge for ANSA."""

#: Single source of truth for the distribution version.
#:
#: ``pyproject.toml`` reads this through ``dynamic = ["version"]`` so the two
#: declarations cannot drift apart again. They did drift: both said ``0.1.0``
#: (the fork baseline), which made ``status.json`` unable to tell a plain
#: ``ansa-mcp`` build from this one — see README §8.
#:
#: Version history is kept in CHANGELOG-ish form in README §8:
#:   0.1.0  fork baseline
#:   1.0.2  review batch 1: timeout payload, tool annotations, pyproject bounds,
#:          artifact relocation, optional-API layer
#:   1.0.3  fix bridge-window leak on hot reload: the ANSA_MCP_SUM_Bridge window
#:          handle now lives in plugin.GUI_HANDLES (never re-executed), so a
#:          bridge_doctor re-exec reuses the same window instead of stacking a
#:          second one under the same xml tag (was the "几十个窗口" symptom)
#:   1.0.4  embed ANSA API documentation inside ansa-mcp-sum (ansa_api_doc_search /
#:          ansa_api_doc_lookup / ansa_api_doc_modules / ansa_api_doc_categories) as a
#:          replica of the ansa-api server, plus a live pre-flight validator
#:          (validate_script + execute_script validate=True) that refuses to run a
#:          script referencing an ANSA API call missing from the running build.
#:   1.0.5  fix pre-flight rejection path: ipc.error_response now accepts an optional
#:          `data` kwarg, so execute_script(validate=True) on a script with missing
#:          ANSA calls returns a clean structured rejection (data.missing / checked)
#:          instead of crashing with TypeError on error_response(data=...).
#:   1.0.6  bundle ansa_api_index.json into the package (src/ansa_mcp_sum/) and make
#:          api_doc prefer the bundled copy over the ansa_tools resource, so the
#:          project is self-contained and shareable; package-data ships the JSON in wheels.
#:   1.0.7  crash-proof the auto-poll bridge (root cause of the 2026-09-13 ANSA
#:          segfault, EXCEPTION 0xC0000005). FIX 1: the bridge window's title-bar
#:          Close (X) button is hidden (ansa_mcp_sum_autoload._ensure_window), so the
#:          user can no longer destroy the window and its child timer - the dangling
#:          C++ handle that the next Auto Poll used to dereference and crash on.
#:          FIX 2: _init_bridge no longer calls guitk.BCTimerIsActive on the retained
#:          timer handle (a native call that segfaults uncatchably on a dangling
#:          handle); timer health is now judged purely from the Python-side heartbeat
#:          _last_tick_at, and a stale timer is rebuilt by dropping the old reference
#:          (never dereferencing it) via _reset_bridge_refs before creating a fresh one.
__version__ = "1.0.7"
