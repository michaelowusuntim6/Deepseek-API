"""
Authentication — Playwright login + session capture.

Mirrors the Windows-Copilot-API design: the browser is used ONLY to establish a
signed-in session (handling the AWS WAF / "verify you're human" check and the
email/password form). It does not chat. We then capture the bearer token from
`localStorage.userToken` plus the session cookies, and hand them to the
pure-HTTP client in `deepseek.client`.

A persistent Chromium profile means the human-check is a one-time thing: once
you've signed in, later runs reuse the profile and capture the token headlessly.

    from deepseek.auth import get_session
    session = get_session()          # logs in (visible) the first time, else headless
    print(session.token[:8], "...")
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
# Override with DEEPSEEK_PROFILE_DIR to reuse an existing signed-in Chrome profile.
DEFAULT_PROFILE_DIR = Path(os.getenv("DEEPSEEK_PROFILE_DIR", ROOT / "session" / "profile"))
DEFAULT_SESSION_FILE = ROOT / "session" / "session.json"

CHAT_URL = "https://chat.deepseek.com/"
SIGNIN_URL = "https://chat.deepseek.com/sign_in"

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
# Token is trusted for this long before we refresh it from the browser again.
SESSION_MAX_AGE = 6 * 60 * 60  # 6 hours

# Browser/profile selection. `auto` prefers the user's real installed Chrome
# profile (so an existing chat.deepseek.com login is reused instantly), then a
# fresh system-Chrome profile, then Playwright's bundled Chromium.
_BROWSER_MODES = ("auto", "system", "fresh", "bundled")

# Substrings that mean "the Chrome user-data-dir is locked by a running Chrome".
_LOCK_MARKERS = ("processsingleton", "singletonlock", "profile is already",
                 "is already in use", "already running", "cannot be opened because the profile")

# Chrome profile folder names ("Default", "Profile 1", ...). Used to preselect a
# profile with --profile-directory so system Chrome does not open the chooser.
_PROFILE_NAME_RE = re.compile(r"^(Default|Profile \d+)$", re.IGNORECASE)

LOCK_WARNING = (
    "[auth] Your Chrome is currently running, so its profile is locked. "
    "Using a separate Playwright profile instead.\n"
    "[auth] To use your real Chrome profile, close Chrome completely and try again."
)

# Dedicated logger that always emits to stderr (message only), so the [auth] lines
# are visible both when run from the CLI (`python -m deepseek.auth`) and when the
# server calls this from a worker thread, without depending on uvicorn's config.
log = logging.getLogger("deepseek.auth")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False


class LoginRequired(RuntimeError):
    """Raised when no usable session exists and interactive login is disallowed
    (e.g. inside the server, where we can't pop open a browser mid-request).
    The message tells the user how to log in."""

    DEFAULT = (
        "No DeepSeek session found. Log in first by running:\n"
        "    python -m deepseek.auth\n"
        "This opens a browser once so you can sign in and clear the human-check; "
        "afterwards the server reuses the saved session automatically."
    )

    def __init__(self, message: str = DEFAULT):
        super().__init__(message)


@dataclass
class Session:
    """A captured signed-in DeepSeek session."""

    token: str
    cookies: Dict[str, str]
    user_agent: str
    captured_at: float

    @property
    def age(self) -> float:
        return time.time() - self.captured_at

    def save(self, path: Path = DEFAULT_SESSION_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = DEFAULT_SESSION_FILE) -> Optional["Session"]:
        if not path.exists():
            return None
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None


# --- in-page helpers --------------------------------------------------------

# Reads the bearer token the web app stores after login. Shape:
#   localStorage.userToken = {"value":"<TOKEN>","__version":"0"}
_READ_TOKEN_JS = """
() => {
  try {
    const raw = window.localStorage.getItem('userToken');
    if (!raw) return null;
    const o = JSON.parse(raw);
    return (o && o.value) ? o.value : null;
  } catch (e) { return null; }
}
"""


def _safe_evaluate(page, js: str):
    """Run page.evaluate, swallowing the benign 'Execution context was destroyed'
    error that fires when a navigation (e.g. the post-login redirect) happens to
    land mid-evaluate. Returns None on any such transient failure instead of
    raising, so callers can just retry on the next poll."""
    try:
        return page.evaluate(js)
    except Exception as e:
        msg = str(e)
        if "Execution context was destroyed" in msg or "navigation" in msg.lower():
            return None
        raise


def _capture_from_context(context, page) -> Optional[Session]:
    """Read token + cookies + UA off a logged-in page, or None if not signed in."""
    token = _safe_evaluate(page, _READ_TOKEN_JS)
    if not token:
        return None
    cookies = {c["name"]: c["value"] for c in context.cookies()}
    ua = _safe_evaluate(page, "() => navigator.userAgent") or ""
    return Session(token=token, cookies=cookies, user_agent=ua, captured_at=time.time())


def _wait_for_token(page, timeout: float) -> Optional[str]:
    """Poll localStorage.userToken until it appears or we time out. Tolerates
    transient navigations (e.g. the Google OAuth redirect chain) that briefly
    destroy the page's JS execution context — those just count as "no token
    yet" rather than aborting the whole login."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        token = _safe_evaluate(page, _READ_TOKEN_JS)
        if token:
            return token
        page.wait_for_timeout(1000)
    return None


def _safe_goto(page, url: str) -> None:
    """Navigate, tolerating the benign `net::ERR_ABORTED` that DeepSeek's SPA
    redirects and the AWS WAF check often raise mid-navigation. We wait only for
    the initial commit, not full `load`; the token-poll afterwards is what
    actually gates sign-in, so an aborted/partial load here is fine."""
    try:
        page.goto(url, wait_until="commit", timeout=60000)
    except Exception as e:
        print(f"[auth] navigation to {url} was interrupted ({type(e).__name__}); "
              "continuing — finish signing in in the window if needed.")
    # Give the SPA a moment to render its login UI before we touch the form.
    page.wait_for_timeout(2000)


def _chrome_profile_bases() -> list[Path]:
    """Candidate Chrome *user-data* directories, most-preferred first.

    Each is a Chrome profile ROOT (it contains a `Default/` subdirectory), which
    is what Playwright's `user_data_dir` expects — not the `Default` dir itself.
    """
    home = Path.home()
    return [
        home / ".config" / "google-chrome",       # Chrome stable
        home / ".config" / "google-chrome-beta",  # Chrome beta
        home / ".config" / "chromium",            # Chromium
    ]


def find_system_chrome_profile() -> Optional[Path]:
    """Return the path to the user's real Chrome profile directory, or None if no
    installed Chrome profile is found.

    Honors the `DEEPSEEK_CHROME_PROFILE` env override (used directly if it is a
    directory). Otherwise returns the first candidate base that exists AND
    contains a `Default/` subdirectory.
    """
    override = os.getenv("DEEPSEEK_CHROME_PROFILE")
    if override:
        p = Path(override).expanduser()
        return p if p.is_dir() else None
    for base in _chrome_profile_bases():
        if base.is_dir() and (base / "Default").is_dir():
            return base
    return None


def _browser_mode() -> str:
    mode = (os.getenv("DEEPSEEK_BROWSER_MODE") or "auto").strip().lower()
    return mode if mode in _BROWSER_MODES else "auto"


def _is_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _LOCK_MARKERS)


