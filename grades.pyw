"""Schoology Grade Checker

Logs in to Schoology (reusing a saved Chrome profile after the first run),
reads the overall grade for each course in data/config.json, and opens an HTML
report. Run Schoology Setup first to choose your classes.

Optional config.json key: "headless": false  -> shows the browser window so you
can approve a Microsoft MFA prompt by hand the first time.
"""

import base64
import hashlib
import hmac
import html
import json
import math
import os
import re
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import urlparse

# Shared code. Importing it also checks that Selenium is installed and moves the
# program into its "data" folder, so keep it above anything that reads files.
from sgcommon import (
    DATA_DIR,
    EMAIL_SEL,
    GRADE_FALLBACK_SEL,
    GRADE_SEL,
    PASS_SEL,
    TITLE_SEL,
    TRANSLOCATED,
    FriendlyError,
    Timeline,
    app_version,
    check_for_update,
    close_browser,
    explain,
    launch_sibling,
    log_error,
    log_in,
    make_driver,
    open_app_window,
    resource_path,
    set_window_icon,
    show_error_dialog,
    wait_until,
)

# The special grade scale is switched on by a salted, deliberately slow hash of one
# email address, so the address itself isn't stored anywhere in the program.
_SCALE_SALT = bytes.fromhex("795f56f64791ad6f6807e63c8c8a1bc0")
_SCALE_HASH = "1431344426edfc0d7e50134a22652548084a47c462ebfcc2530c83c7d0182619"


def wants_custom_scale(email):
    digest = hashlib.pbkdf2_hmac("sha256", (email or "").strip().lower().encode("utf-8"), _SCALE_SALT, 200_000)
    return hmac.compare_digest(digest.hex(), _SCALE_HASH)


# --------------------------------------------------------------------------
# Grade scales
# --------------------------------------------------------------------------
def get_official_grade(pct):
    if pct >= 95:
        return "A"
    elif pct >= 90:
        return "A-"
    elif pct >= 87:
        return "B+"
    elif pct >= 83:
        return "B"
    elif pct >= 80:
        return "B-"
    elif pct >= 77:
        return "C+"
    elif pct >= 73:
        return "C"
    elif pct >= 70:
        return "C-"
    elif pct >= 67:
        return "D+"
    elif pct >= 63:
        return "D"
    elif pct >= 60:
        return "D-"
    return "F"


def get_custom_grade(pct):
    if pct >= 100.1:
        return "A+"
    elif pct >= 99.0:
        return "A"
    elif pct >= 98.0:
        return "B-"
    elif pct >= 97.0:
        return "D+"
    return "F"


def load_config():
    if not os.path.exists("config.json"):
        if TRANSLOCATED:
            raise FriendlyError(
                "macOS is hiding your saved settings",
                "macOS is running this app from a temporary, protected copy. In Finder, drag the whole "
                "SchoologyGrades folder to another place (for example Documents) and open the app from there.",
                (), f"config.json not found. Looked in: {DATA_DIR}", kind="translocated",
            )
        raise FriendlyError(
            "You haven't picked your classes yet",
            "Open Setup to choose which classes to check.",
            ("open_setup",), f"config.json not found. Looked in: {DATA_DIR}", kind="needs_setup",
        )
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        raise FriendlyError(
            "Your saved settings can't be read",
            "Open Setup and pick your classes again.",
            ("open_setup",), f"config.json: {e}", kind="bad_config",
        )
    for key in ("email", "password", "class_urls"):
        if not config.get(key):
            raise FriendlyError(
                "Your saved settings are incomplete",
                "Open Setup and pick your classes again.",
                ("open_setup",), f"config.json is missing '{key}'", kind="bad_config",
            )
    return config


# --------------------------------------------------------------------------
SETTLE_SECONDS = 2.5  # after a page finishes loading, how long to wait for a grade before saying N/A


def round_half_up(x):
    """School-style rounding: 98.5 -> 99 (Python's round() would give 98)."""
    return int(math.floor(x + 0.5))


