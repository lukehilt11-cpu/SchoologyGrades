"""Shared code for Schoology Grade Checker and Schoology Setup.

Importing this module moves the program into its "data" folder. It provides the
browser start-up, login and course-list helpers that both programs use.
Selenium itself is imported on first use, so program windows open instantly.
"""

import atexit
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from urllib.parse import urlparse

def _selenium():
    """Import Selenium the first time it's needed (not at program start)."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.keys import Keys
    except ImportError as e:
        # A .pyw has no console, so say exactly what to do.
        py = sys.executable.replace("pythonw.exe", "python.exe")
        raise RuntimeError(
            "Selenium isn't installed for the Python that opened this file:\n"
            f"{sys.executable}\n\n"
            "Open a Command Prompt and run:\n"
            f'"{py}" -m pip install selenium'
        ) from e
    return webdriver, Service, Keys


# Everything the program creates (config, saved login, logs, report) lives in a
# "data" folder next to the program, so the folder with the programs stays tidy.
def locate_app_dir():
    """Folder that holds the programs: the .exe's folder (or the folder containing
    the .app on macOS), or, when run from source, the folder above "source"."""
    if getattr(sys, "frozen", False):
        d = os.path.dirname(sys.executable)
        if sys.platform == "darwin" and ".app/Contents/MacOS" in d:
            d = os.path.dirname(d.split(".app/Contents/MacOS")[0] + ".app")
        return d
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here) if os.path.basename(here).lower() == "source" else here


APP_DIR = locate_app_dir()

# macOS runs some downloaded, unsigned apps from a hidden read-only copy
# ("App Translocation"), which hides the folder the app was really saved in.
TRANSLOCATED = "/AppTranslocation/" in APP_DIR


def _usable_dir(path):
    """Create path if needed and make sure we can really write to it."""
    os.makedirs(path, exist_ok=True)
    probe = os.path.join(path, ".write_test")
    with open(probe, "w", encoding="utf-8") as f:
        f.write("ok")
    os.remove(probe)
    return path


# Per-user data location, used when the program's own folder can't (or shouldn't) hold it.
_USER_BASE = os.environ.get("LOCALAPPDATA") or os.path.join(
    os.path.expanduser("~"), "Library", "Application Support"
)
_USER_DATA = os.path.join(_USER_BASE, "SchoologyGrades")

# Apps dragged into a shared Applications folder keep their data per user, not next to the app.
PER_USER = sys.platform == "darwin" and os.path.basename(APP_DIR.rstrip("/")) == "Applications"

try:
    DATA_DIR = _usable_dir(_USER_DATA if PER_USER else os.path.join(APP_DIR, "data"))
except Exception:
    # The program's own folder isn't writable (read-only disk, translocated app,
    # protected folder): fall back to a per-user folder so it still works.
    try:
        DATA_DIR = _usable_dir(_USER_DATA)
    except Exception:
        DATA_DIR = os.path.expanduser("~")

try:
    # One-time tidy-up: pull files from the old flat layout into data/.
    for _name in ("config.json", "selenium_profile", "error_log.txt", "grade_report.html", "debug"):
        _old = os.path.join(APP_DIR, _name)
        _new = os.path.join(DATA_DIR, _name)
        if os.path.exists(_old) and not os.path.exists(_new):
            try:
                shutil.move(_old, _new)
            except Exception:
                pass
    os.chdir(DATA_DIR)
except Exception:
    pass

EMAIL_SEL = "input[name='loginfmt'], input[id='edit-name'], input[type='email']"
PASS_SEL = "input[name='passwd'], input[id='edit-pass'], input[type='password']"
STAY_SIGNED_IN_SEL = "input#idSIButton9, button#idSIButton9"
TITLE_SEL = ".page-title, h1, .header-title"
# The class page lists one badge per grading period (Q1, Q2, ...) and the
# cumulative "Course Grade" in a summary line at the bottom. We want the
# cumulative one, never "the first badge on the page" (which is Q1).
GRADE_SEL = ".summary-course .course-grade-value .rounded-grade"
GRADE_FALLBACK_SEL = "tr.course-row .rounded-grade, .course-grade-value .rounded-grade"


# --------------------------------------------------------------------------
# Icons
# --------------------------------------------------------------------------
def resource_path(relative):
    """Path to a file bundled with the program (works from source and inside the .exe/.app)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


def app_version():
    """The version number from the VERSION file (bundled with the programs)."""
    try:
        with open(resource_path("VERSION"), "r", encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except Exception:
        return "dev"


def set_window_icon(root, name):
    """Show our icon in the window's title bar and taskbar. Best effort: never raises."""
    try:
        if sys.platform == "win32":
            root.iconbitmap(resource_path(os.path.join("icons", f"{name}.ico")))
        else:
            import tkinter as tk

            photo = tk.PhotoImage(file=resource_path(os.path.join("icons", f"{name}_256.png")))
            root.iconphoto(True, photo)
            root._icon_photo = photo  # keep a reference so it isn't garbage-collected
    except Exception:
        pass


# --------------------------------------------------------------------------
# "A newer version is available" notice, and "Update now"
# --------------------------------------------------------------------------
UPDATE_BASE = "https://github.com"
UPDATE_CHECK_HOURS = 12  # how long to remember the answer, so GitHub is asked rarely
UPDATE_ALLOWED_HOSTS = set()  # extra download hosts (only ever filled in by tests)
DEFAULT_REPO = "lukehilt11-cpu/SchoologyGrades"  # used for Help links when there is no REPO file


def read_repo():
    """The GitHub repository releases are published to, as "owner/name", or "".

    It comes from a small file named REPO that the GitHub build adds automatically.
    Without that file (a local build) the automatic update notice is switched off.
    """
    try:
        with open(resource_path("REPO"), "r", encoding="utf-8") as f:
            repo = f.read().strip()
    except Exception:
        return ""
    return repo if re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo) else ""


def repo_or_default():
    return read_repo() or DEFAULT_REPO


def releases_url():
    return f"{UPDATE_BASE}/{repo_or_default()}/releases"


def website_url():
    owner, name = repo_or_default().split("/", 1)
    return f"https://{owner}.github.io/{name}/"


def _version_key(text):
    """'v1.10.2' -> (1, 10, 2), so that 1.10 counts as newer than 1.9."""
    return tuple(int(n) for n in re.findall(r"\d+", text or ""))[:4]


def _host_allowed(host):
    host = (host or "").lower()
    return (
        host == "github.com" or host.endswith(".github.com") or host.endswith(".githubusercontent.com")
        or host in UPDATE_ALLOWED_HOSTS
    )


