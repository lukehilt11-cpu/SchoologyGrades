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
import sys
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
    acquire_lock,
    add_help_menu,
    app_version,
    auto_log,
    build_alerts,
    check_for_update,
    close_browser,
    compose_notification,
    explain,
    launch_sibling,
    log_error,
    log_in,
    make_driver,
    notify,
    offer_update,
    open_app_window,
    read_auto,
    release_lock,
    resource_path,
    set_window_icon,
    show_error_dialog,
    update_prompt_snoozed,
    wait_for_idle,
    wait_until,
    write_auto,
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


# Classes differ in how Schoology shows the cumulative grade. We recognise three
# "layouts" and remember which one each class uses (see LayoutMemory below):
#   summary    a "Course Grade" line under the grades
#   course_row a "Course Grade" row in the table
#   periods    the teacher hides the cumulative grade, so the page only shows the
#              grading periods (Q1, Q2, ...); we work it out the way Schoology does:
#              the average of the graded periods, weighted by each period's share.
CALC_JS = """
var calcBadge = function (root) {
  var rows = root.querySelectorAll('tr.period-row'), sum = 0, wsum = 0;
  for (var i = 0; i < rows.length; i++) {
    var w = rows[i].querySelector('.percentage-contrib');
    var g = rows[i].querySelector('td.grade-column .rounded-grade');
    var weight = w ? parseFloat((w.textContent || '').replace(/[^0-9.]/g, '')) : NaN;
    var pct = g ? parseFloat(g.getAttribute('title') || g.textContent || '') : NaN;
    if (isNaN(weight) || weight <= 0 || isNaN(pct)) continue;
    sum += pct * weight; wsum += weight;
  }
  if (!wsum) return null;
  var v = (Math.round(sum / wsum * 100) / 100) + '%';
  return [v, v];
};
var hasNum = function (b) { return !!b && /[0-9]/.test((b[0] || '') + (b[1] || '')); };
var layoutOf = function (g, f, c) { return hasNum(g) ? 'summary' : hasNum(f) ? 'course_row' : c ? 'periods' : null; };
"""

DATA_FN = r"""
var dataOf = function (root) {
  var txt = function (e) { return e ? (e.textContent || '').replace(/\s+/g, ' ').trim() : ''; };
  var ttl = function (r, sel) {
    var e = r.querySelector(sel || '.title'); if (!e) return '';
    var c = e.cloneNode(true), hid = c.querySelectorAll('.visually-hidden');
    for (var i = 0; i < hid.length; i++) hid[i].remove();
    return txt(c);
  };
  var num = function (e) { var v = e ? parseFloat(e.getAttribute('title') || e.textContent || '') : NaN; return isNaN(v) ? null : v; };
  var rows = root.querySelectorAll('tr.report-row'), periods = [], pname = {}, cats = {}, catlist = [], items = [];
  for (var i = 0; i < rows.length && items.length < 400; i++) {
    var r = rows[i], id = r.getAttribute('data-id'), par = r.getAttribute('data-parent-id'), cls = r.classList;
    if (cls.contains('period-row')) {
      var w = parseFloat(txt(r.querySelector('.percentage-contrib')).replace(/[^0-9.]/g, ''));
      pname[id] = ttl(r);
      periods.push({n: pname[id], w: isNaN(w) ? null : w, p: num(r.querySelector('td.grade-column .rounded-grade'))});
    } else if (cls.contains('category-row')) {
      var cw = parseFloat(txt(r.querySelector('.percentage-contrib')).replace(/[^0-9.]/g, ''));
      cats[id] = {n: ttl(r), q: pname[par] || ''};
      catlist.push({q: cats[id].q, n: cats[id].n, w: isNaN(cw) ? null : cw, p: num(r.querySelector('td.grade-column .rounded-grade'))});
    } else if (cls.contains('item-row')) {
      var cat = cats[par] || {n: '', q: ''}, due = ttl(r, '.due-date'), m = due.match(/(\d{1,2})\/(\d{1,2})\/(\d{2,4})/), d = '';
      if (m) { var y = +m[3]; if (y < 100) y += 2000; d = y + '-' + ('0' + m[1]).slice(-2) + '-' + ('0' + m[2]).slice(-2); }
      var mx = parseFloat(txt(r.querySelector('.max-grade')).replace(/[^0-9.]/g, ''));
      var gc = r.querySelector('td.grade-column');
      var miss = cls.contains('missing') || (!!gc && (/missing/i.test(gc.textContent || '') || !!gc.querySelector('[class*="missing" i], [title*="missing" i], [aria-label*="missing" i]')));
      items.push({n: ttl(r), d: d, s: num(r.querySelector('.awarded-grade .rounded-grade')), m: isNaN(mx) ? null : mx, c: cat.n, q: cat.q, x: miss ? 1 : 0});
    }
  }
  return {periods: periods, items: items, cats: catlist};
};
"""

DATA_JS = DATA_FN + "return dataOf(document);"


def read_data(driver):
    """Every grading period, and every assignment with its score, from the page in the current tab."""
    try:
        data = driver.execute_script(DATA_JS)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def short_period(name):
    """'26-27 Q1' -> 'Q1'."""
    return re.sub(r"^\s*\d{2,4}\s*[-/\u2013]\s*\d{2,4}\s+", "", str(name or "")).strip()


def shape_data(data, use_custom):
    """Tidy the page's periods and assignments for the report (and for saving)."""
    out = {}
    periods = []
    for p in (data or {}).get("periods") or []:
        entry = {"n": short_period(p.get("n")), "w": p.get("w")}
        pct = p.get("p")
        if isinstance(pct, (int, float)):
            rounded = round_half_up(pct)
            entry.update(p=round(pct, 2), o=get_official_grade(rounded), c=get_custom_grade(rounded) if use_custom else "")
        periods.append(entry)
    items = []
    for i in (data or {}).get("items") or []:
        if not i.get("n"):
            continue
        item = {"n": i["n"], "d": i.get("d") or "", "s": i.get("s"), "m": i.get("m"), "c": i.get("c") or "", "q": short_period(i.get("q"))}
        if i.get("x"):
            item["x"] = 1  # Schoology marked it missing
        items.append(item)
    cats = [{"q": short_period(k.get("q")), "n": k.get("n") or "", "w": k.get("w"), "p": k.get("p")}
            for k in (data or {}).get("cats") or []]
    if periods:
        out["periods"] = periods
    if items:
        out["items"] = items
    if cats:
        out["cats"] = cats
    return out


PROBE_JS = CALC_JS + """
var vis = function (e) { return !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length); };
var first = function (sel) {
  var list = document.querySelectorAll(sel);
  for (var i = 0; i < list.length; i++) { if (vis(list[i])) return list[i]; }
  return null;
};
var badge = function (e) { return e ? [e.getAttribute('title') || '', e.innerText || e.textContent || ''] : null; };
var heading = document.querySelector(arguments[2]);
var g = badge(first(arguments[0])), f = badge(first(arguments[1])), c = f ? null : calcBadge(document);
return {
  host: location.host,
  path: location.pathname,
  ready: document.readyState,
  login: !!(first(arguments[3]) || first(arguments[4])),
  title: heading ? (heading.innerText || heading.textContent || '') : null,
  grade: g,
  fallback: f || c,
  layout: layoutOf(g, f, c)
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


def _course_result_base(driver, idx, title, badge, use_custom):
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


LAYOUTS_FILE = "layouts.json"
RECHECK_EVERY = 10  # a class whose grade has to be worked out is looked at the slow, careful way every 10th run


class LayoutMemory:
    """Remembers, per class, how its page shows the grade (see CALC_JS above).

    Saved in data/layouts.json. A class we already know has no cumulative-grade line
    doesn't have to be waited on, so its grade is taken as soon as the page is
    drawn. If a class ever can't be read, it is forgotten and looked at carefully
    next time (re-diagnosed); known classes are also re-checked every RECHECK_EVERY
    runs, which catches a teacher turning the cumulative grade on or off.
    """

    def __init__(self, path=LAYOUTS_FILE):
        self.path = path
        self.data = {}
        self.changed = False
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for cid, entry in (raw.get("classes") or {}).items():
                if isinstance(entry, dict) and entry.get("layout") in ("summary", "course_row", "periods"):
                    self.data[str(cid)] = {"layout": entry["layout"], "runs": int(entry.get("runs", 0))}
        except Exception:
            self.data = {}

    def skip_wait(self, cid):
        """True if this class is known to show no cumulative-grade line (and isn't due a re-check)."""
        entry = self.data.get(cid)
        return bool(entry) and entry["layout"] in ("course_row", "periods") and entry["runs"] < RECHECK_EVERY

    def record(self, cid, layout, diagnosed):
        """Note what a read just showed. diagnosed: True if we waited and looked carefully."""
        if cid is None:
            return
        cid = str(cid)
        if layout is None:  # couldn't read it: forget, so the next run looks carefully
            if self.data.pop(cid, None) is not None:
                self.changed = True
            return
        entry = self.data.get(cid)
        if entry is None or entry["layout"] != layout:
            self.data[cid] = {"layout": layout, "runs": 0}
            self.changed = True
        elif diagnosed:
            if entry["runs"]:
                entry["runs"] = 0
                self.changed = True
        else:
            entry["runs"] += 1
            self.changed = True

    def save(self):
        if not self.changed:
            return
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"classes": self.data}, f, indent=1)
        except OSError:
            pass