# One call into the browser that answers everything we need to know about the
# page, instead of half a dozen separate round trips per check.
PROBE_JS = """
var vis = function (e) { return !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length); };
var first = function (sel) {
  var list = document.querySelectorAll(sel);
  for (var i = 0; i < list.length; i++) { if (vis(list[i])) return list[i]; }
  return null;
};
var badge = function (e) { return e ? [e.getAttribute('title') || '', e.innerText || e.textContent || ''] : null; };
var heading = document.querySelector(arguments[2]);
return {
  host: location.host,
  path: location.pathname,
  ready: document.readyState,
  login: !!(first(arguments[3]) || first(arguments[4])),
  title: heading ? (heading.innerText || heading.textContent || '') : null,
  grade: badge(first(arguments[0])),
  fallback: badge(first(arguments[1]))
};
"""


def probe(driver):
    """What the current tab shows right now, or None if it's mid-navigation."""
    try:
        return driver.execute_script(
            PROBE_JS, GRADE_SEL, GRADE_FALLBACK_SEL, TITLE_SEL, EMAIL_SEL, PASS_SEL
        )
    except Exception:
        return None


def badge_pct(badge):
    """Percent from a grade badge's [title, text], or None."""
    for raw in badge or ():
        match = re.search(r"\d+(?:\.\d+)?", raw or "")
        if match:
            return float(match.group())
    return None


def course_result(driver, idx, title, badge, use_custom):
    name = (title or "").split(":")[0].strip() or f"Course {idx}"
    pct = badge_pct(badge)
    if pct is None:
        # Save a screenshot so you can see what the page looked like.
        try:
            if driver is not None:
                os.makedirs("debug", exist_ok=True)
                driver.save_screenshot(os.path.join("debug", f"course_{idx}.png"))
        except Exception:
            pass
        return {"name": name, "pct": "N/A", "official": "N/A", "custom": "N/A"}
    rounded = round_half_up(pct)  # letters use the whole-percent value...
    return {
        "name": name,
        "pct": round(pct, 2),  # ...but the percent shown stays unrounded
        "official": get_official_grade(rounded),
        "custom": get_custom_grade(rounded) if use_custom else "",
    }


SESSION_MARKER = ".session_ok"  # exists after a run that finished while already signed in


class LoginNeeded(RuntimeError):
    """A course tab showed the sign-in page."""


def course_id(path):
    match = re.search(r"/course/(\d+)", path or "")
    return match.group(1) if match else None


def read_courses(driver, handles, want, host, use_custom, on_result, max_wait=25, blank_wait=15):
    """Watch several tabs until every wanted course has its grade.

    handles: window handles to watch. want: {course id: course number} still
    needed. Each tab is matched to its course by the address it shows, so it
    doesn't matter which tab is which. All tabs share one loop, so a course with
    no grade yet only costs one short wait for everyone, not one per course.
    Tabs still blank after blank_wait seconds are given up on early (the caller
    reloads them). Returns ({course number: result}, [course numbers not loaded]).
    """
    results = {}
    want = dict(want)
    watching = list(handles)
    first_title, settled, offsite = {}, {}, {}
    start = time.time()
    end = start + max_wait

    while want and time.time() < end:
        for handle in list(watching):
            driver.switch_to.window(handle)
            state = probe(driver)
            if not state:
                continue
            if state.get("login"):
                raise LoginNeeded("Schoology asked for a login again partway through. Try running again.")
            if state.get("host") != host:
                if state.get("host"):  # sent to another site (a sign-in service) and staying there
                    offsite.setdefault(handle, time.time())
                    if time.time() - offsite[handle] > 3:
                        raise LoginNeeded("Schoology asked for a login again partway through. Try running again.")
                continue
            offsite.pop(handle, None)
            cid = course_id(state.get("path"))
            idx = want.get(cid)
            if idx is None:
                continue

            badge, title, finished = state.get("grade"), state.get("title"), False
            if badge is not None:
                finished = True
            elif title is not None:
                now = time.time()
                first_title.setdefault(idx, now)
                if state.get("ready") == "complete" or now - first_title[idx] > 6:
                    settled.setdefault(idx, now)
                if idx in settled and now - settled[idx] >= SETTLE_SECONDS:
                    badge = state.get("fallback")  # last resort: first grade badge on the page
                    finished = True

            if finished:
                results[idx] = course_result(driver, idx, title, badge, use_custom)
                del want[cid]
                watching.remove(handle)
                on_result(idx)
        if want:
            if time.time() - start > blank_wait and not any(i in first_title for i in want.values()):
                break  # nothing left has even started to render
            time.sleep(0.15)
    return results, sorted(want.values())