def _url_allowed(url):
    parts = urllib.parse.urlparse(url)
    return _host_allowed(parts.hostname) and (parts.scheme == "https" or parts.hostname in UPDATE_ALLOWED_HOSTS)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Follows redirects, but only to GitHub's own addresses."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_allowed(newurl):
            raise urllib.error.URLError("redirected to an address that isn't allowed")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_safe(url, timeout):
    if not _url_allowed(url):
        raise urllib.error.URLError("address isn't allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "SchoologyGradeChecker"})
    return urllib.request.build_opener(_SafeRedirect).open(request, timeout=timeout)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # don't follow: the address it points to IS the answer


def _latest_release_tag(repo, timeout=3):
    """The newest release's tag (e.g. "v1.2"), "" if there is none yet, None if we couldn't find out.

    Reads where github.com/<repo>/releases/latest redirects to: no login, and none of
    the strict limits of GitHub's API (a whole classroom shares one internet address).
    """
    request = urllib.request.Request(
        f"{UPDATE_BASE}/{repo}/releases/latest",
        method="HEAD",
        headers={"User-Agent": "SchoologyGradeChecker"},
    )
    try:
        urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            match = re.search(r"/releases/tag/([^/?#]+)", e.headers.get("Location", ""))
            return urllib.parse.unquote(match.group(1)) if match else ""
        return None
    except Exception:
        return None
    return None


def fetch_release_info(repo, timeout=4):
    """The small latest.json the build publishes: what's new, and a checksum for each download."""
    try:
        with _open_safe(f"{UPDATE_BASE}/{repo}/releases/latest/download/latest.json", timeout) as r:
            data = json.loads(r.read(200_000).decode("utf-8"))
        files = {}
        for name, meta in (data.get("files") or {}).items():
            if (isinstance(meta, dict) and isinstance(meta.get("size"), int)
                    and re.fullmatch(r"[0-9a-f]{64}", str(meta.get("sha256", "")).lower())):
                files[str(name)] = {"sha256": str(meta["sha256"]).lower(), "size": meta["size"]}
        return {"version": str(data.get("version", "")), "notes": str(data.get("notes", "")).strip()[:600], "files": files}
    except Exception:
        return None


def _check_for_update(force=False):
    """("update", info) | ("current", None) | ("unknown", None)."""
    try:
        repo = read_repo() or (DEFAULT_REPO if force else "")
        current = _version_key(app_version())
        if not repo or not current:
            return "unknown", None
        cache = {}
        try:
            with open("update_check.json", "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            pass
        fresh = (
            not force and cache.get("repo") == repo and "latest" in cache
            and time.time() - float(cache.get("checked", 0)) < UPDATE_CHECK_HOURS * 3600
        )
        if fresh:
            latest, info = str(cache.get("latest", "")), cache.get("info")
        else:
            latest = _latest_release_tag(repo)
            if latest is None:
                return "unknown", None  # couldn't tell just now
            info = None
            if latest and _version_key(latest) > current:
                meta = fetch_release_info(repo) or {}
                info = {
                    "version": latest.lstrip("vV"),
                    "url": f"{UPDATE_BASE}/{repo}/releases/latest",
                    "notes": meta.get("notes", ""),
                    "files": meta.get("files", {}),
                }
            try:
                with open("update_check.json", "w", encoding="utf-8") as f:
                    json.dump({"repo": repo, "checked": time.time(), "latest": latest, "info": info}, f)
            except OSError:
                pass
        if latest and _version_key(latest) > current:
            return "update", info or {
                "version": latest.lstrip("vV"), "url": f"{UPDATE_BASE}/{repo}/releases/latest", "notes": "", "files": {},
            }
        return "current", None
    except Exception:
        return "unknown", None


def check_for_update(force=False):
    """The newer release's info ({"version", "url", "notes", "files"}), or None. Never raises, never slow."""
    status, info = _check_for_update(force)
    return info if status == "update" else None


def update_prompt_snoozed(version):
    """True if the person already said "Not now" to this version within the last day."""
    try:
        with open("update_prompt.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("version") == version and time.time() < float(data.get("until", 0))
    except Exception:
        return False


def snooze_update_prompt(version, hours=24):
    try:
        with open("update_prompt.json", "w", encoding="utf-8") as f:
            json.dump({"version": version, "until": time.time() + hours * 3600}, f)
    except OSError:
        pass


class UpdateError(Exception):
    pass


UPDATE_ASSETS = {"installer": "SchoologyGrades-Setup.exe", "mac": "SchoologyGrades.dmg"}


def install_kind():
    """How this copy runs: "installer" (Windows installer), "mac", "portable" (loose Windows programs), "source"."""
    if not getattr(sys, "frozen", False):
        return "source"
    if sys.platform == "darwin":
        return "mac"
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        installed = os.path.join(local, "Programs", "SchoologyGrades") if local else ""
        if installed and os.path.normcase(os.path.abspath(APP_DIR)).startswith(os.path.normcase(installed)):
            return "installer"
        return "portable"
    return "source"


def can_update_in_place(info):
    name = UPDATE_ASSETS.get(install_kind())
    return bool(name and name in (info.get("files") or {}) and read_repo())


def download_update(info, progress=None, dest_dir=None):
    """Download this system's installer from the release and check its size and checksum."""
    name = UPDATE_ASSETS.get(install_kind())
    meta = (info.get("files") or {}).get(name or "")
    repo = read_repo()
    if not name or not meta or not repo:
        raise UpdateError("an automatic update isn't available for this copy")
    dest = os.path.join(dest_dir or tempfile.mkdtemp(prefix="sg_update_"), name)
    digest, got, total = hashlib.sha256(), 0, meta["size"]
    with _open_safe(f"{UPDATE_BASE}/{repo}/releases/latest/download/{name}", 30) as response, open(dest, "wb") as out:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            got += len(chunk)
            if got > total:
                raise UpdateError("the download is bigger than expected")
            out.write(chunk)
            digest.update(chunk)
            if progress and total:
                progress(min(1.0, got / total))
    if got != total:
        raise UpdateError("the download is incomplete")
    if digest.hexdigest() != meta["sha256"]:
        raise UpdateError("the download doesn't match its checksum")
    return dest


MAC_UPDATE_SCRIPT = r"""#!/bin/bash
# Waits for the running program to quit, copies the new apps over the old ones, then reopens Schoology Grades.
PID="$1"; DMG="$2"; DEST="$3"
for i in $(seq 1 60); do kill -0 "$PID" 2>/dev/null || break; sleep 0.5; done
MNT="$(mktemp -d /tmp/sgupdate.XXXXXX)"
if ! hdiutil attach -nobrowse -quiet -readonly -mountpoint "$MNT" "$DMG"; then open "$DMG"; exit 1; fi
ok=1
for app in SchoologyGrades SchoologySetup SchoologyAutopilot; do
    [ -d "$MNT/$app.app" ] || continue
    rm -rf "$DEST/$app.app.old"
    [ -d "$DEST/$app.app" ] && mv "$DEST/$app.app" "$DEST/$app.app.old"
    if cp -R "$MNT/$app.app" "$DEST/"; then
        rm -rf "$DEST/$app.app.old"
    else
        rm -rf "$DEST/$app.app"
        [ -d "$DEST/$app.app.old" ] && mv "$DEST/$app.app.old" "$DEST/$app.app"
        ok=0
    fi
done
hdiutil detach -quiet "$MNT" >/dev/null 2>&1
rmdir "$MNT" 2>/dev/null
if [ "$ok" = 1 ]; then open "$DEST/SchoologyGrades.app"; else open "$DMG"; fi
"""


def apply_update(path):
    """Start installing the downloaded update. The caller must then quit. Returns what happened:
    "installer" (Windows installer started), "replacing" (a Mac helper will swap the apps), "opened"."""
    kind = install_kind()
    if kind == "installer":
        _spawn([path])
        return "installer"
    if kind == "mac":
        if os.access(APP_DIR, os.W_OK):
            script = os.path.join(os.path.dirname(path), "update.sh")
            with open(script, "w", encoding="utf-8") as f:
                f.write(MAC_UPDATE_SCRIPT)
            os.chmod(script, 0o755)
            _spawn(["/bin/bash", script, str(os.getpid()), path, APP_DIR])
            return "replacing"
        _spawn(["open", path])  # can't write to the apps' folder: let the person drag them
        return "opened"
    raise UpdateError("an automatic update isn't available for this copy")


# --------------------------------------------------------------------------
# Starting other programs and windows
# --------------------------------------------------------------------------
CHROME_URL = "https://www.google.com/chrome/"


def _clean_env():
    """Environment for a program we start. A single-file PyInstaller program that
    starts another one must not hand over its own unpacking variables."""
    env = dict(os.environ)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for key in list(env):
        if key.startswith("_PYI") or key == "_MEIPASS2":
            del env[key]
    return env


def _spawn(args):
    """Start a program that keeps running after this one closes."""
    kwargs = dict(
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=_clean_env(), close_fds=True,
    )
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)


def launch_sibling(name):
    """Start another program ("grades", "setup" or "autopilot") from the same place. True if it started."""
    programs = {"grades": "SchoologyGrades", "setup": "SchoologySetup", "autopilot": "SchoologyAutopilot"}
    scripts = {"grades": "grades.pyw", "setup": "setup.py", "autopilot": "autopilot.py"}
    try:
        if getattr(sys, "frozen", False):
            if sys.platform == "darwin":
                target = os.path.join(APP_DIR, programs[name] + ".app")
                if not os.path.isdir(target):
                    return False
                _spawn(["open", target])
            else:
                target = os.path.join(
                    os.path.dirname(sys.executable), programs[name] + (".exe" if os.name == "nt" else "")
                )
                if not os.path.isfile(target):
                    return False
                _spawn([target])
        else:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), scripts[name])
            if not os.path.isfile(script):
                return False
            _spawn([sys.executable, script])
        return True
    except Exception:
        return False


