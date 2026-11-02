"""Shared code for Schoology Grade Checker and Schoology Setup.

Importing this module moves the program into its "data" folder. It provides the
browser start-up, login and course-list helpers that both programs use.
Selenium itself is imported on first use, so program windows open instantly.
"""

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
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
GRADE_SEL = ".course-grade-value .rounded-grade, .awarded-grade .rounded-grade"
GRADE_FALLBACK_SEL = ".rounded-grade"


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
# "A newer version is available" notice
# --------------------------------------------------------------------------
UPDATE_BASE = "https://github.com"
UPDATE_CHECK_HOURS = 12  # how long to remember the answer, so GitHub is asked rarely


def read_repo():
    """The GitHub repository releases are published to, as "owner/name", or "".

    It comes from a small file named REPO that the GitHub build adds automatically.
    Without that file (a local build) the update notice is simply switched off.
    """
    try:
        with open(resource_path("REPO"), "r", encoding="utf-8") as f:
            repo = f.read().strip()
    except Exception:
        return ""
    return repo if re.fullmatch(r"[\w.\-]+/[\w.\-]+", repo) else ""


def _version_key(text):
    """'v1.10.2' -> (1, 10, 2), so that 1.10 counts as newer than 1.9."""
    return tuple(int(n) for n in re.findall(r"\d+", text or ""))[:4]


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


def check_for_update():
    """{"version", "url"} if a newer release exists, otherwise None. Never raises, never slow."""
    try:
        repo = read_repo()
        current = _version_key(app_version())
        if not repo or not current:
            return None
        latest = None
        try:
            with open("update_check.json", "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("repo") == repo and time.time() - float(cache.get("checked", 0)) < UPDATE_CHECK_HOURS * 3600:
                latest = str(cache.get("latest", ""))
        except Exception:
            pass
        if latest is None:
            latest = _latest_release_tag(repo)
            if latest is None:
                return None  # couldn't tell just now; try again next time
            try:
                with open("update_check.json", "w", encoding="utf-8") as f:
                    json.dump({"repo": repo, "checked": time.time(), "latest": latest}, f)
            except OSError:
                pass
        if latest and _version_key(latest) > current:
            return {"version": latest.lstrip("vV"), "url": f"{UPDATE_BASE}/{repo}/releases/latest"}
    except Exception:
        pass
    return None


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
    """Start the other program ("grades" or "setup") from the same place. True if it started."""
    programs = {"grades": "SchoologyGrades", "setup": "SchoologySetup"}
    scripts = {"grades": "grades.pyw", "setup": "setup.py"}
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

    actions: any of "try_again", "open_setup", "download_chrome".
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
            "The sign-in didn't work",
            "Your email or password may be wrong, or your school wants you to approve the sign-in. "
            "Open Setup to re-enter them (tick \"Show the browser\" if you need to approve it).",
            ("open_setup", "try_again"), "bad_login",
        )
    if has("asked for a login again"):
        return make(
            "Schoology signed you out",
            "Schoology asked you to sign in again in the middle of checking. Try again.",
            ("try_again",), "signed_out",
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
    """A small friendly dialog. Returns "try_again", "open_setup" or "close"."""
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
        choice[0] = action if action in ("try_again", "open_setup") else "close"
        win.destroy()

    labels = {"try_again": "Try again", "open_setup": "Open Setup", "download_chrome": "Download Chrome"}
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