def open_courses(driver, urls, navigate_main):
    """Open every course in its own tab with a single call into the browser.

    With navigate_main the current tab loads the first course as well; otherwise
    it is assumed to be on the first course already. Returns all window handles.
    """
    driver.execute_script(
        "var urls = arguments[0];"
        "for (var i = 1; i < urls.length; i++) { window.open(urls[i], '_blank', 'noopener'); }"
        "if (arguments[1]) { window.location.href = urls[0]; }",
        list(urls),
        bool(navigate_main),
    )
    wait_until(lambda: len(driver.window_handles) >= len(urls), 1.5)
    handles = driver.window_handles
    # If the browser refused some pop-up tabs, open the rest one by one.
    for url in urls[len(handles):]:
        driver.switch_to.new_window("tab")
        driver.execute_script("window.location.href = arguments[0];", url)
    return driver.window_handles


def fetch_all(driver, urls, host, use_custom, note, navigate_main, timeline):
    """Load every course in parallel tabs and read each grade. Returns results in order."""
    want = {course_id(u): i for i, u in enumerate(urls, start=1)}
    handles = open_courses(driver, urls, navigate_main)
    timeline.mark("All course tabs opened")

    results, stuck = read_courses(driver, handles, want, host, use_custom, note)

    if stuck:  # tabs that never rendered (e.g. throttled): load each directly and look again
        timeline.mark(f"{len(stuck)} tab(s) never loaded, reloading them")
        fresh = []
        for idx in stuck:
            driver.switch_to.new_window("tab")
            driver.get(urls[idx - 1])
            fresh.append(driver.current_window_handle)
        retry_want = {course_id(urls[i - 1]): i for i in stuck}
        more, stuck = read_courses(driver, fresh, retry_want, host, use_custom, note, max_wait=20)
        results.update(more)
        for idx in stuck:  # still nothing: record N/A (with a screenshot)
            results[idx] = course_result(driver, idx, "", None, use_custom)

    return [results[i] for i in range(1, len(urls) + 1)]


def reset_tabs(driver):
    """Close every tab except the first, and go back to it."""
    handles = driver.window_handles
    for handle in handles[1:]:
        try:
            driver.switch_to.window(handle)
            driver.close()
        except Exception:
            pass
    driver.switch_to.window(handles[0])


# --------------------------------------------------------------------------
# Shortcut: read the grades straight from each course page's HTML
#
# Opening 8 real pages means waiting for Schoology to draw all 8. If the grades
# are already in the page's HTML, one tab can request all 8 pages directly and
# skip the drawing. It is only trusted after a normal run has shown it gives
# exactly the same grades, and it is re-checked every VERIFY_EVERY runs.
# --------------------------------------------------------------------------
FETCH_STATE_FILE = "fetch_mode.json"
VERIFY_EVERY = 20

FETCH_JS = """
var done = arguments[arguments.length - 1];
var urls = arguments[0], gradeSel = arguments[1], fallbackSel = arguments[2], titleSel = arguments[3];
var badge = function (e) { return e ? [e.getAttribute('title') || '', e.textContent || ''] : null; };
Promise.all(urls.map(function (u) {
  return fetch(u, {credentials: 'same-origin', cache: 'no-store'}).then(function (r) {
    return r.text().then(function (t) {
      var doc = new DOMParser().parseFromString(t, 'text/html');
      var h = doc.querySelector(titleSel);
      return {
        url: r.url, ok: r.ok,
        login: !!doc.querySelector("input[type='password']"),
        title: h ? h.textContent : null,
        grade: badge(doc.querySelector(gradeSel)),
        fallback: badge(doc.querySelector(fallbackSel))
      };
    });
  }).catch(function (e) { return {url: u, error: String(e)}; });
})).then(done);
"""


def load_fetch_state():
    """('unknown' | 'ok' | 'no', count). 'ok' = verified; count = runs since the last check."""
    try:
        with open(FETCH_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("mode", "unknown")), int(data.get("count", 0))
    except Exception:
        return "unknown", 0