def _split_system_profile(path: Path) -> tuple[Path, str]:
    """Return (user_data_dir, profile_directory_name) for a detected or overridden
    Chrome path.

    If the path already points at a profile folder (`Default` / `Profile N`), use
    its parent as the data dir and that folder as the profile name; otherwise treat
    the path as the data dir and preselect the `Default` profile. Preselecting
    avoids Chrome's profile chooser opening `about:blank`.
    """
    if _PROFILE_NAME_RE.match(path.name):
        return path.parent, path.name
    return path, "Default"


def _launch_candidates(fallback_profile: Path):
    """Ordered `(mode_label, channel, user_data_dir, profile_directory)` specs.

    system-chrome = the user's real profile (needs Chrome installed);
    fresh-chrome  = a Playwright-managed profile under system Chrome;
    bundled       = Playwright's bundled Chromium (no `channel`).
    `profile_directory` is passed for the real profile only; the fresh/bundled dirs
    are dedicated and show no chooser, so they get None.
    """
    mode = _browser_mode()
    sys_profile = find_system_chrome_profile()
    if sys_profile is not None:
        udd, pname = _split_system_profile(sys_profile)
        a = ("system-chrome", "chrome", udd, pname)
    else:
        a = None
    b = ("fresh-chrome", "chrome", fallback_profile, None)
    c = ("bundled-chromium", None, fallback_profile, None)
    if mode == "bundled":
        return [c]
    if mode == "fresh":
        return [b, c]
    # auto and system both prefer the real profile, then fall back down.
    return ([a] if a else []) + [b, c]