SHOW_BROWSER = [False]  # True for one run when the person chose "Sign in": Chrome is shown so they can sign in themselves

def course_result(driver, idx, title, badge, use_custom, data=None):
    result = _course_result_base(driver, idx, title, badge, use_custom)
    result.update(shape_data(data, use_custom))  # periods and assignments, when the page had them
    return result


SESSION_MARKER = ".session_ok"  # exists after a run that finished while already signed in


class LoginNeeded(RuntimeError):
    """A course tab showed the sign-in page."""


def course_id(path):
    match = re.search(r"/course/(\d+)", path or "")
    return match.group(1) if match else None


def read_courses(driver, handles, want, host, use_custom, on_result, max_wait=25, blank_wait=15, layouts=None):
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

            badge, title, finished, diagnosed = state.get("grade"), state.get("title"), False, True
            if badge is not None:
                finished = True
            elif title is not None:
                now = time.time()
                first_title.setdefault(idx, now)
                if state.get("ready") == "complete" or now - first_title[idx] > 6:
                    settled.setdefault(idx, now)
                if (state.get("ready") == "complete" and state.get("fallback") is not None
                        and layouts is not None and layouts.skip_wait(cid)):
                    badge, finished, diagnosed = state.get("fallback"), True, False  # known layout: no need to wait
                elif idx in settled and now - settled[idx] >= SETTLE_SECONDS:
                    badge = state.get("fallback")  # last resort: the course grade row, or worked out from the grading periods
                    finished = True

            if finished:
                if layouts is not None:
                    layouts.record(cid, state.get("layout") if badge is not None else None, diagnosed)
                results[idx] = course_result(driver, idx, title, badge, use_custom, read_data(driver))
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


def fetch_all(driver, urls, host, use_custom, note, navigate_main, timeline, layouts=None):
    """Load every course in parallel tabs and read each grade. Returns results in order."""
    want = {course_id(u): i for i, u in enumerate(urls, start=1)}
    handles = open_courses(driver, urls, navigate_main)
    timeline.mark("All course tabs opened")

    results, stuck = read_courses(driver, handles, want, host, use_custom, note, layouts=layouts)

    if stuck:  # tabs that never rendered (e.g. throttled): load each directly and look again
        timeline.mark(f"{len(stuck)} tab(s) never loaded, reloading them")
        fresh = []
        for idx in stuck:
            driver.switch_to.new_window("tab")
            driver.get(urls[idx - 1])
            fresh.append(driver.current_window_handle)
        retry_want = {course_id(urls[i - 1]): i for i in stuck}
        more, stuck = read_courses(driver, fresh, retry_want, host, use_custom, note, max_wait=20, layouts=layouts)
        results.update(more)
        for idx in stuck:  # still nothing: record N/A (with a screenshot)
            results[idx] = course_result(driver, idx, "", None, use_custom)
            if layouts is not None:
                layouts.record(course_id(urls[idx - 1]), None, True)  # forget it: look carefully next time

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