def find_chrome():
    """Path to Google Chrome, or None."""
    candidates = []
    if sys.platform == "win32":
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(var)
            if base:
                candidates.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        try:
            import winreg

            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(
                        hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
                    ) as key:
                        candidates.append(winreg.QueryValue(key, None))
                except OSError:
                    pass
        except ImportError:
            pass
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def open_app_window(url, width=620, height=720):
    """Show a page in a small bare Chrome window (no tabs, no address bar).

    Uses its own small profile so it never clashes with the program's saved login.
    Returns True if the window was started.
    """
    chrome = find_chrome()
    if not chrome:
        return False
    try:
        _spawn([
            chrome,
            f"--app={url}",
            f"--window-size={int(width)},{int(height)}",
            f"--user-data-dir={os.path.join(DATA_DIR, 'report_profile')}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-features=Translate",
        ])
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Plain-language errors
# --------------------------------------------------------------------------
class FriendlyError(Exception):
    """An error explained in plain words, with what the person can do about it.

    actions: any of "try_again", "open_setup", "download_chrome", "sign_in".
    kind: a short label; "needs_setup" makes the program open Setup by itself.
    """

    def __init__(self, title, message, actions=("try_again",), details="", kind="other"):
        super().__init__(message)
        self.title = title
        self.message = message
        self.actions = tuple(actions)
        self.details = details
        self.kind = kind


def _chain_text(exc):
    parts, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        parts.append(str(exc))
        exc = exc.__cause__ or exc.__context__
    return "\n".join(p for p in parts if p).split("\n\nStacktrace")[0].strip()


def explain(exc):
    """Turn any error into a FriendlyError. The technical text is kept as details."""
    if isinstance(exc, FriendlyError):
        return exc
    text = _chain_text(exc) or exc.__class__.__name__
    low = text.lower()

    def has(*needles):
        return any(n in low for n in needles)

    def make(title, message, actions=("try_again",), kind="other"):
        return FriendlyError(title, message, actions, text, kind)

    if has("cannot find chrome binary", "unable to obtain driver", "unable to locate or obtain driver",
           "no such driver", "chrome not found", "binary location"):
        return make(
            "Google Chrome isn't installed",
            "This program uses Google Chrome to read your grades. Install Chrome, then open the program again.",
            ("download_chrome",), "chrome_missing",
        )
    if has("chrome would not start", "user data directory is already in use", "devtoolsactiveport",
           "chrome failed to start", "session not created"):
        advice = (
            "A leftover Chrome window from an earlier run may be in the way. Try again. "
            "If it keeps happening, restart your computer."
        )
        if "onedrive" in low:
            advice += (
                " This program's folder also looks like it's inside OneDrive, which can get in "
                "Chrome's way: pause OneDrive syncing, or move the folder somewhere else."
            )
        return make("Chrome couldn't start", advice, ("try_again",), "chrome_stuck")
    if has("err_internet_disconnected", "err_name_not_resolved", "err_network_changed",
           "err_connection_refused", "err_connection_reset", "err_address_unreachable", "err_proxy"):
        return make(
            "Can't reach the internet",
            "Check your Wi-Fi or network connection, then try again.",
            ("try_again",), "no_internet",
        )
    if has("err_timed_out", "err_connection_timed_out", "timed out", "timeout"):
        return make(
            "Schoology is taking too long to answer",
            "Schoology may be busy or down. Wait a minute and try again.",
            ("try_again",), "slow",
        )
    if has("password box never appeared"):
        return make(
            "Couldn't reach the sign-in page",
            "The sign-in page didn't load. Check your internet connection, and that the school address in Setup is right.",
            ("try_again", "open_setup"), "no_signin_page",
        )
    if has("login did not complete"):
        return make(
            "Please sign in again",
            "Schoology didn't accept the saved sign-in. Choose Sign in and a browser window will open: "
            "sign in there (and approve it on your phone if asked), and the check carries on by itself. "
            "If your password has changed, open Setup instead.",
            ("sign_in", "open_setup"), "sign_in",
        )
    if has("asked for a login again"):
        return make(
            "Schoology signed you out",
            "Schoology asked you to sign in again in the middle of checking. Try again. "
            "If it keeps happening, choose Sign in to do it yourself in a browser window.",
            ("try_again", "sign_in"), "signed_out",
        )
    if has("couldn't find your course list"):
        return make(
            "Couldn't find your classes",
            "You're signed in, but the class list didn't appear. Wait a minute and try again.",
            ("try_again",), "no_courses",
        )
    if has("without a course number"):
        return make(
            "Your class list needs to be set up again",
            "Something in your saved classes isn't readable. Open Setup and pick your classes again.",
            ("open_setup",), "bad_config",
        )
    if has("selenium isn't installed"):
        return make("A required piece is missing", text, ("close",), "dev")
    return make(
        "Something went wrong",
        "An unexpected problem stopped the check. Try again. If it keeps happening, "
        "send the details below to whoever gave you this program.",
        ("try_again",), "other",
    )