def save_fetch_state(mode, count):
    try:
        with open(FETCH_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"mode": mode, "count": count}, f)
    except OSError:
        pass


def fetch_course_pages(driver, urls, host):
    """Ask for every course page from one tab. Returns a list of dicts, or None on failure."""
    try:
        driver.set_script_timeout(25)
        driver.get(f"https://{host}/robots.txt")  # a tiny page on the same site; just gives us the right origin
        items = driver.execute_async_script(FETCH_JS, list(urls), GRADE_SEL, GRADE_FALLBACK_SEL, TITLE_SEL)
        return items if isinstance(items, list) and len(items) == len(urls) else None
    except Exception:
        return None


def pages_usable(items, urls):
    """True if every course page came back as the real, signed-in course page."""
    if not items:
        return False
    for item, url in zip(items, urls):
        if not item or item.get("error") or not item.get("ok") or item.get("login"):
            return False
        if item.get("title") is None:
            return False
        if course_id(urlparse(item.get("url") or "").path) != course_id(url):
            return False  # redirected somewhere else (for example a sign-in page)
    return True


def results_from_pages(items, use_custom):
    results = []
    for idx, item in enumerate(items, start=1):
        badge = item.get("grade") if item.get("grade") is not None else item.get("fallback")
        results.append(course_result(None, idx, item.get("title") or "", badge, use_custom))
    return results


def same_results(a, b):
    def clean(r):
        return (" ".join(str(r["name"]).split()), r["pct"])
    return len(a) == len(b) and all(clean(x) == clean(y) for x, y in zip(a, b))


def scrape_all(config, status):
    # Collapse accidental duplicates, keeping the order.
    urls = list(dict.fromkeys(config["class_urls"]))
    if any(course_id(u) is None for u in urls):
        raise RuntimeError("config.json has a class link without a course number. Run SchoologySetup again.")
    host = urlparse(urls[0]).netloc
    headless = config.get("headless", True)
    use_custom = wants_custom_scale(config["email"])

    timeline = Timeline()
    done = [0]

    def note(idx):
        done[0] += 1
        status(f"Reading grades ({done[0]} of {len(urls)})...")
        timeline.mark(f"Grade {done[0]} of {len(urls)} read (course {idx})")

    status("Starting Chrome...")
    driver = make_driver(headless, config.get("profile_dir"))
    timeline.mark("Chrome started")
    try:
        results = None

        # Fast path: if the last run finished while already signed in, skip the
        # separate "am I signed in?" step and go straight to the courses.
        if os.path.exists(SESSION_MARKER):
            status("Loading all courses...")
            mode, count = load_fetch_state()
            pages, pages_ok, html_has_grades = None, False, False
            if mode != "no":
                pages = fetch_course_pages(driver, urls, host)
                pages_ok = pages_usable(pages, urls)
                html_has_grades = pages_ok and any(p.get("grade") is not None for p in pages)
                timeline.mark(
                    f"Course pages requested directly (usable: {pages_ok}, grades in HTML: {html_has_grades})"
                )
            verify_due = mode == "unknown" or (mode == "ok" and count >= VERIFY_EVERY)

            if mode == "ok" and html_has_grades and not verify_due:
                results = results_from_pages(pages, use_custom)  # no tabs needed at all
                save_fetch_state("ok", count + 1)
                for idx in range(1, len(urls) + 1):
                    note(idx)
            else:
                try:
                    results = fetch_all(driver, urls, host, use_custom, note, True, timeline)
                except LoginNeeded:
                    results = None
                    try:
                        os.remove(SESSION_MARKER)
                    except OSError:
                        pass
                    done[0] = 0
                    reset_tabs(driver)
                    timeline.mark("Saved sign-in expired, signing in again")

                # Compare the shortcut with what the real pages just showed.
                if results is not None and mode != "no" and pages_ok:
                    if not any(isinstance(r["pct"], (int, float)) for r in results):
                        # No grades visible anywhere (for example hidden at quarter end):
                        # that proves nothing either way, so leave the shortcut as it is.
                        timeline.mark("No grades visible right now; shortcut check skipped")
                    elif html_has_grades and same_results(results, results_from_pages(pages, use_custom)):
                        save_fetch_state("ok", 1)
                        timeline.mark("Shortcut verified: same grades as the real pages")
                    elif mode == "unknown":
                        save_fetch_state("no", 0)  # grades aren't in the HTML, or they differ
                        timeline.mark("Shortcut not usable here; turned off")
                    else:
                        save_fetch_state("no", 0)  # it was fine before but no longer matches
                        timeline.mark("Shortcut no longer matches; turned off")
                elif results is not None and mode == "unknown" and not pages_ok:
                    save_fetch_state("unknown" if count + 1 < 3 else "no", count + 1)

        if results is None:
            status("Opening Schoology...")
            driver.get(urls[0])

            def ready():
                state = probe(driver)
                if not state:
                    return None
                if state.get("login"):
                    return "login"
                if state.get("host") == host and state.get("title") is not None:
                    return "ready"
                return None

            if wait_until(ready, 15) != "ready":
                log_in(driver, config["email"], config["password"], host, 30 if headless else 180, status)
                timeline.mark("Signed in")

            # Make sure the first tab is on the first course (login may land elsewhere).
            here, want = urlparse(driver.current_url), urlparse(urls[0])
            navigate_main = (here.netloc, here.path.rstrip("/")) != (want.netloc, want.path.rstrip("/"))
            timeline.mark("Schoology ready")
            status("Loading all courses...")
            results = fetch_all(driver, urls, host, use_custom, note, navigate_main, timeline)

        try:
            with open(SESSION_MARKER, "w", encoding="utf-8") as f:
                f.write("ok")
        except OSError:
            pass
        return results, use_custom
    except LoginNeeded:
        try:
            os.remove(SESSION_MARKER)
        except OSError:
            pass
        raise
    finally:
        timeline.mark("Finished")
        timeline.save()
        try:
            driver.quit()
        except Exception:
            pass


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
# Colours for dark mode, used twice below: when the person picks Dark, and when
# they leave it on Auto and their system is set to dark.
DARK_VARS = """
  --bg: #0e1014; --card: #1a1d24; --text: #f2f3f7; --muted: #9ba0ad; --line: #2a2e38;
  --head: #222630; --head-text: #aeb4c2; --official: #c3c8d4; --custom: #7fb0ff;
  --shadow: 0 6px 28px rgba(0,0,0,.45); --btn: #262a34; --btn-hover: #323744;
  --upd-bg: #2a2f55; --upd-fg: #c3caff;
"""