def _open_context(p, *, headless: bool, fallback_profile: Path):
    """Launch the first browser/profile path that works for the current mode.

    Each candidate is attempted exactly once (no per-path retry loop). Returns a
    Playwright persistent context. On a locked real-Chrome profile it prints the
    lock warning and falls through; it never forces the lock. `--no-sandbox` is
    suppressed from Playwright's defaults so Chrome shows no security banner — the
    sandbox works on a normal desktop; a genuine startup failure has another cause
    (permissions, a stale SingletonLock) that must be diagnosed, not worked around.
    Raises RuntimeError only if nothing can launch.
    """
    last_err: Optional[BaseException] = None
    for (label, channel, udd, profile_directory) in _launch_candidates(fallback_profile):
        args = list(LAUNCH_ARGS)
        if profile_directory:
            args.append(f"--profile-directory={profile_directory}")
        kwargs = {
            "headless": headless,
            "args": args,
            "ignore_default_args": ["--no-sandbox"],
        }
        if channel:
            kwargs["channel"] = channel
        try:
            udd.mkdir(parents=True, exist_ok=True)
            context = p.chromium.launch_persistent_context(str(udd), **kwargs)
            log.info("[auth] using profile: %s (mode: %s)", udd, label)
            return context
        except Exception as exc:
            last_err = exc
            if label == "system-chrome" and _is_lock_error(exc):
                print(LOCK_WARNING)
            else:
                log.warning("[auth] could not start %s (%s); trying next option",
                            label, str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    raise RuntimeError(f"No usable browser or profile was available: {last_err}")


def login(
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    headless: bool = False,
    assume_logged_out: bool = False,
) -> Session:
    """Interactive login. Opens a visible window and waits for you to sign in by
    hand (and clear the AWS WAF human-check); once a token appears it captures
    and saves the session. The persistent profile means later `get_session()`
    calls capture the token headlessly without a window.

    `assume_logged_out=True` skips the initial "are we already signed in?" hop to
    CHAT_URL and goes straight to the sign-in page. Callers that have just
    confirmed there's no token (e.g. get_session after a failed headless refresh)
    pass this so the window doesn't visibly bounce CHAT_URL -> SIGNIN_URL."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = _open_context(p, headless=headless, fallback_profile=profile_dir)
        page = context.pages[0] if context.pages else context.new_page()

        # Normally we first land on CHAT_URL to reuse an already-signed-in
        # profile. When the caller already knows we're logged out, skip straight
        # to the sign-in page so the window doesn't appear to "refresh".
        existing = None
        if not assume_logged_out:
            _safe_goto(page, CHAT_URL)
            existing = page.evaluate(_READ_TOKEN_JS)

        if not existing:
            _safe_goto(page, SIGNIN_URL)
            print("[auth] Please sign in in the window (solve the human-check if "
                  "shown). Waiting for the session...")
            if not _wait_for_token(page, timeout=300):
                context.close()
                raise RuntimeError("Login timed out — no token captured.")

        session = _capture_from_context(context, page)
        context.close()
        if session is None:
            raise RuntimeError("Logged in but could not read the token.")
        session.save()
        return session


def _headless_refresh(profile_dir: Path) -> Optional[Session]:
    """Try to capture a token headlessly from the persistent profile. Returns a
    saved Session if the profile is still signed in, else None. Never opens a
    visible window."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = _open_context(p, headless=True, fallback_profile=profile_dir)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            _safe_goto(page, CHAT_URL)
            session = _capture_from_context(context, page)
        finally:
            context.close()

    if session is not None:
        session.save()
    return session


def get_session(
    profile_dir: Path = DEFAULT_PROFILE_DIR,
    session_file: Path = DEFAULT_SESSION_FILE,
    max_age: int = SESSION_MAX_AGE,
    allow_interactive: bool = True,
) -> Session:
    """Return a usable session: cached file if fresh, else a headless refresh
    from the browser profile.

    If neither works and `allow_interactive` is True, open a visible window for
    manual sign-in. If it's False (the server's case — we can't pop a browser
    mid-request), raise `LoginRequired` telling the user to run the login step.

    Note: this uses Playwright's *sync* API, so it must not be called from inside
    an asyncio event loop — call it from a worker thread (e.g. run_in_threadpool)."""
    cached = Session.load(session_file)
    if cached and cached.age < max_age:
        return cached

    # Try a headless refresh from the (presumably logged-in) persistent profile.
    session = _headless_refresh(profile_dir)
    if session is not None:
        return session

    if not allow_interactive:
        raise LoginRequired()

    # Not logged in yet — open a visible window so the user can sign in (and
    # clear the human-check) by hand. The persistent profile means this only
    # happens once — later calls capture the token headlessly. We just confirmed
    # (above) there's no token, so go straight to the sign-in page.
    print("[auth] No valid session found — opening a browser window to log in...")
    return login(profile_dir=profile_dir, assume_logged_out=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m deepseek.auth",
        description=(
            "Sign in to DeepSeek once and save the session. By default this "
            "prefers your installed Chrome and its real profile, so an existing "
            "chat.deepseek.com login is reused with no password prompt."
        ),
    )
    parser.add_argument(
        "--browser", choices=_BROWSER_MODES, default=None,
        help=(
            "which browser/profile to use: 'system' = your real Chrome profile, "
            "'fresh' = a separate Playwright Chrome profile, 'bundled' = "
            "Playwright's Chromium, 'auto' (default) = try them in order."
        ),
    )
    args = parser.parse_args()
    if args.browser:
        os.environ["DEEPSEEK_BROWSER_MODE"] = args.browser

    try:
        s = login()
    except Exception as exc:
        print(f"[auth] login failed: {exc}")
        raise SystemExit(1)
    # Never print the token itself — only its length and the cookie count.
    print(f"[auth] captured a session token ({len(s.token)} chars, "
          f"{len(s.cookies)} cookies); saved to {DEFAULT_SESSION_FILE}")