FETCH_JS = CALC_JS + DATA_FN + """
var done = arguments[arguments.length - 1];
var urls = arguments[0], gradeSel = arguments[1], fallbackSel = arguments[2], titleSel = arguments[3];
var badge = function (e) { return e ? [e.getAttribute('title') || '', e.textContent || ''] : null; };
Promise.all(urls.map(function (u) {
  return fetch(u, {credentials: 'same-origin', cache: 'no-store'}).then(function (r) {
    return r.text().then(function (t) {
      var doc = new DOMParser().parseFromString(t, 'text/html');
      var h = doc.querySelector(titleSel);
      var g = badge(doc.querySelector(gradeSel)), f = badge(doc.querySelector(fallbackSel)), c = f ? null : calcBadge(doc);
      return {
        url: r.url, ok: r.ok,
        login: !!doc.querySelector("input[type='password']"),
        title: h ? h.textContent : null,
        grade: g,
        fallback: f || c,
        layout: layoutOf(g, f, c),
        data: dataOf(doc)
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


def results_from_pages(items, use_custom, layouts=None):
    results = []
    for idx, item in enumerate(items, start=1):
        badge = item.get("grade") if item.get("grade") is not None else item.get("fallback")
        if layouts is not None:
            layouts.record(course_id(urlparse(item.get("url") or "").path), item.get("layout") if badge is not None else None, True)
        results.append(course_result(None, idx, item.get("title") or "", badge, use_custom, item.get("data")))
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
    headless = config.get("headless", True) and not SHOW_BROWSER[0]
    use_custom = wants_custom_scale(config["email"])
    layouts = LayoutMemory()

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
                results = results_from_pages(pages, use_custom, layouts)  # no tabs needed at all
                save_fetch_state("ok", count + 1)
                for idx in range(1, len(urls) + 1):
                    note(idx)
            else:
                try:
                    results = fetch_all(driver, urls, host, use_custom, note, True, timeline, layouts)
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
                if not headless:
                    status("Sign in in the browser window if it asks you to...")
                log_in(driver, config["email"], config["password"], host, 30 if headless else 180, status)
                timeline.mark("Signed in")

            # Make sure the first tab is on the first course (login may land elsewhere).
            here, want = urlparse(driver.current_url), urlparse(urls[0])
            navigate_main = (here.netloc, here.path.rstrip("/")) != (want.netloc, want.path.rstrip("/"))
            timeline.mark("Schoology ready")
            status("Loading all courses...")
            results = fetch_all(driver, urls, host, use_custom, note, navigate_main, timeline, layouts)

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
        layouts.save()
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
  --note-bg: #3a2f12; --note-fg: #f3d9a4;
  --up: #4ade80; --down: #fb923c;
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
  --upd-bg: #eef2ff; --upd-fg: #3730a3; --note-bg: #fff4d6; --note-fg: #6b4a05;
  --up: #15803d; --down: #c2410c;
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
td:first-child a { color: inherit; text-decoration: none; border-bottom: 1px dashed var(--line); }
td:first-child a:hover { color: var(--custom); border-bottom-color: var(--custom); }
.note { margin: 14px 0 0; padding: 10px 14px; border-radius: 12px; text-align: center; font-size: 13px; font-weight: 600;
  background: var(--note-bg); color: var(--note-fg); }
.cheer { margin: 6px 0 0; font-size: 13.5px; font-weight: 700; color: var(--custom); }
.update { display: block; margin: 14px 0 0; padding: 10px 14px; border-radius: 12px; text-align: center;
  background: var(--upd-bg); color: var(--upd-fg); font-size: 13px; font-weight: 600; text-decoration: none; }
.update:hover { filter: brightness(1.06); }
.bar { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; flex-wrap: wrap;
  margin-top: 16px; border-bottom: 1px solid var(--line); }
.tabs { display: flex; gap: 2px; }
.tab { appearance: none; background: none; border: 0; border-bottom: 2px solid transparent; margin-bottom: -1px;
  padding: 8px 12px; font: inherit; font-size: 14px; font-weight: 600; color: var(--muted); cursor: pointer; }
.tab:hover { color: var(--text); }
.tab[aria-selected="true"] { color: var(--text); border-bottom-color: var(--custom); }
.tab:focus-visible, .seg button:focus-visible { outline: 2px solid var(--custom); outline-offset: 2px; border-radius: 6px; }
.seg { display: inline-flex; background: var(--btn); border-radius: 9px; padding: 2px; margin-bottom: 6px; }
.seg button { border: 0; background: none; font: inherit; font-size: 12.5px; font-weight: 600; color: var(--muted);
  padding: 5px 11px; border-radius: 7px; cursor: pointer; }
.seg button[aria-pressed="true"] { background: var(--card); color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,.14); }
.panel[hidden], .bar[hidden], .seg[hidden] { display: none; }
.panel table { margin-top: 6px; }
.delta { margin-left: 6px; font-size: 11.5px; font-weight: 700; }
.delta.up { color: var(--up); } .delta.down { color: var(--down); }
.tools { display: flex; justify-content: flex-end; margin-top: 12px; }
.tools .seg { margin: 0; }
.acls { margin: 18px 0 2px; font-size: 14px; font-weight: 650; }
.acls small { margin-left: 6px; font-size: 12px; font-weight: 500; color: var(--muted); }
.arow { display: flex; justify-content: space-between; gap: 14px; padding: 9px 0; border-bottom: 1px solid var(--line); font-size: 13.5px; }
.arow:last-child { border-bottom: 0; }
.arow .when { display: block; margin-top: 2px; color: var(--muted); font-size: 12px; }
.arow .score { font-weight: 600; white-space: nowrap; font-variant-numeric: tabular-nums; }
.arow .score.low { color: var(--down); } .arow .score.none { color: var(--muted); font-weight: 500; }
.empty { margin: 30px 0 14px; text-align: center; color: var(--muted); font-size: 13.5px; }
.lede { margin: 18px 0 4px; font-size: 14.5px; line-height: 1.5; }
.bars { display: block; width: 100%; height: auto; margin-top: 6px; }
.bars .lbl { fill: var(--text); font-size: 13px; } .bars .val { fill: var(--muted); font-size: 12.5px; font-variant-numeric: tabular-nums; }
.bars .track { fill: var(--head); } .bars .fill { fill: var(--custom); }
.stack { display: flex; height: 12px; margin-top: 10px; border-radius: 6px; overflow: hidden; background: var(--head); }
.stack span { display: block; min-width: 0; }
.s0 { background: var(--up); } .s1 { background: var(--custom); } .s2 { background: var(--muted); } .s3 { background: var(--down); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; margin: 10px 0 0; padding: 0; list-style: none; font-size: 12.5px; color: var(--muted); }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 3px; margin-right: 6px; vertical-align: baseline; }
.legend b { color: var(--text); font-weight: 650; }
.qt { margin-top: 6px; } .qt td, .qt th { padding: 9px 12px; font-size: 13.5px; }
.hint { margin: 4px 0 0; color: var(--muted); font-size: 12.5px; line-height: 1.45; }
.gpa input, .gpa select, .form input, .form select { font: inherit; font-size: 13.5px; padding: 6px 9px; border-radius: 9px;
  border: 1px solid var(--line); background: var(--card); color: var(--text); max-width: 100%; }
.gpa input { width: 70px; text-align: center; }
.gpa input:focus-visible, .gpa select:focus-visible, .form input:focus-visible, .form select:focus-visible { outline: 2px solid var(--custom); outline-offset: 1px; }
.result { margin: 12px 0 0; padding: 12px 14px; border-radius: 12px; background: var(--head); font-size: 14px; line-height: 1.5; }
.result b { font-weight: 700; }
.form { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px 12px; margin-top: 12px; }
.form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.form .seg { grid-column: 1 / -1; justify-self: start; margin: 0; }
.arow .score.missing { color: var(--down); }
.aright { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
.linkbtn { border: 0; background: none; color: var(--muted); font: inherit; font-size: 12px; cursor: pointer; padding: 2px 4px; border-radius: 6px; }
.linkbtn:hover { color: var(--text); text-decoration: underline; }
.linkbtn:focus-visible { outline: 2px solid var(--custom); outline-offset: 2px; }
.topbtns { display: flex; align-items: center; gap: 8px; position: relative; flex: none; }
#share { flex: none; width: 36px; height: 36px; border: 0; border-radius: 50%; cursor: pointer;
  background: var(--btn); color: var(--text); display: grid; place-items: center; padding: 0; }
#share:hover, #share[aria-expanded="true"] { background: var(--btn-hover); }
#share:focus-visible, .mi:focus-visible { outline: 2px solid var(--custom); outline-offset: 2px; }
#share svg { width: 19px; height: 19px; }
#sharemenu { position: absolute; right: 0; top: 44px; z-index: 20; min-width: 210px; padding: 6px; border-radius: 14px;
  background: var(--card); box-shadow: 0 8px 30px rgba(0,0,0,.22), 0 0 0 1px var(--line); }
#sharemenu[hidden] { display: none; }
.mi { display: block; width: 100%; text-align: left; border: 0; background: none; color: var(--text); font: inherit; font-size: 14px;
  padding: 9px 12px; border-radius: 9px; cursor: pointer; }
.mi:hover { background: var(--btn); }
#toast { position: fixed; left: 50%; bottom: 22px; transform: translateX(-50%); padding: 9px 16px; border-radius: 999px;
  background: var(--text); color: var(--card); font-size: 13px; opacity: 0; pointer-events: none; transition: opacity .18s; }
#toast.on { opacity: 1; }
#sharecard { display: none; }
@media print {
  body > :not(#sharecard) { display: none !important; }
  #sharecard { display: block; }
  #sharecard .sg-card { width: auto; max-width: 720px; margin: 0 auto; }
  body { background: #fff; padding: 0; }
  @page { margin: 14mm; }
}
.foot { margin: 14px 0 0; color: var(--muted); font-size: 11.5px; text-align: center; }
</style>
</head>
<body data-cheer="__CHEERFLAG__">
<div class="card">
  <div class="top">
    <div>
      <h2>&#128202; Grade Report</h2>
      <p class="sub">Checked __CHECKED__</p>
      __CHEER__
    </div>
    <div class="topbtns">
      <button id="share" type="button" title="Share" aria-label="Share" aria-haspopup="menu" aria-expanded="false">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15V3.5M7.5 8 12 3.5 16.5 8M5 13v6.5a1.5 1.5 0 0 0 1.5 1.5h11a1.5 1.5 0 0 0 1.5-1.5V13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
      </button>
      <div id="sharemenu" role="menu" aria-label="Share these grades" hidden>
        <button class="mi" role="menuitem" type="button" data-do="text">Copy text summary</button>
        <button class="mi" role="menuitem" type="button" data-do="image">Save as image (PNG)</button>
        <button class="mi" role="menuitem" type="button" data-do="pdf">Save as PDF&hellip;</button>
        <button class="mi" role="menuitem" type="button" data-do="html">Save as HTML</button>
      </div>
      <button id="theme" type="button" data-mode="auto" title="Theme">
        <svg class="i-auto" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor"/></svg>
        <svg class="i-sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4.2" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M5.3 5.3L7 7M17 17l1.7 1.7M5.3 18.7L7 17M17 7l1.7-1.7" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>
        <svg class="i-moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20.5 14.2A8.6 8.6 0 0 1 9.8 3.5a8.6 8.6 0 1 0 10.7 10.7z" fill="currentColor"/></svg>
      </button>
    </div>
  </div>
  __NOTICE__
  __UPDATE__
  <div class="bar" id="bar" hidden>
    <div class="tabs" role="tablist" aria-label="Report sections">
      <button class="tab" role="tab" id="t-grades" aria-selected="true" aria-controls="grades" type="button">Grades</button>
      <button class="tab" role="tab" id="t-assign" aria-selected="false" aria-controls="assign" type="button">Assignments</button>
      <button class="tab" role="tab" id="t-stats" aria-selected="false" aria-controls="stats" type="button">Stats</button>
      <button class="tab" role="tab" id="t-calc" aria-selected="false" aria-controls="calc" type="button">Calculator</button>
    </div>
    <div class="seg" id="scope" role="group" aria-label="Grading period"></div>
  </div>
  <section class="panel" id="grades" role="tabpanel" aria-labelledby="t-grades">
  <table>
    <thead><tr>__HEADERS__</tr></thead>
    <tbody>__ROWS__</tbody>
  </table>
  </section>
  <section class="panel" id="assign" role="tabpanel" aria-labelledby="t-assign" hidden>
    <div class="tools"><div class="seg" id="view" role="group" aria-label="Which assignments"></div></div>
    <div id="alist"></div>
  </section>
  <section class="panel" id="stats" role="tabpanel" aria-labelledby="t-stats" hidden><div id="statbox"></div></section>
  <section class="panel" id="calc" role="tabpanel" aria-labelledby="t-calc" hidden><div id="calcbox"></div></section>
  <script type="application/json" id="sgdata">__DATA__</script>
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
<div id="toast" role="status" aria-live="polite"></div>
<div id="sharecard" aria-hidden="true"></div>
<script>
/* tabs, grading-period picker, assignments, stats and calculator; the grades table works without any of this */
(function () {
  var node = document.getElementById("sgdata"), D;
  try { D = JSON.parse(node.textContent); } catch (e) { return; }
  var classes = D.classes || [], scopes = ["Semester"], seen = {}, scope = "Semester", view = "attention";
  var order = [], graded = {};
  classes.forEach(function (c) { (c.periods || []).forEach(function (p) {
    if (order.indexOf(p.n) < 0) order.push(p.n);
    if (typeof p.p === "number") graded[p.n] = 1; }); });
  order.forEach(function (n) { if (graded[n]) scopes.push(n); });
  var hasItems = classes.some(function (c) { return (c.items || []).length; });
  var hasGrades = classes.some(function (c) { return typeof c.g === "number"; });
  if (scopes.length < 2 && !hasItems && !hasGrades) return;
  function $(id) { return document.getElementById(id); }
  $("bar").hidden = false;
  var rows = document.querySelectorAll("#grades tbody tr"), keep = [];
  Array.prototype.forEach.call(rows, function (tr) { keep.push(Array.prototype.map.call(tr.children, function (td) { return td.innerHTML; })); });

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function svgEl(tag, attrs) { var e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (var k in attrs) e.setAttribute(k, attrs[k]); return e; }
  function fmt(n) { return String(Math.round(n * 100) / 100); }
  function segment(box, names, labels, current, onPick) {
    box.textContent = "";
    names.forEach(function (n, i) {
      var b = el("button", "", labels[i]); b.type = "button"; b.setAttribute("aria-pressed", n === current ? "true" : "false");
      b.addEventListener("click", function () { onPick(n); }); box.appendChild(b);
    });
  }
  function today() { var d = new Date(); return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2); }
  function kind(it) {
    if (it.x) return "missing";
    if (typeof it.s !== "number") return it.d && it.d <= today() ? "none" : "";
    return it.m > 0 && it.s / it.m < 0.7 ? "low" : "";
  }
  function shortDate(s) { var p = s.split("-"); return new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString(undefined, { month: "short", day: "numeric" }); }
  function periodOf(c, s) { var r = null; (c.periods || []).forEach(function (x) { if (x.n === s) r = x; }); return r; }
  function pctFor(c, s) { if (s === "Semester") return typeof c.g === "number" ? c.g : null; var p = periodOf(c, s); return p && typeof p.p === "number" ? p.p : null; }
  function letterFor(c, s) { if (s === "Semester") return c.o || null; var p = periodOf(c, s); return p && p.o ? p.o : null; }
  function inScope(it) { return scope === "Semester" || it.q === scope; }
  function name(i) { return classes[i].n || "Class " + (i + 1); }

  /* ---- grades table ---- */
  function showScope() {
    segment($("scope"), scopes, scopes, scope, function (n) { scope = n; showScope(); });
    $("scope").hidden = scopes.length < 2;
    Array.prototype.forEach.call(rows, function (tr, i) {
      var cells = tr.children, p = periodOf(classes[i] || {}, scope);
      for (var k = 1; k < cells.length; k++) {
        if (scope === "Semester") cells[k].innerHTML = keep[i][k];
        else cells[k].textContent = !p || typeof p.p !== "number" ? "\u2014" : k === 1 ? p.p + "%" : k === 2 ? p.o : p.c;
      }
    });
    showAssign(); showStats(); showCalc();
  }

  /* ---- assignments ---- */
  var dismissed = {}, showHidden = false;
  try { dismissed = JSON.parse(localStorage.getItem("sgDismissed") || "{}") || {}; } catch (e) { dismissed = {}; }
  function saveDismissed() { try { localStorage.setItem("sgDismissed", JSON.stringify(dismissed)); } catch (e) {} }
  function iso(d) { return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2); }
  function keyOf(i, it) { return classes[i].n + "|" + it.n + "|" + it.d; }
  function addRow(parent, it, action) {
    var row = el("div", "arow"), left = el("div"), right = el("div", "aright"), k = kind(it), graded = typeof it.s === "number";
    left.appendChild(el("span", "", it.n));
    var when = [it.d ? shortDate(it.d) : "", it.c].filter(Boolean).join(", ");
    if (when) left.appendChild(el("span", "when", when));
    row.appendChild(left);
    right.appendChild(el("span", "score" + (k ? " " + k : ""), it.x ? "Missing" : !graded ? "Not graded" : it.m > 0 ? it.s + " / " + it.m : it.s + " extra credit"));
    if (action) {
      var b = el("button", "linkbtn", action.label); b.type = "button"; b.setAttribute("aria-label", action.label + " " + it.n);
      b.addEventListener("click", action.run); right.appendChild(b);
    }
    row.appendChild(right); parent.appendChild(row);
  }
  function showAssign() {
    segment($("view"), ["attention", "all"], ["Needs attention", "All"], view, function (n) { view = n; showAssign(); });
    var box = $("alist"), shown = 0, old = 0, hidden = [], attention = view === "attention", d = new Date(); box.textContent = "";
    d.setDate(d.getDate() - 30); var cutoff = iso(d);
    classes.forEach(function (c, i) {
      var list = (c.items || []).filter(inScope).filter(function (it) {
        if (!attention) return true;
        if (!kind(it)) return false;
        if (it.d && it.d < cutoff) { old++; return false; }
        if (dismissed[keyOf(i, it)]) { hidden.push({ i: i, it: it }); return false; }
        return true;
      });
      if (!list.length) return;
      list.sort(function (a, b) { return a.d < b.d ? 1 : a.d > b.d ? -1 : 0; });
      var h = el("h3", "acls", name(i)); h.appendChild(el("small", "", list.length + (list.length === 1 ? " item" : " items"))); box.appendChild(h);
      list.forEach(function (it) {
        addRow(box, it, attention ? { label: "Dismiss", run: function () { dismissed[keyOf(i, it)] = 1; saveDismissed(); showAssign(); } } : null);
        shown++;
      });
    });
    if (!shown) box.appendChild(el("p", "empty", attention ? "Nothing needs attention. Nice." : "No assignments to show here."));
    if (attention && old) box.appendChild(el("p", "hint", "Items from more than a month ago aren't listed here. Choose All to see them."));
    if (attention && hidden.length) {
      var t = el("button", "linkbtn", showHidden ? "Hide dismissed" : "Show dismissed (" + hidden.length + ")"); t.type = "button";
      t.addEventListener("click", function () { showHidden = !showHidden; showAssign(); }); box.appendChild(t);
      if (showHidden) hidden.forEach(function (x) {
        addRow(box, x.it, { label: "Restore", run: function () { delete dismissed[keyOf(x.i, x.it)]; saveDismissed(); showAssign(); } });
      });
    }
  }

  /* ---- stats ---- */
  function showStats() {
    var box = $("statbox"); box.textContent = "";
    var vals = [];
    classes.forEach(function (c, i) { var v = pctFor(c, scope); if (v !== null) vals.push({ n: name(i), v: v }); });
    if (!vals.length) { box.appendChild(el("p", "empty", "No grades to chart for this period yet.")); return; }
    var sum = 0, hi = vals[0], lo = vals[0];
    vals.forEach(function (x) { sum += x.v; if (x.v > hi.v) hi = x; if (x.v < lo.v) lo = x; });
    box.appendChild(el("p", "lede", "Your average is " + fmt(sum / vals.length) + "% across " + vals.length + (vals.length === 1 ? " class." : " classes.") +
      (vals.length > 1 ? " Highest is " + hi.n + " (" + fmt(hi.v) + "%); lowest is " + lo.n + " (" + fmt(lo.v) + "%)." : "")));
    box.appendChild(el("h3", "acls", "Grade by class"));
    var W = 600, rowH = 30, labelW = 176, barX = labelW + 10, barW = W - barX - 58, max = 100;
    vals.forEach(function (x) { if (x.v > max) max = x.v; });
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + (vals.length * rowH + 8), role: "img", "aria-label": "Grade by class", "class": "bars" });
    vals.forEach(function (x, i) {
      var y = 4 + i * rowH, t = svgEl("text", { x: labelW, y: y + 17, "text-anchor": "end", "class": "lbl" });
      t.textContent = x.n.length > 25 ? x.n.slice(0, 24) + "\u2026" : x.n; svg.appendChild(t);
      svg.appendChild(svgEl("rect", { x: barX, y: y + 6, width: barW, height: 12, rx: 6, "class": "track" }));
      svg.appendChild(svgEl("rect", { x: barX, y: y + 6, width: Math.max(4, barW * x.v / max), height: 12, rx: 6, "class": "fill" }));
      var v = svgEl("text", { x: W - 4, y: y + 17, "text-anchor": "end", "class": "val" }); v.textContent = fmt(x.v) + "%"; svg.appendChild(v);
    });
    box.appendChild(svg);

    var b = [0, 0, 0, 0], total = 0;
    classes.forEach(function (c) { (c.items || []).forEach(function (it) {
      if (inScope(it) && typeof it.s === "number" && it.m > 0) { var p = it.s / it.m * 100; b[p >= 90 ? 0 : p >= 80 ? 1 : p >= 70 ? 2 : 3]++; total++; } }); });
    if (total) {
      box.appendChild(el("h3", "acls", "Scores on assignments"));
      var stack = el("div", "stack"), legend = el("ul", "legend"), names = ["90% and up", "80 to 89%", "70 to 79%", "Under 70%"];
      b.forEach(function (n, i) {
        if (n) { var s = el("span", "s" + i); s.style.width = (n / total * 100) + "%"; stack.appendChild(s); }
        var li = el("li"), sw = el("i", "s" + i); li.appendChild(sw); li.appendChild(document.createTextNode(names[i] + " ")); li.appendChild(el("b", "", String(n))); legend.appendChild(li);
      });
      box.appendChild(stack); box.appendChild(legend);
    }

    var qs = scopes.slice(1), qrows = [];
    if (qs.length > 1) {
      classes.forEach(function (c, i) {
        var vs = qs.map(function (q) { return pctFor(c, q); }), nums = vs.filter(function (v) { return v !== null; });
        if (nums.length > 1) qrows.push({ n: name(i), vs: vs, d: nums[nums.length - 1] - nums[0] });
      });
    }
    if (qrows.length) {
      box.appendChild(el("h3", "acls", "Quarter to quarter"));
      var t = el("table", "qt"), hr = el("tr"); ["Class"].concat(qs, ["Change"]).forEach(function (h) { hr.appendChild(el("th", "", h)); });
      t.appendChild(el("thead")).appendChild(hr); var tb = el("tbody");
      qrows.forEach(function (r) {
        var tr = el("tr"); tr.appendChild(el("td", "", r.n));
        r.vs.forEach(function (v) { tr.appendChild(el("td", "", v === null ? "\u2014" : fmt(v) + "%")); });
        var td = el("td"); if (Math.abs(r.d) >= 0.01) td.appendChild(el("span", "delta " + (r.d > 0 ? "up" : "down"), (r.d > 0 ? "\u25b2" : "\u25bc") + fmt(Math.abs(r.d)))); else td.textContent = "\u2014";
        tr.appendChild(td); tb.appendChild(tr);
      });
      t.appendChild(tb); box.appendChild(t);
    }
  }

  /* ---- calculator ---- */
  var GP = { "A+": 4, "A": 4, "A-": 3.7, "B+": 3.3, "B": 3, "B-": 2.7, "C+": 2.3, "C": 2, "C-": 1.7, "D+": 1.3, "D": 1, "D-": 0.7, "F": 0 };
  var saved = {}; try { saved = JSON.parse(localStorage.getItem("sgCalc") || "{}") || {}; } catch (e) { saved = {}; }
  function persist() { try { localStorage.setItem("sgCalc", JSON.stringify(saved)); } catch (e) {} }
  var wi = { ci: null, q: null, cat: null, mode: "score", a: "", b: "10" };

  /* The course grade if one more score is added. Everything else uses the grades Schoology reports, so only the change is estimated. */
  function course(c, ex) {
    var sum = 0, ws = 0;
    (c.periods || []).forEach(function (p) {
      if (!(p.w > 0)) return;
      var s2 = 0, w2 = 0;
      (c.cats || []).forEach(function (k) {
        if (k.q !== p.n || !(k.w > 0)) return;
        var v = typeof k.p === "number" ? k.p : null;
        if (ex && ex.q === p.n && ex.c === k.n) {
          var P = 0; (c.items || []).forEach(function (it) { if (it.q === p.n && it.c === k.n && typeof it.s === "number") P += it.m || 0; });
          if (P + ex.m > 0) v = ((v === null ? 0 : v * P / 100) + ex.s) / (P + ex.m) * 100;
        }
        if (v !== null) { s2 += k.w * v; w2 += k.w; }
      });
      if (w2 > 0) { sum += p.w * s2 / w2; ws += p.w; }
    });
    return ws > 0 ? sum / ws : null;
  }
  function usable(c) { var v = course(c, null); return typeof c.g === "number" && (c.cats || []).length && v !== null && Math.abs(v - c.g) < 0.3; }

  function showCalc() {
    var box = $("calcbox"); box.textContent = "";
    box.appendChild(el("h3", "acls", "GPA estimate"));
    box.appendChild(el("p", "hint", "Uses the grades above for " + (scope === "Semester" ? "the semester" : scope) + " on a standard 4.0 scale. If you know a class's credits or whether it's Honors or AP, set them here; they're remembered on this computer."));
    var gt = el("table", "gpa"), gb = el("tbody"), picks = [];
    classes.forEach(function (c, i) {
      var l = letterFor(c, scope); if (!l || GP[l] === undefined) return;
      var s = saved[c.n] || {}, tr = el("tr"), cr = el("input"), bo = el("select");
      cr.type = "number"; cr.min = "0"; cr.step = "0.5"; cr.value = s.cr != null ? s.cr : 1; cr.setAttribute("aria-label", "Credits for " + c.n);
      [[0, "No boost"], [0.5, "Honors +0.5"], [1, "AP +1.0"]].forEach(function (o) { var op = el("option", "", o[1]); op.value = o[0]; bo.appendChild(op); });
      bo.value = s.b != null ? s.b : 0; bo.setAttribute("aria-label", "Honors or AP boost for " + c.n);
      tr.appendChild(el("td", "", c.n)); tr.appendChild(el("td", "", l));
      var t3 = el("td"); t3.appendChild(cr); tr.appendChild(t3); var t4 = el("td"); t4.appendChild(bo); tr.appendChild(t4);
      gb.appendChild(tr); picks.push({ c: c, l: l, cr: cr, bo: bo });
    });
    var out = el("p", "result");
    if (picks.length) {
      var head = el("tr"); ["Class", "Grade", "Credits", "Boost"].forEach(function (h) { head.appendChild(el("th", "", h)); });
      gt.appendChild(el("thead")).appendChild(head); gt.appendChild(gb); box.appendChild(gt); box.appendChild(out);
      var update = function () {
        var pts = 0, cred = 0;
        picks.forEach(function (x) {
          var cr = parseFloat(x.cr.value), bo = parseFloat(x.bo.value); if (isNaN(cr) || cr < 0) cr = 0;
          saved[x.c.n] = { cr: cr, b: bo };
          pts += cr * (GP[x.l] + (GP[x.l] > 0 ? bo : 0)); cred += cr;
        });
        persist(); out.textContent = ""; out.appendChild(document.createTextNode("Estimated GPA: "));
        out.appendChild(el("b", "", cred > 0 ? (Math.round(pts / cred * 100) / 100).toFixed(2) : "\u2014"));
      };
      picks.forEach(function (x) { x.cr.addEventListener("input", update); x.bo.addEventListener("change", update); }); update();
    } else box.appendChild(el("p", "empty", "No letter grades to count for this period yet."));

    box.appendChild(el("h3", "acls", "What if I score\u2026"));
    var ok = []; classes.forEach(function (c, i) { if (usable(c)) ok.push(i); });
    if (!ok.length) { box.appendChild(el("p", "hint", "Not available yet: it needs a graded class whose categories are listed on Schoology.")); return; }
    if (ok.indexOf(wi.ci) < 0) { wi.ci = ok[0]; wi.q = null; wi.cat = null; }
    var c = classes[wi.ci], pers = (c.periods || []).filter(function (p) { return (c.cats || []).some(function (k) { return k.q === p.n && k.w > 0; }) && p.w > 0; });
    if (!pers.some(function (p) { return p.n === wi.q; })) { var lastGraded = pers.filter(function (p) { return typeof p.p === "number"; }); wi.q = (lastGraded.length ? lastGraded[lastGraded.length - 1] : pers[pers.length - 1] || {}).n; wi.cat = null; }
    var cats = (c.cats || []).filter(function (k) { return k.q === wi.q && k.w > 0; });
    if (!cats.some(function (k) { return k.n === wi.cat; })) wi.cat = cats.length ? cats[0].n : null;

    var form = el("div", "form");
    function field(label, ctl) { var l = el("label", "", label); l.appendChild(ctl); form.appendChild(l); return ctl; }
    function select(items, labels, cur, onPick) { var s = el("select"); items.forEach(function (v, i) { var o = el("option", "", labels[i]); o.value = v; s.appendChild(o); }); s.value = cur; s.addEventListener("change", function () { onPick(s.value); }); return s; }
    var modeBox = el("div", "seg"); form.appendChild(modeBox);
    segment(modeBox, ["score", "need"], ["If I score", "What do I need"], wi.mode, function (n) { wi.mode = n; showCalc(); });
    field("Class", select(ok, ok.map(name), wi.ci, function (v) { wi.ci = +v; wi.q = null; wi.cat = null; showCalc(); }));
    field("Grading period", select(pers.map(function (p) { return p.n; }), pers.map(function (p) { return p.n; }), wi.q, function (v) { wi.q = v; wi.cat = null; showCalc(); }));
    field("Category", select(cats.map(function (k) { return k.n; }), cats.map(function (k) { return k.n + " (" + k.w + "%)"; }), wi.cat, function (v) { wi.cat = v; showCalc(); }));
    var a = el("input"); a.type = "number"; a.min = "0"; a.step = "any"; a.value = wi.mode === "score" ? wi.a : (wi.at || "");
    var m = el("input"); m.type = "number"; m.min = "0"; m.step = "any"; m.value = wi.b;
    field(wi.mode === "score" ? "My score" : "Grade I want in the class (%)", a); field("Out of (points)", m);
    box.appendChild(form);
    var res = el("p", "result"); box.appendChild(res);
    var base = c.g;
    function run() {
      wi.b = m.value; if (wi.mode === "score") wi.a = a.value; else wi.at = a.value;
      var pts = parseFloat(m.value), val = parseFloat(a.value); res.textContent = "";
      if (isNaN(pts) || pts <= 0 || isNaN(val) || val < 0) { res.textContent = "Enter " + (wi.mode === "score" ? "a score" : "a target") + " and what it's out of."; return; }
      function at(x) { return course(c, { q: wi.q, c: wi.cat, s: x, m: pts }); }
      if (wi.mode === "score") {
        var now = at(val);
        res.appendChild(document.createTextNode("Your " + c.n + " grade would go from " + fmt(base) + "% to "));
        res.appendChild(el("b", "", fmt(now) + "%")); res.appendChild(document.createTextNode(" (" + (now >= base ? "+" : "\u2212") + fmt(Math.abs(now - base)) + ")."));
      } else {
        if (at(0) >= val) { res.textContent = "You'd already be at " + fmt(val) + "% or higher, whatever you score on it."; return; }
        if (at(pts * 3) < val) { res.textContent = "One " + pts + "-point assignment in this category can't get you to " + fmt(val) + "%."; return; }
        var lo = 0, hi = pts * 3; for (var i = 0; i < 40; i++) { var mid = (lo + hi) / 2; if (at(mid) >= val) hi = mid; else lo = mid; }
        res.appendChild(document.createTextNode("To reach " + fmt(val) + "% you'd need about "));
        res.appendChild(el("b", "", (Math.ceil(hi * 100 - 1e-6) / 100) + " out of " + pts + " (" + fmt(hi / pts * 100) + "%)")); res.appendChild(document.createTextNode("."));
      }
    }
    a.addEventListener("input", run); m.addEventListener("input", run); run();
    box.appendChild(el("p", "hint", "An estimate from the grades Schoology shows. Your teacher's rules, like dropped scores, can shift it a little."));
  }

  /* ---- tabs ---- */
  var ids = ["grades", "assign", "stats", "calc"];
  function showTab(n) {
    ids.forEach(function (id) { var on = id === n; $(id).hidden = !on; $("t-" + id).setAttribute("aria-selected", on ? "true" : "false"); });
  }
  ids.forEach(function (id) { $("t-" + id).addEventListener("click", function () { showTab(id); }); });
  if (!hasItems) $("t-assign").style.display = "none";
  if (!hasGrades) { $("t-stats").style.display = "none"; $("t-calc").style.display = "none"; }
  showScope();
})();
</script>
<script>
/* Share: a text summary, an image, a PDF (through the print window) or an HTML copy of the grades as they are shown now */
(function () {
  var btn = document.getElementById("share"), menu = document.getElementById("sharemenu"), toastEl = document.getElementById("toast");
  if (!btn || !menu || !document.querySelector("#grades table")) return;
  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function say(text) { toastEl.textContent = text; toastEl.classList.add("on"); clearTimeout(say.t); say.t = setTimeout(function () { toastEl.classList.remove("on"); }, 2400); }

  function snapshot() {
    var t = document.querySelector("#grades table"), pick = function (s) { var e = document.querySelector(s); return e ? e.textContent.replace(/\s+/g, " ").trim() : ""; };
    var heads = Array.prototype.map.call(t.querySelectorAll("thead th"), function (th) { return th.textContent.trim(); });
    var rows = Array.prototype.map.call(t.querySelectorAll("tbody tr"), function (tr) {
      return Array.prototype.map.call(tr.children, function (td) {
        var c = td.cloneNode(true), chips = c.querySelectorAll(".delta");
        for (var i = 0; i < chips.length; i++) chips[i].remove();
        return c.textContent.replace(/\s+/g, " ").trim();
      });
    });
    var sc = document.querySelector("#scope:not([hidden]) button[aria-pressed=true]");
    return { heads: heads, rows: rows, scope: sc ? sc.textContent : "", checked: pick(".sub"), note: pick(".note"), foot: pick(".foot") };
  }
  function stem(s) {
    var d = new Date();
    return "Grades-" + d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2) + (s.scope ? "-" + s.scope : "");
  }
  var CSS = ".sg-card{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;background:#fff;color:#1d1d1f;padding:26px 28px 20px;width:600px;box-sizing:border-box}" +
    ".sg-card h1{font-size:22px;margin:0;letter-spacing:-.01em}.sg-card p{margin:6px 0 0;color:#6e6e73;font-size:13px}" +
    ".sg-card table{width:100%;border-collapse:collapse;margin-top:16px}" +
    ".sg-card th{background:#f2f2f7;color:#555;padding:10px 14px;font-size:12.5px;font-weight:600;text-align:center;border-bottom:1px solid #e5e5ea}" +
    ".sg-card td{padding:12px 14px;font-size:15px;text-align:center;border-bottom:1px solid #ececf0}" +
    ".sg-card th:first-child,.sg-card td:first-child{text-align:left}.sg-card tr:last-child td{border-bottom:0}" +
    ".sg-card .foot{margin-top:12px;text-align:center;color:#8e8e93;font-size:11.5px}";
  function card(s) {
    var c = el("div", "sg-card"), tbl = el("table"), hr = el("tr"), body = el("tbody");
    c.appendChild(el("style", "", CSS));
    c.appendChild(el("h1", "", "\uD83D\uDCCA Grade Report" + (s.scope ? " \u2013 " + s.scope : "")));
    c.appendChild(el("p", "", s.checked));
    if (s.note) c.appendChild(el("p", "", s.note));
    s.heads.forEach(function (h) { hr.appendChild(el("th", "", h)); });
    tbl.appendChild(el("thead")).appendChild(hr);
    s.rows.forEach(function (r) { var tr = el("tr"); r.forEach(function (v) { tr.appendChild(el("td", "", v)); }); body.appendChild(tr); });
    tbl.appendChild(body); c.appendChild(tbl);
    if (s.foot) c.appendChild(el("div", "foot", s.foot));
    return c;
  }
  function save(blob, name) {
    var a = el("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 1000); say("Saved " + name);
  }

  var actions = {
    text: function (s) {
      var lines = ["Grade Report" + (s.scope ? " (" + s.scope + ")" : ""), s.checked, ""];
      s.rows.forEach(function (r) { lines.push(r[0] + ": " + r.slice(1).join("  ")); });
      var text = lines.join("\n");
      function fallback() { var ta = el("textarea"); ta.value = text; ta.style.cssText = "position:fixed;left:-9999px"; document.body.appendChild(ta); ta.select();
        var ok = false; try { ok = document.execCommand("copy"); } catch (e) {} ta.remove(); say(ok ? "Copied to the clipboard" : "Couldn't copy"); }
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(function () { say("Copied to the clipboard"); }, fallback); else fallback();
    },
    image: function (s) {
      var c = card(s), host = el("div"); host.style.cssText = "position:fixed;left:-9999px;top:0;visibility:hidden"; host.appendChild(c); document.body.appendChild(host);
      var w = c.offsetWidth, h = c.offsetHeight, xml = new XMLSerializer().serializeToString(c); host.remove();
      var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + w + '" height="' + h + '"><foreignObject width="100%" height="100%">' + xml + "</foreignObject></svg>";
      var img = new Image();
      img.onload = function () {
        var cv = document.createElement("canvas"); cv.width = w * 2; cv.height = h * 2; var x = cv.getContext("2d");
        x.fillStyle = "#fff"; x.fillRect(0, 0, cv.width, cv.height); x.scale(2, 2); x.drawImage(img, 0, 0);
        try { cv.toBlob(function (b) { if (b) save(b, stem(s) + ".png"); else say("Couldn't make the image"); }, "image/png"); } catch (e) { say("Couldn't make the image"); }
      };
      img.onerror = function () { say("Couldn't make the image"); };
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    },
    pdf: function (s) {
      var host = document.getElementById("sharecard"), title = document.title;
      host.textContent = ""; host.appendChild(card(s)); document.title = stem(s);
      window.addEventListener("afterprint", function once() { document.title = title; window.removeEventListener("afterprint", once); });
      window.print();
    },
    html: function (s) {
      var page = '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Grade Report</title>' +
        "<style>body{margin:0;padding:24px;background:#f2f2f7;display:flex;justify-content:center}</style></head><body>" + card(s).outerHTML + "</body></html>";
      save(new Blob([page], { type: "text/html;charset=utf-8" }), stem(s) + ".html");
    }
  };

  function close(back) { menu.hidden = true; btn.setAttribute("aria-expanded", "false"); if (back) btn.focus(); }
  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    var open = menu.hidden; menu.hidden = !open; btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) menu.querySelector(".mi").focus();
  });
  menu.addEventListener("click", function (e) {
    var item = e.target.closest(".mi"); if (!item) return;
    close(true); actions[item.getAttribute("data-do")](snapshot());
  });
  document.addEventListener("click", function (e) { if (!menu.hidden && !menu.contains(e.target)) close(false); });
  document.addEventListener("keydown", function (e) {
    if (menu.hidden) return;
    if (e.key === "Escape") close(true);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault(); var items = Array.prototype.slice.call(menu.querySelectorAll(".mi")), i = items.indexOf(document.activeElement);
      items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length].focus();
    }
  });
})();
</script>
<script>
/* a short, quiet burst of confetti when every class is an A */
(function () {
  if (document.body.getAttribute("data-cheer") !== "1") return;
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  var c = document.createElement("canvas");
  c.style.cssText = "position:fixed;left:0;top:0;width:100%;height:100%;pointer-events:none;z-index:9";
  document.body.appendChild(c);
  var W = c.width = window.innerWidth, H = c.height = window.innerHeight, ctx = c.getContext("2d");
  var colors = ["#5e5ce6", "#12bcf0", "#fbbf24", "#f43f5e", "#34d399"], bits = [], i;
  for (i = 0; i < 90; i++) bits.push({ x: W / 2 + (Math.random() - .5) * W * .3, y: H * .3, vx: (Math.random() - .5) * 9,
    vy: -Math.random() * 9 - 3, r: Math.random() * 5 + 3, c: colors[i % 5], a: Math.random() * 6.28, va: (Math.random() - .5) * .4 });
  var t0 = null;
  function frame(t) {
    if (t0 === null) t0 = t;
    var age = t - t0;
    ctx.clearRect(0, 0, W, H);
    bits.forEach(function (p) {
      p.vy += .28; p.x += p.vx; p.y += p.vy; p.a += p.va;
      ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.a); ctx.fillStyle = p.c;
      ctx.globalAlpha = Math.max(0, 1 - age / 2600); ctx.fillRect(-p.r, -p.r / 2, p.r * 2, p.r); ctx.restore();
    });
    if (age < 2600) requestAnimationFrame(frame); else c.remove();
  }
  requestAnimationFrame(frame);
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


LAST_REPORT_FILE = "last_report.json"
# Errors where showing older grades wouldn't help or make sense.
NO_SAVED_GRADES_KINDS = {"chrome_missing", "bad_config", "translocated", "needs_setup", "dev"}


def is_straight_a(results):
    """True if every class that has a grade is an A (A+, A or A-), and at least one does."""
    graded = [r["official"] for r in results if r.get("pct") != "N/A"]
    return bool(graded) and all(str(letter).startswith("A") for letter in graded)


def save_last_report(results, use_custom):
    """Keep the newest successful grades, so there's something to show when Schoology can't be reached."""
    try:
        with open(LAST_REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump({"saved": time.time(), "use_custom": use_custom, "results": results}, f)
    except OSError:
        pass


def load_last_report():
    """(results, use_custom, saved_time) from the last successful check, or None."""
    try:
        with open(LAST_REPORT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        results = data["results"]
        if isinstance(results, list) and results:
            return results, bool(data.get("use_custom")), float(data["saved"])
    except Exception:
        pass
    return None


def write_report(results, use_custom, update=None, saved_time=None, previous=None):
    """Write grade_report.html. saved_time: these are older, saved grades (Schoology couldn't be reached)."""
    headers = "<th>Course</th><th>Schoology %</th><th>Official Grade</th>"
    if use_custom:
        headers += "<th>Custom Grade</th>"

    before = {(r.get("url") or r.get("name")): r.get("pct") for r in previous or []}
    rows = ""
    for d in results:
        pct = f"{d['pct']}%" if isinstance(d["pct"], (int, float)) else html.escape(str(d["pct"]))
        old_pct = before.get(d.get("url") or d.get("name"))
        if saved_time is None and isinstance(d["pct"], (int, float)) and isinstance(old_pct, (int, float)):
            change = round(d["pct"] - old_pct, 2)
            if abs(change) >= 0.01:  # what moved since the last check
                up = change > 0
                pct += (f"<span class='delta {'up' if up else 'down'}' title='Since your last check'>"
                        f"{'&#9650;' if up else '&#9660;'}{abs(change):g}</span>")
        name = html.escape(d["name"])
        if d.get("url"):  # click a class to open it on Schoology
            name = f"<a href='{html.escape(d['url'], quote=True)}' target='_blank' rel='noopener' title='Open on Schoology'>{name}</a>"
        rows += (
            f"<tr><td>{name}</td><td>{pct}</td>"
            f"<td class='official'>{html.escape(d['official'])}</td>"
        )
        if use_custom:
            rows += f"<td class='custom'>{html.escape(d['custom'])}</td>"
        rows += "</tr>"

    banner = ""
    if update:
        notes = html.escape((update.get("notes") or "").strip()[:160])
        banner = (
            f"<a class='update' href='{html.escape(update['url'], quote=True)}' target='_blank' rel='noopener'>"
            f"&#11014; Version {html.escape(update['version'])} is available"
            f"{': ' + notes if notes else '.'} Download it</a>"
        )

    notice, checked_at = "", datetime.now()
    if saved_time is not None:
        checked_at = datetime.fromtimestamp(saved_time)
        when = f"{checked_at:%b %d at %I:%M %p}".replace(" 0", " ")
        notice = f"<p class='note'>Couldn't check Schoology just now. These are the grades saved on {when}.</p>"

    shared = {"classes": [
        {"n": r["name"], "o": r.get("official"), "g": r["pct"] if isinstance(r.get("pct"), (int, float)) else None,
         "periods": r.get("periods") or [], "items": r.get("items") or [], "cats": r.get("cats") or []}
        for r in results
    ]}
    data_json = json.dumps(shared, separators=(",", ":")).replace("<", "\\u003c")  # safe inside a <script> tag

    cheer = is_straight_a(results) and saved_time is None
    page = (
        REPORT_TEMPLATE
        .replace("__DARK__", DARK_VARS)
        .replace("__FAVICON__", favicon_data())
        .replace("__CHECKED__", f"{checked_at:%b %d, %Y at %I:%M %p}".replace(" 0", " "))
        .replace("__CHEERFLAG__", "1" if cheer else "0")
        .replace("__CHEER__", "<p class='cheer'>Straight A's! &#127881;</p>" if cheer else "")
        .replace("__NOTICE__", notice)
        .replace("__UPDATE__", banner)
        .replace("__HEADERS__", headers)
        .replace("__ROWS__", rows)
        .replace("__DATA__", data_json)
        .replace("__VERSION__", html.escape(app_version()))
    )
    path = os.path.abspath("grade_report.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


def show_report(path, rows):
    """Open the report in a small bare Chrome window (no tabs, no address bar).
    Falls back to the normal browser if Chrome can't be found."""
    height = max(440, min(900, 310 + 47 * rows))  # room for the update banner too
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

    locked = False
    try:
        config = load_config()
        wait_for_idle(status)  # a scheduled (Autopilot) check may be running: let it finish first
        locked = acquire_lock()
        # Ask (quietly, in the background) whether a newer version has been published.
        update = {}
        checker = threading.Thread(target=lambda: update.update(info=check_for_update()), daemon=True)
        checker.start()
        previous = load_last_report()  # last time's grades, to show what changed
        results, use_custom = scrape_all(config, status)
        status("Generating report card...")
        checker.join(timeout=1.5)
        for result, url in zip(results, dict.fromkeys(config["class_urls"])):
            result["url"] = url  # lets the report link each class to Schoology
        save_last_report(results, use_custom)
        info = update.get("info")
        path = write_report(results, use_custom, info, previous=previous[0] if previous else None)
        root.after(0, lambda: on_ok(path, len(results), info))
    except Exception as e:
        friendly = explain(e)
        if friendly.kind != "needs_setup":  # not really an error: they just haven't set up yet
            log_error(traceback.format_exc())
        root.after(0, lambda f=friendly: on_error(f))
    finally:
        if locked:
            release_lock()


def run_auto():
    """A scheduled check with no window (SchoologyGrades --auto). It saves the grades and a fresh report,
    and sends a notification only when something changed. Returns the exit code."""
    if not acquire_lock():
        auto_log("Skipped: another check was already running")
        return 0
    try:
        settings = read_auto()
        config = load_config()
        previous = load_last_report()
        results, use_custom = scrape_all(config, lambda text: None)
        for result, url in zip(results, dict.fromkeys(config["class_urls"])):
            result["url"] = url
        save_last_report(results, use_custom)
        write_report(results, use_custom, None, previous=previous[0] if previous else None)
        lines = build_alerts(previous[0] if previous else None, results, settings["notify"])
        if lines:
            notify(*compose_notification(lines))
        auto_log(f"Checked {len(results)} classes, " + (f"told you about {len(lines)}" if lines else "nothing new"))
        return 0
    except Exception as e:
        friendly = explain(e)
        if friendly.kind != "needs_setup":
            log_error(traceback.format_exc())
        auto_log("Couldn't check: " + friendly.title)
        if friendly.kind in ("sign_in", "signed_out", "bad_login"):
            settings = read_auto()
            if time.time() - settings.get("last_signin_notice", 0) > 20 * 3600:  # once a day is enough
                notify("Please sign in again", "Open Schoology Grades and choose Sign in. Until then, Autopilot can't check your grades.")
                settings["last_signin_notice"] = time.time()
                write_auto(settings)
        return 1
    finally:
        close_browser()
        release_lock()


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

    def on_ok(path, rows, info=None):
        SHOW_BROWSER[0] = False  # signed in: back to the quiet, hidden browser
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
        if info and not update_prompt_snoozed(info["version"]):
            # Offer the update once the report window has had a moment to appear.
            def prompt():
                if not offer_update(root, info):
                    root.destroy()
            root.after(1300, prompt)
            return
        root.after(900, root.destroy)  # give the report window a moment to appear first

    def show_saved_grades(saved):
        results, use_custom, saved_time = saved
        status_label.config(text="Opening your saved grades...")
        try:
            show_report(write_report(results, use_custom, None, saved_time), len(results))
        except Exception:
            log_error(traceback.format_exc())
        root.after(900, root.destroy)

    def on_error(friendly):
        if friendly.kind == "needs_setup":  # first run: go straight to Setup
            status_label.config(text="Opening Setup...")
            open_setup()
            root.after(600, root.destroy)
            return
        saved = load_last_report()
        if saved and friendly.kind not in NO_SAVED_GRADES_KINDS and "show_saved" not in friendly.actions:
            friendly.actions = friendly.actions + ("show_saved",)
        choice = show_error_dialog(root, friendly, "grades")
        if choice == "try_again":
            start_check()
            return
        if choice == "sign_in":  # one run with Chrome visible, so they can sign in themselves
            SHOW_BROWSER[0] = True
            start_check()
            return
        if choice == "show_saved" and saved:
            show_saved_grades(saved)
            return
        if choice == "open_setup":
            open_setup()
        root.destroy()

    def start_check():
        status_label.config(text="Initializing...")
        threading.Thread(target=worker, args=(root, status_label, on_ok, on_error), daemon=True).start()

    add_help_menu(root)
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
    if "--auto" in sys.argv:
        sys.exit(run_auto())
    main()