REPORT_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Grade Report</title>
<link rel="icon" href="data:image/png;base64,__FAVICON__">
<script>
  /* apply a saved Light/Dark choice before anything is drawn, so there is no flash */
  try { var t = localStorage.getItem("sgTheme"); if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t); } catch (e) {}
</script>
<style>
:root {
  --bg: #f3f4f8; --card: #ffffff; --text: #1d1d1f; --muted: #6b7280; --line: #e8eaf0;
  --head: #f1f3f9; --head-text: #4b5563; --official: #4b5563; --custom: #2563eb;
  --shadow: 0 6px 28px rgba(15,23,42,.09); --btn: #eef0f6; --btn-hover: #e2e5ef;
  --upd-bg: #eef2ff; --upd-fg: #3730a3;
}
:root[data-theme="dark"] {__DARK__}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {__DARK__}
}
* { box-sizing: border-box; }
body { margin: 0; padding: 18px; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
.card { max-width: 780px; margin: 0 auto; background: var(--card); padding: 22px 24px 18px;
  border-radius: 18px; box-shadow: var(--shadow); }
.top { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h2 { margin: 0; font-size: 22px; letter-spacing: -.01em; }
.sub { margin: 6px 0 0; color: var(--muted); font-size: 13px; }
#theme { flex: none; width: 36px; height: 36px; border: 0; border-radius: 50%; cursor: pointer;
  background: var(--btn); color: var(--text); display: grid; place-items: center; padding: 0; }
#theme:hover { background: var(--btn-hover); }
#theme:focus-visible { outline: 2px solid var(--custom); outline-offset: 2px; }
#theme svg { width: 20px; height: 20px; display: none; }
#theme[data-mode="auto"] .i-auto, #theme[data-mode="light"] .i-sun, #theme[data-mode="dark"] .i-moon { display: block; }
table { width: 100%; border-collapse: collapse; margin-top: 16px; }
th { background: var(--head); color: var(--head-text); padding: 11px 14px; font-size: 12.5px; font-weight: 600;
  text-align: center; border-bottom: 1px solid var(--line); white-space: nowrap; }
th:first-child { text-align: left; border-radius: 10px 0 0 10px; }
th:last-child { border-radius: 0 10px 10px 0; }
td { padding: 13px 14px; border-bottom: 1px solid var(--line); text-align: center; font-variant-numeric: tabular-nums; }
td:first-child { text-align: left; font-weight: 500; }
tr:last-child td { border-bottom: 0; }
td.official { font-weight: 700; color: var(--official); }
td.custom { font-weight: 700; color: var(--custom); }
.update { display: block; margin: 14px 0 0; padding: 10px 14px; border-radius: 12px; text-align: center;
  background: var(--upd-bg); color: var(--upd-fg); font-size: 13px; font-weight: 600; text-decoration: none; }
.update:hover { filter: brightness(1.06); }
.foot { margin: 14px 0 0; color: var(--muted); font-size: 11.5px; text-align: center; }
</style>
</head>
<body>
<div class="card">
  <div class="top">
    <div>
      <h2>&#128202; Grade Report</h2>
      <p class="sub">Checked __CHECKED__</p>
    </div>
    <button id="theme" type="button" data-mode="auto" title="Theme">
      <svg class="i-auto" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor"/></svg>
      <svg class="i-sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M5.3 5.3L7 7M17 17l1.7 1.7M5.3 18.7L7 17M17 7l1.7-1.7" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>
      <svg class="i-moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 14.2A8.6 8.6 0 0 1 9.8 3.5a8.6 8.6 0 1 0 10.7 10.7z" fill="currentColor"/></svg>
    </button>
  </div>
  __UPDATE__
  <table>
    <thead><tr>__HEADERS__</tr></thead>
    <tbody>__ROWS__</tbody>
  </table>
  <p class="foot">Schoology Grade Checker __VERSION__</p>
</div>
<script>
(function () {
  var KEY = "sgTheme", root = document.documentElement, order = ["auto", "light", "dark"];
  var names = { auto: "Auto (follows your system)", light: "Light", dark: "Dark" };
  function load() { try { var v = localStorage.getItem(KEY); return order.indexOf(v) >= 0 ? v : "auto"; } catch (e) { return "auto"; } }
  function save(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  function apply(mode) {
    if (mode === "auto") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", mode);
    var btn = document.getElementById("theme");
    btn.setAttribute("data-mode", mode);
    btn.title = "Theme: " + names[mode] + " (click to change)";
    btn.setAttribute("aria-label", btn.title);
  }
  apply(load());
  document.getElementById("theme").addEventListener("click", function () {
    var next = order[(order.indexOf(load()) + 1) % order.length];
    save(next); apply(next);
  });
})();
</script>
</body>
</html>
"""


def favicon_data():
    try:
        with open(resource_path(os.path.join("icons", "grades_64.png")), "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return ""


def write_report(results, use_custom, update=None):
    headers = "<th>Course</th><th>Schoology %</th><th>Official Grade</th>"
    if use_custom:
        headers += "<th>Custom Grade</th>"

    rows = ""
    for d in results:
        pct = f"{d['pct']}%" if isinstance(d["pct"], (int, float)) else html.escape(str(d["pct"]))
        rows += (
            f"<tr><td>{html.escape(d['name'])}</td><td>{pct}</td>"
            f"<td class='official'>{html.escape(d['official'])}</td>"
        )
        if use_custom:
            rows += f"<td class='custom'>{html.escape(d['custom'])}</td>"
        rows += "</tr>"

    banner = ""
    if update:
        banner = (
            f"<a class='update' href='{html.escape(update['url'], quote=True)}' target='_blank' rel='noopener'>"
            f"&#11014; Version {html.escape(update['version'])} is available. Download it</a>"
        )

    page = (
        REPORT_TEMPLATE
        .replace("__DARK__", DARK_VARS)
        .replace("__FAVICON__", favicon_data())
        .replace("__CHECKED__", f"{datetime.now():%b %d, %Y at %I:%M %p}".replace(" 0", " "))
        .replace("__UPDATE__", banner)
        .replace("__HEADERS__", headers)
        .replace("__ROWS__", rows)
        .replace("__VERSION__", html.escape(app_version()))
    )
    path = os.path.abspath("grade_report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


def show_report(path, rows):
    """Open the report in a small bare Chrome window (no tabs, no address bar).
    Falls back to the normal browser if Chrome can't be found."""
    height = max(400, min(900, 260 + 47 * rows))  # room for the update banner too
    if open_app_window(Path(path).as_uri(), 640, height):
        return
    if hasattr(os, "startfile"):
        os.startfile(path)
    else:
        webbrowser.open(Path(path).as_uri())


# --------------------------------------------------------------------------
# UI (all Tk calls stay on the main thread)
# --------------------------------------------------------------------------
def worker(root, status_label, on_ok, on_error):
    def status(text):
        try:
            root.after(0, lambda: status_label.config(text=text))
        except Exception:
            pass

    try:
        config = load_config()
        # Ask (quietly, in the background) whether a newer version has been published.
        update = {}
        checker = threading.Thread(target=lambda: update.update(info=check_for_update()), daemon=True)
        checker.start()
        results, use_custom = scrape_all(config, status)
        status("Generating report card...")
        checker.join(timeout=1.5)
        path = write_report(results, use_custom, update.get("info"))
        root.after(0, lambda: on_ok(path, len(results)))
    except Exception as e:
        friendly = explain(e)
        if friendly.kind != "needs_setup":  # not really an error: they just haven't set up yet
            log_error(traceback.format_exc())
        root.after(0, lambda f=friendly: on_error(f))


def main():
    root = tk.Tk()
    root.title(f"Schoology Grade Checker {app_version()}")
    set_window_icon(root, "grades")
    root.geometry("340x150")
    root.resizable(False, False)
    root.eval("tk::PlaceWindow . center")

    status_label = tk.Label(root, text="Initializing...", font=("Segoe UI", 11, "bold"), pady=15)
    status_label.pack()

    progress = ttk.Progressbar(root, orient="horizontal", length=280, mode="indeterminate")
    progress.pack(pady=5)
    progress.start(10)

    def quit_now():
        """Close the browser we started, then leave. Nothing is left running."""
        status_label.config(text="Closing...")
        root.update_idletasks()
        close_browser()
        try:
            root.destroy()
        except Exception:
            pass
        os._exit(0)  # also stops the background worker

    def ask_to_close():
        if messagebox.askyesno("Schoology Grade Checker", "Stop checking grades and close?", parent=root):
            quit_now()

    def open_setup():
        if not launch_sibling("setup"):
            messagebox.showinfo(
                "Schoology Grade Checker",
                "Please open Schoology Setup (it's installed next to this program) to choose your classes.",
                parent=root,
            )

    def on_ok(path, rows):
        status_label.config(text="Opening your report...")
        try:
            show_report(path, rows)
        except Exception:
            log_error(traceback.format_exc())
            messagebox.showerror(
                "Schoology Grade Checker",
                f"Your grades were checked, but the report window couldn't open.\n\nYou can open the file yourself:\n{path}",
                parent=root,
            )
        root.after(900, root.destroy)  # give the report window a moment to appear first

    def on_error(friendly):
        if friendly.kind == "needs_setup":  # first run: go straight to Setup
            status_label.config(text="Opening Setup...")
            open_setup()
            root.after(600, root.destroy)
            return
        choice = show_error_dialog(root, friendly, "grades")
        if choice == "try_again":
            start_check()
            return
        if choice == "open_setup":
            open_setup()
        root.destroy()

    def start_check():
        status_label.config(text="Initializing...")
        threading.Thread(target=worker, args=(root, status_label, on_ok, on_error), daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", ask_to_close)
    try:
        root.createcommand("tk::mac::Quit", quit_now)  # macOS: Cmd-Q and Dock > Quit
    except Exception:
        pass

    # Make sure the window is in front (on macOS it can open behind other windows).
    root.lift()
    root.attributes("-topmost", True)
    root.after(500, lambda: root.attributes("-topmost", False))
    root.focus_force()

    start_check()
    root.mainloop()


if __name__ == "__main__":
    main()
