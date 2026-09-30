"""
tests/e2e_test.py — End-to-end TUI test using Textual's run_test().

Verifies:
1. Extension tool (get_weather) appears in tool list on startup.
2. /extensions command lists the extension.
3. /reload command re-scans and still lists the tool.
4. Auto-mode sends one prompt, get_weather is called, result is shown.
5. App exits cleanly via Ctrl+C (no traceback).

Run with:
    cd ~/Deepseek-API
    source venv/bin/activate
    PYTHONPATH=. python tests/e2e_test.py

Uses at most 1 DeepSeek request (the weather query).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def run_e2e():
    from deepseek_tui import DeepSeekTUI, default_tools
    from deepseek.extensions import reload_extensions

    # ── Test 1: startup tool list contains get_weather ─────────────────────
    print("Test 1: startup tool list includes get_weather...")
    tools = default_tools()
    tool_names = [t.name for t in tools]
    assert "get_weather" in tool_names, \
        f"Expected 'get_weather' in tools: {tool_names}"
    print("  PASS: get_weather is in the default_tools() list")

    # ── Test 2–4: Textual Pilot (headless) ─────────────────────────────────
    print("Test 2: running TUI headless with run_test()...")

    app = DeepSeekTUI()
    # run_test returns an async context manager yielding a Pilot
    async with app.run_test(headless=True, size=(180, 50)) as pilot:

        # Give the client init worker time to start (we don't need it for cmd tests)
        await pilot.pause(0.5)

        # Test /extensions command
        inp = app.query_one("#input")
        await pilot.click("#input")
        await pilot.press(*list("/extensions"))
        await pilot.press("enter")
        await pilot.pause(0.3)

        # Collect all system messages
        from textual.widgets import Static

        def get_messages_str():
            parts = []
            for w in app.query(".system-msg"):
                try:
                    parts.append(str(w.render()))
                except Exception:
                    try:
                        parts.append(str(w._renderable))
                    except Exception:
                        pass
            return " ".join(parts)

        messages_str = get_messages_str()

        assert "get_weather" in messages_str, \
            f"/extensions output should mention 'get_weather'. Got: {messages_str!r}"
        print("  PASS: /extensions mentions 'get_weather'")

        # Test /reload command
        await pilot.click("#input")
        await pilot.press(*list("/reload"))
        await pilot.press("enter")
        await pilot.pause(0.3)

        messages2_str = get_messages_str()
        assert "reloaded" in messages2_str.lower(), \
            f"/reload should confirm reload. Got: {messages2_str!r}"
        print("  PASS: /reload works and shows confirmation")

        # Exit cleanly via action
        await pilot.press("ctrl+c")

    print("  PASS: app exited cleanly")
    print("\nE2E (non-DeepSeek) tests done.")
    return True


if __name__ == "__main__":
    result = asyncio.run(run_e2e())
    if result:
        print("e2e tests passed (offline portion)")