def show_error_dialog(root, fe, icon_name="grades"):
    """A small friendly dialog. Returns "try_again", "open_setup", "show_saved", "sign_in" or "close"."""
    import tkinter as tk
    from tkinter import ttk

    choice = ["close"]
    win = tk.Toplevel(root)
    win.title(fe.title)
    win.resizable(False, False)
    try:
        win.transient(root)
    except Exception:
        pass
    set_window_icon(win, icon_name)

    frame = ttk.Frame(win, padding=18)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=fe.title, font=("Segoe UI", 13, "bold"), wraplength=400, justify="left").pack(anchor="w")
    ttk.Label(frame, text=fe.message, wraplength=400, justify="left").pack(anchor="w", pady=(8, 14))

    details = {"box": None}

    def toggle_details():
        if details["box"] is None:
            box = tk.Text(frame, height=7, width=56, wrap="word")
            box.insert("1.0", (fe.details or "No further details.") + f"\n\n(Saved in {os.path.join(DATA_DIR, 'error_log.txt')})")
            box.config(state="disabled")
            box.pack(pady=(10, 0))
            details["box"] = box
            details_button.config(text="Hide details")
        else:
            details["box"].destroy()
            details["box"] = None
            details_button.config(text="Show details")
        win.update_idletasks()

    def pick(action):
        if action == "download_chrome":
            webbrowser.open(CHROME_URL)
            action = "close"
        choice[0] = action if action in ("try_again", "open_setup", "show_saved", "sign_in") else "close"
        win.destroy()

    labels = {
        "try_again": "Try again", "open_setup": "Open Setup", "download_chrome": "Download Chrome",
        "show_saved": "Show saved grades", "sign_in": "Sign in",
    }
    row = ttk.Frame(frame)
    row.pack(fill="x")
    details_button = ttk.Button(row, text="Show details", command=toggle_details)
    details_button.pack(side="left")
    ttk.Button(row, text="Close", command=lambda: pick("close")).pack(side="right")
    first = None
    for action in reversed([a for a in fe.actions if a in labels]):
        button = ttk.Button(row, text=labels[action], command=lambda a=action: pick(a))
        button.pack(side="right", padx=(0, 6))
        first = button
    if first is not None:
        first.focus_set()
    win.protocol("WM_DELETE_WINDOW", lambda: pick("close"))

    win.update_idletasks()
    try:
        x = root.winfo_rootx() + (root.winfo_width() - win.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + 20
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.wait_visibility()
        win.grab_set()
    except Exception:
        pass
    root.wait_window(win)
    return choice[0]


# --------------------------------------------------------------------------
# Help menu, update prompt and troubleshooting pages (small Tk dialogs)
# --------------------------------------------------------------------------
def open_folder(path):
    """Show a folder in Explorer / Finder."""
    try:
        if os.name == "nt":
            os.startfile(path)
        elif sys.platform == "darwin":
            _spawn(["open", path])
        else:
            _spawn(["xdg-open", path])
    except Exception:
        pass


def _center_over(win, root):
    win.update_idletasks()
    try:
        x = root.winfo_rootx() + (root.winfo_width() - win.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + 30
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    except Exception:
        pass


class ProgressDialog:
    """A small window with a message and a progress bar (determinate or not)."""

    def __init__(self, root, title, text):
        import tkinter as tk
        from tkinter import ttk

        self.win = tk.Toplevel(root)
        self.win.title(title)
        self.win.resizable(False, False)
        try:
            self.win.transient(root)
        except Exception:
            pass
        set_window_icon(self.win, "grades")
        frame = ttk.Frame(self.win, padding=18)
        frame.pack()
        self.label = ttk.Label(frame, text=text, wraplength=340, justify="left")
        self.label.pack(anchor="w")
        self.bar = ttk.Progressbar(frame, length=340, mode="indeterminate", maximum=100)
        self.bar.pack(pady=(10, 0))
        self.bar.start(12)
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)  # not while it is working
        _center_over(self.win, root)

    def set(self, text=None, fraction=None):
        try:
            if text is not None:
                self.label.config(text=text)
            if fraction is not None:
                if str(self.bar.cget("mode")) != "determinate":
                    self.bar.stop()
                    self.bar.config(mode="determinate")
                self.bar["value"] = max(0.0, min(1.0, fraction)) * 100
        except Exception:
            pass

    def close(self):
        try:
            self.win.destroy()
        except Exception:
            pass


def _run_update(root, info):
    """Download the update with a progress bar, then hand over to the installer and quit."""
    from tkinter import messagebox

    dialog = ProgressDialog(root, "Updating", f"Downloading version {info['version']}...")
    state = {"progress": 0.0, "done": False, "path": None, "error": None}

    def work():
        try:
            state["path"] = download_update(info, progress=lambda f: state.__setitem__("progress", f))
        except Exception as e:
            state["error"] = e
        state["done"] = True

    threading.Thread(target=work, daemon=True).start()

    def fall_back(reason):
        log_error(f"Update failed: {reason}")
        if messagebox.askyesno(
            "Couldn't update automatically",
            "The update couldn't be installed automatically. Open the download page instead?",
            parent=root,
        ):
            webbrowser.open(info["url"])

    def poll():
        if not state["done"]:
            dialog.set(fraction=state["progress"])
            root.after(100, poll)
            return
        dialog.close()
        if state["error"] is not None:
            fall_back(repr(state["error"]))
            return
        try:
            result = apply_update(state["path"])
        except Exception as e:
            fall_back(repr(e))
            return
        if result == "opened":
            messagebox.showinfo(
                "Finish the update",
                "The new version's disk image is open. Drag both apps onto Applications to finish.",
                parent=root,
            )
        close_browser()
        try:
            root.destroy()
        except Exception:
            pass
        os._exit(0)  # the installer / helper needs this program to be closed

    root.after(100, poll)


def offer_update(root, info):
    """Tell the person a newer version exists and offer to install it.
    Returns True if an update was started (the caller must then leave the window open)."""
    import tkinter as tk
    from tkinter import ttk

    version = info["version"]
    win = tk.Toplevel(root)
    win.title("Update available")
    win.resizable(False, False)
    try:
        win.transient(root)
    except Exception:
        pass
    set_window_icon(win, "grades")
    frame = ttk.Frame(win, padding=18)
    frame.pack()
    ttk.Label(frame, text=f"Version {version} is available", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    notes = (info.get("notes") or "").strip()
    ttk.Label(
        frame, text=notes or "A newer version of Schoology Grade Checker is ready to download.",
        wraplength=380, justify="left",
    ).pack(anchor="w", pady=(8, 4))
    in_place = can_update_in_place(info)
    if not in_place:
        ttk.Label(
            frame, text="Download the new version and run it over this one. Your saved settings are kept.",
            wraplength=380, justify="left", foreground="#475569",
        ).pack(anchor="w", pady=(0, 4))
    row = ttk.Frame(frame)
    row.pack(fill="x", pady=(14, 0))

    def not_now():
        snooze_update_prompt(version)
        win.destroy()

    started = [False]

    def update_now():
        started[0] = True
        win.destroy()
        _run_update(root, info)

    def open_page():
        webbrowser.open(info["url"])
        win.destroy()

    ttk.Button(row, text="Not now", command=not_now).pack(side="right")
    main_button = ttk.Button(row, text="Update now" if in_place else "Open download page",
                             command=update_now if in_place else open_page)
    main_button.pack(side="right", padx=(0, 6))
    main_button.focus_set()
    ttk.Button(row, text="What's new", command=lambda: webbrowser.open(releases_url())).pack(side="left")
    win.protocol("WM_DELETE_WINDOW", not_now)
    _center_over(win, root)
    try:
        win.wait_visibility()
        win.grab_set()
    except Exception:
        pass
    root.wait_window(win)
    return started[0]


def check_updates_now(root):
    """Help > Check for updates: answer right away, even if we asked recently."""
    from tkinter import messagebox

    if app_version() == "dev":
        messagebox.showinfo("Updates", "This copy is running from the source code, so it doesn't check for updates.", parent=root)
        return
    dialog = ProgressDialog(root, "Checking for updates", "Checking...")
    state = {"done": False, "result": ("unknown", None)}

    def work():
        state["result"] = _check_for_update(force=True)
        state["done"] = True

    threading.Thread(target=work, daemon=True).start()

    def poll():
        if not state["done"]:
            root.after(100, poll)
            return
        dialog.close()
        status, info = state["result"]
        if status == "update":
            offer_update(root, info)
        elif status == "current":
            messagebox.showinfo("Updates", f"You're up to date (version {app_version()}).", parent=root)
        else:
            messagebox.showinfo("Updates", "Couldn't check right now. Check your internet connection and try again.", parent=root)

    root.after(100, poll)


def add_help_menu(root, extras=()):
    """Give a window a Help menu. extras: [(label, callback), ...] go above the standard items."""
    import tkinter as tk
    from tkinter import messagebox

    def about():
        messagebox.showinfo(
            "About",
            f"Schoology Grade Checker {app_version()}\n\n"
            "An independent project. It isn't made by, or connected to, Schoology or any school.\n\n"
            f"{website_url()}",
            parent=root,
        )

    menubar = tk.Menu(root)
    help_menu = tk.Menu(menubar, tearoff=0)
    help_menu.add_command(label="Website", command=lambda: webbrowser.open(website_url()))
    help_menu.add_command(label="What's new", command=lambda: webbrowser.open(releases_url()))
    help_menu.add_command(label="Check for updates...", command=lambda: check_updates_now(root))
    help_menu.add_separator()
    help_menu.add_command(label="Open the data folder", command=lambda: open_folder(DATA_DIR))
    help_menu.add_command(label="Automatic checks (Autopilot)...", command=lambda: launch_sibling("autopilot"))
    help_menu.add_command(label="Report a problem...", command=lambda: report_problem(root))
    for label, command in extras:
        help_menu.add_command(label=label, command=command)
    help_menu.add_separator()
    help_menu.add_command(label=f"About (version {app_version()})", command=about)
    menubar.add_cascade(label="Help", menu=help_menu)
    root.config(menu=menubar)


# --------------------------------------------------------------------------
# Help > Report a problem: one file a student can send to whoever helps them
# --------------------------------------------------------------------------
_KEEP_WORDS = {"missing", "late", "incomplete", "excused", "exception", "due", "assignment", "category", "grading", "period", "course", "grade"}


def scrub_text(text, keep_numbers=False):
    """Blank out names, titles and scores, but keep the few words that describe how a page is built."""
    def blank(match):
        word = match.group(0)
        return word if word.lower() in _KEEP_WORDS else "x" * len(word)
    text = re.sub(r"[^\W\d_]+", blank, text)
    return text if keep_numbers else re.sub(r"\d", "0", text)


def scrub_html(page, keep_numbers=False):
    """A copy of a saved page that still shows how it is built (tags, classes, ids) but holds no names or scores."""
    page = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", "", page)
    page = re.sub(r"(?is)<head\b.*?</head>", "<head></head>", page)
    page = re.sub(r"(?s)<!--.*?-->", "", page)
    keep = {"class", "id", "role", "type", "for", "colspan", "rowspan", "scope", "data-id", "data-parent-id", "aria-hidden", "hidden"}
    words = {"title", "aria-label", "alt", "value", "placeholder"}

    def attribute(m):
        name, value = m.group(1), m.group(3)
        if name.lower() in keep:
            return m.group(0)
        if name.lower() in words:
            return '%s="%s"' % (name, scrub_text(value, keep_numbers).replace('"', "'"))
        return '%s=""' % name

    page = re.sub(r"<[^<>]+>", lambda t: re.sub(r"([A-Za-z_:][-\w:.]*)=([\"'])(.*?)\2", attribute, t.group(0), flags=re.S), page)
    return re.sub(r">([^<>]+)<", lambda m: ">" + scrub_text(m.group(1), keep_numbers) + "<", page)


def _mask_private(text):
    text = re.sub(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", "<email>", text)
    return re.sub(r"(?i)(users|home)([\\/])[^\\/\s]+", r"\1\2USER", text)


def make_problem_report(include_grades=False):
    """Write SchoologyGrades-problem-<date>.zip to the Desktop (or the data folder). Returns (path, notes)."""
    import zipfile
    folder = os.path.join(os.path.expanduser("~"), "Desktop")
    if not (os.path.isdir(folder) and os.access(folder, os.W_OK)):
        folder = DATA_DIR
    path = os.path.join(folder, f"SchoologyGrades-problem-{datetime.now():%Y%m%d-%H%M}.zip")

    def read(name):
        try:
            with open(os.path.join(DATA_DIR, name), "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return None

    about = [f"Version: {app_version()}", f"System: {sys.platform}", f"Python: {sys.version.split()[0]}",
             f"Report made: {datetime.now():%Y-%m-%d %H:%M}", f"Grades included: {'yes' if include_grades else 'no'}"]
    try:
        for key, value in json.loads(read("config.json") or "{}").items():  # only the shape: never the email or password
            if key not in ("email", "password"):
                about.append(f"Setting {key}: " + (f"{len(value)} items" if isinstance(value, (list, dict)) else str(value) if isinstance(value, (bool, int)) else "set"))
    except ValueError:
        about.append("config.json could not be read")

    shape, layouts = [], {}
    try:
        layouts = (json.loads(read("layouts.json") or "{}") or {}).get("classes") or {}
        saved = json.loads(read("last_report.json") or "{}")
        for r in (saved.get("results") if isinstance(saved, dict) else saved) or []:
            cid = course_id_from_url(r.get("url"))
            shape.append({"layout": (layouts.get(str(cid)) or {}).get("layout"), "has_grade": isinstance(r.get("pct"), (int, float)),
                          "periods": len(r.get("periods") or []), "assignments": len(r.get("items") or []), "categories": len(r.get("cats") or [])})
    except Exception:
        shape = shape or []

    notes, pages = [], []
    pages_dir = os.path.join(DATA_DIR, "pages")
    if os.path.isdir(pages_dir):
        pages = sorted(n for n in os.listdir(pages_dir) if n.lower().endswith(".html"))[:12]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("about.txt", "\n".join(about) + "\n")
        z.writestr("what-the-program-read.json", json.dumps(shape, indent=1))
        for name in ("layouts.json", "fetch_mode.json"):
            if read(name) is not None:
                z.writestr(name, read(name))
        for name in ("timing.txt", "error_log.txt"):
            text = read(name)
            if text is not None:
                z.writestr(name, _mask_private("\n".join(text.splitlines()[-150:])))
        for name in pages:
            try:
                with open(os.path.join(pages_dir, name), "r", encoding="utf-8", errors="replace") as f:
                    z.writestr("pages/" + name, scrub_html(f.read(), keep_numbers=include_grades))
            except OSError:
                pass
    if not pages:
        notes.append("No class pages were saved yet. For more detail, open Setup, then Help > Save class pages for troubleshooting, and make this file again.")
    return path, notes


def report_problem(root):
    """The Help > Report a problem dialog."""
    import tkinter as tk
    from tkinter import messagebox, ttk

    win = tk.Toplevel(root)
    win.title("Report a problem")
    win.resizable(False, False)
    try:
        win.transient(root)
    except Exception:
        pass
    set_window_icon(win, "grades")
    frame = ttk.Frame(win, padding=18)
    frame.pack()
    ttk.Label(frame, text="Make a file to send when something isn't working", font=("", 11, "bold")).pack(anchor="w")
    ttk.Label(
        frame, wraplength=380, justify="left",
        text="It holds technical details: the program's version, which kind of page each class uses, and a copy of your class pages "
             "with names and scores blanked out. It never holds your password or email.",
    ).pack(anchor="w", pady=(8, 8))
    grades = tk.BooleanVar(value=False)
    ttk.Checkbutton(frame, variable=grades, text="Include my grades (only if a grade looks wrong)").pack(anchor="w")
    buttons = ttk.Frame(frame)
    buttons.pack(anchor="e", pady=(14, 0))

    def make():
        try:
            path, notes = make_problem_report(grades.get())
        except Exception as e:  # never let the helper itself crash
            log_error(f"report_problem: {e!r}")
            messagebox.showinfo("Report a problem", "Sorry, the file couldn't be made.", parent=win)
            return
        win.destroy()
        messagebox.showinfo(
            "Report a problem",
            f"Done. The file is {os.path.basename(path)}, in the folder that's opening now. Send it to whoever is helping you."
            + ("\n\n" + "\n".join(notes) if notes else ""),
            parent=root,
        )
        open_folder(os.path.dirname(path))

    ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right", padx=(8, 0))
    ttk.Button(buttons, text="Make the file", command=make).pack(side="right")
    _center_over(win, root)


# --------------------------------------------------------------------------
# Autopilot: scheduled quiet checks, change alerts and notifications
# --------------------------------------------------------------------------
AUTO_FILE = "auto.json"
AUTO_LOG = "auto_log.txt"
LOCK_FILE = ".check_running"
TASK_NAME = "SchoologyGradeChecker-Autopilot"
LAUNCH_LABEL = "local.schoologygrades.autopilot"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
AUTO_DEFAULTS = {
    "enabled": False,
    "days": [0, 1, 2, 3, 4],
    "times": ["15:30"],
    "notify": {"changes": True, "new_grades": True, "missing": True, "low": True, "low_below": 80},
}


def valid_time(text):
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(text)))


def format_time12(text):
    """'15:30' -> '3:30 PM'."""
    hour, minute = int(text[:2]), text[3:5]
    return f"{(hour + 11) % 12 + 1}:{minute} {'AM' if hour < 12 else 'PM'}"


def read_auto():
    """Autopilot's settings from auto.json, with anything missing or odd replaced by the defaults."""
    out = json.loads(json.dumps(AUTO_DEFAULTS))
    try:
        with open(AUTO_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        return out
    out["enabled"] = bool(data.get("enabled"))
    if isinstance(data.get("days"), list):
        out["days"] = sorted({d for d in data["days"] if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6})
    if isinstance(data.get("times"), list):
        out["times"] = sorted({t for t in data["times"] if valid_time(t)})[:4]
    notify_cfg = data.get("notify") if isinstance(data.get("notify"), dict) else {}
    for key in ("changes", "new_grades", "missing", "low"):
        if key in notify_cfg:
            out["notify"][key] = bool(notify_cfg[key])
    low = notify_cfg.get("low_below")
    if isinstance(low, (int, float)) and not isinstance(low, bool) and 0 < low <= 100:
        out["notify"]["low_below"] = low
    if isinstance(data.get("last_signin_notice"), (int, float)):
        out["last_signin_notice"] = float(data["last_signin_notice"])
    return out


def write_auto(settings):
    try:
        with open(AUTO_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=1)
        return True
    except OSError:
        return False


def next_run(settings, now=None):
    """When the next scheduled check is (a datetime), or None if nothing is scheduled."""
    from datetime import timedelta

    if not settings.get("days") or not settings.get("times"):
        return None
    now = now or datetime.now()
    for offset in range(0, 8):
        day = (now + timedelta(days=offset)).date()
        if day.weekday() not in settings["days"]:
            continue
        for t in sorted(settings["times"]):
            moment = datetime(day.year, day.month, day.day, int(t[:2]), int(t[3:5]))
            if moment > now:
                return moment
    return None


def auto_log(line):
    """One line in auto_log.txt (the newest 100 are kept)."""
    try:
        try:
            with open(AUTO_LOG, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            lines = []
        lines.append(f"{datetime.now():%Y-%m-%d %H:%M}  {line}")
        with open(AUTO_LOG, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[-100:]) + "\n")
    except OSError:
        pass


def last_auto_line():
    try:
        with open(AUTO_LOG, "r", encoding="utf-8") as f:
            lines = [x for x in f.read().splitlines() if x.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


# ---- one check at a time: the program's Chrome profile can't be shared ----------------------
LOCK_STALE_SECONDS = 15 * 60


def lock_busy():
    try:
        return time.time() - os.path.getmtime(LOCK_FILE) < LOCK_STALE_SECONDS
    except OSError:
        return False


def acquire_lock():
    if lock_busy():
        return False
    try:
        with open(LOCK_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def wait_for_idle(status, max_wait=120):
    """If a scheduled check is running, let it finish first (up to two minutes)."""
    waited = 0
    while lock_busy() and waited < max_wait:
        if waited == 0:
            status("A scheduled check is finishing...")
        time.sleep(2)
        waited += 2


# ---- what changed since last time ---------------------------------------------------------
def _num(n):
    return "%g" % round(n, 2)


def _item_key(item):
    return f"{item.get('c')}|{item.get('n')}|{item.get('d')}"


def build_alerts(previous, current, notify=None):
    """Plain-English lines about what changed between two checks (an empty list if nothing did).

    previous / current: lists of results (as saved in last_report.json). With no previous
    check there is nothing to compare, so the first run only sets the baseline."""
    cfg = dict(AUTO_DEFAULTS["notify"])
    cfg.update(notify or {})
    if not previous:
        return []
    before = {r.get("url") or r.get("name"): r for r in previous}
    lines = []
    for r in current:
        old = before.get(r.get("url") or r.get("name"))
        if old is None:
            continue
        parts = []
        now, was = r.get("pct"), old.get("pct")
        if isinstance(now, (int, float)) and isinstance(was, (int, float)):
            change = round(now - was, 2)
            if cfg["changes"] and abs(change) >= 0.05:
                sign = "+" if change > 0 else "\u2212"
                parts.append(f"{_num(was)}% \u2192 {_num(now)}% ({sign}{_num(abs(change))})")
            if cfg["low"] and now < cfg["low_below"] <= was:
                parts.append(f"dropped below {_num(cfg['low_below'])}%")
        if old.get("items"):  # an older saved report has no assignments to compare
            graded = lambda res: {_item_key(i) for i in res.get("items") or [] if isinstance(i.get("s"), (int, float))}
            fresh = len(graded(r) - graded(old))
            if cfg["new_grades"] and fresh:
                parts.append(f"{fresh} new grade{'s' if fresh != 1 else ''}")
            seen = {_item_key(i) for i in old.get("items") or [] if i.get("x")}
            newly = [i["n"] for i in r.get("items") or [] if i.get("x") and _item_key(i) not in seen]
            if cfg["missing"] and newly:
                parts.append("missing: " + ", ".join(newly[:2]) + (f" and {len(newly) - 2} more" if len(newly) > 2 else ""))
        if parts:
            lines.append(f"{r.get('name', 'A class')}: " + "; ".join(parts))
    return lines


def compose_notification(lines):
    """(title, message) for a notification about these lines."""
    shown = lines[:4]
    if len(lines) > 4:
        shown.append(f"and {len(lines) - 4} more")
    return ("Grade update" if len(lines) == 1 else f"Grade updates ({len(lines)} classes)"), "\n".join(shown)


_TOAST_SCRIPT = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
    "$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$n = $t.GetElementsByTagName('text');"
    "$n.Item(0).AppendChild($t.CreateTextNode($env:SG_TITLE)) | Out-Null;"
    "$n.Item(1).AppendChild($t.CreateTextNode($env:SG_BODY)) | Out-Null;"
    "$toast = [Windows.UI.Notifications.ToastNotification]::new($t);"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($toast)"
)


def _run(args, timeout=30, env=None):
    """subprocess.run with no console window. Returns the result, or None if it couldn't run."""
    kwargs = dict(stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, env=env or _clean_env())
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        return subprocess.run(args, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None


def notify(title, message):
    """Show a desktop notification. True if the system accepted it."""
    env = dict(_clean_env())
    env.update(SG_TITLE=title, SG_BODY=message)  # passed as variables, so no quoting problems
    if os.name == "nt":
        args = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-Command", _TOAST_SCRIPT]
    elif sys.platform == "darwin":
        args = ["osascript", "-e", 'display notification (system attribute "SG_BODY") with title (system attribute "SG_TITLE")']
    elif shutil.which("notify-send"):
        args = ["notify-send", title, message]
    else:
        return False
    result = _run(args, timeout=20, env=env)
    return bool(result) and result.returncode == 0


# ---- the schedule itself: Windows Task Scheduler / a macOS launch agent -----------------------
def auto_command():
    """The command that runs the grade checker quietly, or None if it can't be found."""
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            exe = os.path.join(APP_DIR, "SchoologyGrades.app", "Contents", "MacOS", "SchoologyGrades")
        else:
            exe = os.path.join(os.path.dirname(sys.executable), "SchoologyGrades" + (".exe" if os.name == "nt" else ""))
        return [exe, "--auto"] if os.path.isfile(exe) else None
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grades.pyw")
    return [sys.executable, script, "--auto"] if os.path.isfile(script) else None


def windows_task_xml(settings, command):
    """The Task Scheduler definition: runs at each chosen time on the chosen days, and catches up if the computer was off."""
    from xml.sax.saxutils import escape

    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    days = "".join(f"<{names[d]} />" for d in settings["days"])
    start = datetime.now().strftime("%Y-%m-%d")
    triggers = "".join(
        f"<CalendarTrigger><StartBoundary>{start}T{t}:00</StartBoundary><Enabled>true</Enabled>"
        f"<ScheduleByWeek><DaysOfWeek>{days}</DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek></CalendarTrigger>"
        for t in settings["times"]
    )
    args = " ".join('"%s"' % a if " " in a else a for a in command[1:])
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<RegistrationInfo><Description>Checks your Schoology grades quietly and tells you what changed.</Description></RegistrationInfo>"
        f"<Triggers>{triggers}</Triggers>"
        '<Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>'
        "<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>"
        "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable>"
        "<AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden>"
        "<ExecutionTimeLimit>PT15M</ExecutionTimeLimit></Settings>"
        f'<Actions Context="Author"><Exec><Command>{escape(command[0])}</Command><Arguments>{escape(args)}</Arguments></Exec></Actions></Task>'
    )


def launch_agent_plist(settings, command):
    """The macOS launch agent: runs at each chosen time on the chosen days (missed runs happen when the Mac wakes)."""
    import plistlib

    # launchd: 0 or 7 is Sunday, 1 is Monday ... ours: 0 is Monday ... 6 is Sunday
    when = [{"Weekday": (d + 1) % 7, "Hour": int(t[:2]), "Minute": int(t[3:5])} for d in settings["days"] for t in settings["times"]]
    return plistlib.dumps({"Label": LAUNCH_LABEL, "ProgramArguments": list(command), "StartCalendarInterval": when,
                           "RunAtLoad": False, "ProcessType": "Background"})


def _agent_path():
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", LAUNCH_LABEL + ".plist")


def install_schedule(settings):
    """Create (or replace) the scheduled job. Returns (ok, message)."""
    if not settings.get("days") or not settings.get("times"):
        return False, "Pick at least one day and one time."
    command = auto_command()
    if not command:
        return False, "Couldn't find Schoology Grades next to this program."
    if os.name == "nt":
        xml_path = os.path.join(tempfile.gettempdir(), "schoology-autopilot-task.xml")
        try:
            with open(xml_path, "w", encoding="utf-16") as f:
                f.write(windows_task_xml(settings, command))
            result = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"])
        finally:
            try:
                os.remove(xml_path)
            except OSError:
                pass
        if result is None or result.returncode != 0:
            return False, "Windows wouldn't set up the schedule" + (": " + result.stderr.decode("utf-8", "replace").strip() if result and result.stderr else ".")
        return True, "Scheduled."
    if sys.platform == "darwin":
        path = _agent_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(launch_agent_plist(settings, command))
        except OSError as e:
            return False, f"Couldn't save the schedule: {e}"
        domain = f"gui/{os.getuid()}"
        _run(["launchctl", "bootout", f"{domain}/{LAUNCH_LABEL}"])  # replace an older copy; fine if there isn't one
        result = _run(["launchctl", "bootstrap", domain, path])
        if result is None or result.returncode != 0:
            return False, "macOS wouldn't set up the schedule."
        return True, "Scheduled."
    return False, "Automatic checks work on Windows and Mac."


def remove_schedule():
    """Delete the scheduled job (fine if there isn't one). Returns (ok, message)."""
    if os.name == "nt":
        _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        return True, "Removed."
    if sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}"])
        try:
            os.remove(_agent_path())
        except OSError:
            pass
        return True, "Removed."
    return True, "Nothing to remove."


def schedule_installed():
    if os.name == "nt":
        result = _run(["schtasks", "/Query", "/TN", TASK_NAME])
        return bool(result) and result.returncode == 0
    return sys.platform == "darwin" and os.path.isfile(_agent_path())


def _save_page(driver, path):
    html_text = driver.execute_script("return '<!DOCTYPE html>\\n' + document.documentElement.outerHTML")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_text)
    try:
        driver.save_screenshot(os.path.splitext(path)[0] + ".png")
    except Exception:
        pass


def save_course_pages(config, status):
    """Sign in and save a copy of the dashboard and of each class page into data/pages.

    For troubleshooting: the copies show exactly what the program sees. They contain the
    student's name and grades, so they should only be shared on purpose. Returns the folder.
    """
    urls = list(dict.fromkeys(config["class_urls"]))
    host = urllib.parse.urlparse(urls[0]).netloc
    headless = config.get("headless", True)
    out = os.path.join(DATA_DIR, "pages")
    os.makedirs(out, exist_ok=True)
    status("Starting Chrome...")
    driver = make_driver(headless, config.get("profile_dir"))
    try:
        fetch_courses(driver, host, config["email"], config["password"], headless, status)  # signs in
        _save_page(driver, os.path.join(out, "dashboard.html"))
        for i, url in enumerate(urls, start=1):
            status(f"Saving class {i} of {len(urls)}...")
            driver.get(url)
            wait_until(lambda: driver.execute_script("return document.readyState") == "complete", 15)
            time.sleep(2)  # let the page finish drawing
            _save_page(driver, os.path.join(out, f"class_{i}_{course_id_from_url(url) or 'x'}.html"))
    finally:
        close_browser()
    return out


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def wait_until(condition, timeout, poll=0.2):
    """Call condition() until it returns something truthy or time runs out."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            result = condition()
        except Exception:
            result = None
        if result:
            return result
        time.sleep(poll)
    return None


def visible(driver, selector):
    """Displayed elements matching selector (never raises)."""
    try:
        return [e for e in driver.find_elements("css selector", selector) if e.is_displayed()]
    except Exception:
        return []


def has_login_form(driver):
    return bool(visible(driver, EMAIL_SEL) or visible(driver, PASS_SEL))


def on_host(driver, host):
    try:
        return urlparse(driver.current_url).netloc == host
    except Exception:
        return False


def log_error(text):
    try:
        with open("error_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}]\n{text}\n\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------
def kill_leftover_chrome(profile):
    """Stop Chrome processes from an earlier run that are still holding our profile.

    Only a Chrome whose command line contains this program's exact profile
    folder is stopped. Your normal Chrome (its tabs, windows, other profiles)
    can never match, and nothing else is touched.
    """
    if os.name != "nt":
        # macOS / Linux: find Chrome processes started with our profile folder.
        try:
            out = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdout=subprocess.PIPE, text=True, timeout=10,
            ).stdout
            for line in out.splitlines():
                pid, _, command = line.strip().partition(" ")
                if pid.isdigit() and int(pid) != os.getpid() and profile in command:
                    try:
                        os.kill(int(pid), 9)
                    except Exception:
                        pass
        except Exception:
            pass
        return
    path = profile.replace("'", "''")  # escape for a PowerShell single-quoted string
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
        f"Where-Object {{ $_.CommandLine -and $_.CommandLine.IndexOf('{path}', "
        "[StringComparison]::OrdinalIgnoreCase) -ge 0 } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        # Explicit DEVNULL handles: a windowed (.pyw / --noconsole) app has no
        # valid stdin/stdout, which otherwise makes subprocess fail on Windows.
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=20,
        )
    except Exception:
        pass


DRIVER_PATH_FILE = "chromedriver_path.txt"

# Chrome switches that skip work we never need (updates, sync, extensions...) and
# stop Chrome from slowing down tabs that aren't in front.
FAST_START_FLAGS = (
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-component-update",
    "--metrics-recording-only",
    "--mute-audio",
    "--disable-breakpad",
    "--disable-popup-blocking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    # Fewer browser processes per tab = faster to open many tabs.
    "--disable-site-isolation-trials",
    "--disable-features=Translate,MediaRouter,OptimizationHints,IsolateOrigins,site-per-process",
)


class Timeline:
    """Records how long each phase of a run takes, saved to data/timing.txt."""

    def __init__(self):
        self.t0 = time.time()
        self.rows = []

    def mark(self, label):
        self.rows.append((time.time() - self.t0, label))

    def save(self, path="timing.txt"):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"Run started {datetime.fromtimestamp(self.t0):%Y-%m-%d %H:%M:%S}\n")
                for seconds, label in self.rows:
                    f.write(f"{seconds:6.2f}s  {label}\n")
        except Exception:
            pass


_active_driver = [None]


def close_browser():
    """Quit the browser we started, if any. Safe to call any time, any number of times."""
    driver, _active_driver[0] = _active_driver[0], None
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass


atexit.register(close_browser)


def _read_cached_driver():
    try:
        with open(DRIVER_PATH_FILE, "r", encoding="utf-8") as f:
            path = f.read().strip()
        return path if path and os.path.isfile(path) else None
    except Exception:
        return None


def _remember_driver(driver):
    """Save where chromedriver lives so next time Selenium Manager is skipped."""
    try:
        path = driver.service.path
        if path and os.path.isfile(path):
            with open(DRIVER_PATH_FILE, "w", encoding="utf-8") as f:
                f.write(path)
    except Exception:
        pass


def _forget_driver():
    try:
        os.remove(DRIVER_PATH_FILE)
    except Exception:
        pass


def make_driver(headless, profile_dir=None):
    webdriver, Service, _ = _selenium()

    # Saved login lives in the data folder unless config.json sets "profile_dir".
    profile = os.path.abspath(profile_dir or os.path.join(os.getcwd(), "selenium_profile"))

    # Clear stale lock files left by a crashed Chrome (Linux/Mac only).
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            os.remove(os.path.join(profile, name))
        except Exception:
            pass

    if os.name != "nt":
        kill_leftover_chrome(profile)  # instant here; on Windows it only runs after a failed start

    options = webdriver.ChromeOptions()
    options.add_argument(f"--user-data-dir={profile}")
    options.add_argument("--window-size=1400,1000")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    if headless:
        options.add_argument("--headless=new")
    for flag in FAST_START_FLAGS:
        options.add_argument(flag)
    # Speed: return at DOMContentLoaded instead of waiting for every resource,
    # and skip downloading images (we only read text).
    options.page_load_strategy = "eager"
    options.add_experimental_option(
        "prefs",
        {
            "profile.managed_default_content_settings.images": 2,
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        },
    )
    options.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])

    def start(driver_path):
        # With a known driver path, Selenium Manager (an extra program launch plus
        # version checks) is skipped entirely.
        service = Service(executable_path=driver_path) if driver_path else Service()
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            service.creation_flags = subprocess.CREATE_NO_WINDOW  # no flashing console
        driver = webdriver.Chrome(service=service, options=options)
        _active_driver[0] = driver
        _remember_driver(driver)
        return driver

    cached = _read_cached_driver()
    if cached:
        try:
            return start(cached)
        except Exception:
            # Chrome probably updated and the old driver no longer matches.
            _forget_driver()

    try:
        return start(None)
    except Exception:
        # Most often a Chrome from an earlier run is still holding the profile.
        kill_leftover_chrome(profile)
        time.sleep(2)
        try:
            return start(None)
        except Exception as e:
            raise RuntimeError(
                "Chrome would not start with the saved profile:\n"
                f"{profile}\n\n"
                "If this folder is synced by OneDrive, OneDrive may be locking its "
                "files. Try pausing sync, deleting the 'selenium_profile' folder "
                "(you'll log in again), or add a \"profile_dir\" entry to config.json "
                "pointing to a folder outside OneDrive.\n\n"
                f"Details: {str(e).splitlines()[0] if str(e) else e.__class__.__name__}"
            ) from e


def log_in(driver, email, password, host, timeout, status):
    _, _, Keys = _selenium()
    status("Logging in...")
    email_fields = visible(driver, EMAIL_SEL)
    pass_fields = visible(driver, PASS_SEL)

    if email_fields:
        box = email_fields[0]
        box.clear()
        box.send_keys(email)
        if not pass_fields:  # two-step login: submit email first
            box.send_keys(Keys.RETURN)

    pw = wait_until(lambda: visible(driver, PASS_SEL), 20)
    if not pw:
        raise RuntimeError(
            "The password box never appeared.\n\n"
            f"Browser was at: {driver.current_url}\n"
            "Check your internet connection and email address."
        )
    time.sleep(0.5)
    pw[0].clear()
    pw[0].send_keys(password)
    time.sleep(0.5)
    pw[0].send_keys(Keys.RETURN)

    def logged_in():
        if on_host(driver, host) and not has_login_form(driver):
            return True
        for button in visible(driver, STAY_SIGNED_IN_SEL):  # "Stay signed in?"
            try:
                button.click()
            except Exception:
                pass
        return False

    if not wait_until(logged_in, timeout, poll=1):
        raise RuntimeError(
            "Login did not complete.\n\n"
            "Check the email and password in config.json. If your school uses "
            "multi-factor approval, set \"headless\": false in config.json and "
            "approve the sign-in in the browser window that opens."
        )


# --------------------------------------------------------------------------
# Finding the student's classes on the Course Dashboard
# --------------------------------------------------------------------------
DEFAULT_HOST = "lyncs.schoology.com"
DASHBOARD_PATHS = ("/home/course-dashboard", "/home")
CARD_SEL = "li.sgy-grid-layout__item[data-key]"

# Each course card is an <li data-key=".$<course id>"> holding a section whose
# aria-label reads "Navigate to course: <Course>: <Section>.   Instructors: ...".
GRAB_CARDS_JS = """
return Array.from(document.querySelectorAll(arguments[0])).map(function (li) {
  var s = li.querySelector('section[aria-label]');
  return [li.getAttribute('data-key'), s ? s.getAttribute('aria-label') : (li.innerText || '')];
});
"""


def read_config():
    """config.json as a dict, or {} if it is missing or unreadable."""
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def normalize_host(text):
    """'https://lyncs.schoology.com/home' -> 'lyncs.schoology.com'"""
    text = (text or "").strip()
    if "//" not in text:
        text = "//" + text
    return urlparse(text).netloc.strip().lower()


def grade_url(host, course_id):
    return f"https://{host}/course/{course_id}/student_grades"


def course_id_from_url(url):
    match = re.search(r"/course/(\d+)", url or "")
    return match.group(1) if match else None


def parse_card(key, label):
    """Turn a card's data-key and aria-label into {"id", "name", "section"}."""
    match = re.search(r"\d+", key or "")
    if not match:
        return None
    text = re.sub(r"^\s*Navigate to course:\s*", "", label or "")
    text = re.split(r"\.\s+Instructors?:", text)[0].strip()  # drop the instructor list
    name, _, section = text.partition(":")
    return {
        "id": match.group(),
        "name": name.strip() or f"Course {match.group()}",
        "section": section.strip().rstrip("."),
    }


def read_course_cards(driver):
    """Courses currently shown on the page, in dashboard order (never raises)."""
    try:
        rows = driver.execute_script(GRAB_CARDS_JS, CARD_SEL) or []
    except Exception:
        return []
    courses, seen = [], set()
    for key, label in rows:
        course = parse_card(key, label)
        if course and course["id"] not in seen:
            seen.add(course["id"])
            courses.append(course)
    return courses


def fetch_courses(driver, host, email, password, headless, status):
    """Sign in if needed, open the Course Dashboard, and return its courses."""

    def state():
        if has_login_form(driver):
            return "login"
        if read_course_cards(driver):
            return "cards"
        return None

    logged_in = False
    for path in DASHBOARD_PATHS:
        status("Opening your course list...")
        driver.get(f"https://{host}{path}")
        found = wait_until(state, 20)
        if found == "login" and not logged_in:
            log_in(driver, email, password, host, 30 if headless else 180, status)
            logged_in = True
            status("Opening your course list...")
            driver.get(f"https://{host}{path}")
            found = wait_until(state, 20)
        if found == "cards":
            time.sleep(1)  # let the whole grid finish drawing
            return read_course_cards(driver)

    raise RuntimeError(
        "Couldn't find your course list.\n\n"
        f"Browser was at: {driver.current_url}\n"
        "Check your internet connection and the Schoology address, and try again."
    )
