"""Checks that every class-page layout is read correctly, and that remembered layouts behave.

Needs:  pip install playwright && playwright install chromium
Usage:  python tests/test_layouts.py        (SG_SOURCE=/path/to/source to test a different copy)
Uses only the made-up pages in tests/pages/ (run tests/make_test_pages.py first if they're missing).
"""
import http.server, importlib.machinery, importlib.util, json, os, shutil, sys, tempfile, threading, time
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_ORIG = os.environ.get("SG_SOURCE") or os.path.join(HERE, "..", "source")
sys.path.insert(0, HERE)
import make_test_pages as pages_mod  # noqa: E402

pages_mod.main()
tmp = tempfile.mkdtemp()
work = os.path.join(tmp, "SchoologyGrades", "source")
shutil.copytree(SRC_ORIG, work)
os.chdir(work)  # the program keeps its files in the current folder
sys.path.insert(0, work)
for n in ("tkinter", "tkinter.messagebox", "tkinter.ttk"):
    sys.modules[n] = MagicMock()  # no Tk needed here
loader = importlib.machinery.SourceFileLoader("grades", os.path.join(work, "grades.pyw"))
spec = importlib.util.spec_from_loader("grades", loader)
g = importlib.util.module_from_spec(spec)
loader.exec_module(g)

failures = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  -> " + str(detail)))
    if not ok:
        failures.append(label)


def pick(item):
    """The same choice the program makes: the grade line if there is one, else the fallback."""
    badge = item.get("grade") if item.get("grade") is not None else item.get("fallback")
    return g.badge_pct(badge)


# ---- 1. real Chromium: both ways of reading a page -------------------------------------------
print("Reading each made-up page (live page and direct-HTML shortcut)")
from playwright.sync_api import sync_playwright  # noqa: E402

pages_dir = os.path.join(HERE, "pages")
class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=pages_dir))
threading.Thread(target=server.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d/" % server.server_address[1]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    for name, (_html, (want_layout, want_pct)) in pages_mod.CASES.items():
        page.goto(base + name + ".html")
        live = page.evaluate(
            "([js, a, b, c, d, e]) => new Function(js)(a, b, c, d, e)",
            [g.PROBE_JS, g.GRADE_SEL, g.GRADE_FALLBACK_SEL, g.TITLE_SEL, g.EMAIL_SEL, g.PASS_SEL],
        )
        got = pick(live)
        check("%-24s live page   -> %s %s" % (name, live["layout"], got),
              live["layout"] == want_layout and (got == want_pct or (got is not None and want_pct is not None and abs(got - want_pct) < 0.006)),
              "wanted %s %s" % (want_layout, want_pct))
    # The direct-HTML shortcut: fetch every page from one tab and read them without drawing.
    page.goto(base + "summary_two_quarters.html")
    urls = [base + n + ".html" for n in pages_mod.CASES]
    items = page.evaluate(
        "([js, urls, a, b, c]) => new Promise(done => new Function(js)(urls, a, b, c, done))",
        [g.FETCH_JS, urls, g.GRADE_SEL, g.GRADE_FALLBACK_SEL, g.TITLE_SEL],
    )
    for (name, (_h, (want_layout, want_pct))), item in zip(pages_mod.CASES.items(), items):
        got = pick(item)
        check("%-24s HTML shortcut -> %s %s" % (name, item.get("layout"), got),
              item.get("layout") == want_layout and (got == want_pct or (got is not None and want_pct is not None and abs(got - want_pct) < 0.006)),
              "wanted %s %s" % (want_layout, want_pct))
    browser.close()
server.shutdown()

# ---- 2. remembered layouts: a stand-in browser that just replays what each tab "shows" ------
print("Remembering layouts")


class FakeDriver:
    def __init__(self, states):
        self.states, self.current = states, None
        self.switch_to = self

    def window(self, handle):
        self.current = handle

    def execute_script(self, *a, **k):
        return dict(self.states[self.current])

    def save_screenshot(self, *a, **k):
        return True


HOST, CID = "x.schoology.com", "9000004"
g.SETTLE_SECONDS = 0.4


def drawn(layout, grade=None, fallback=None):
    return {"host": HOST, "path": "/course/%s/student_grades" % CID, "ready": "complete", "login": False,
            "title": "Demo English: DEMO E - 4", "grade": grade, "fallback": fallback, "layout": layout}


def read(states, memory):
    driver = FakeDriver({"h": states})
    start = time.time()
    results, left = g.read_courses(driver, ["h"], {CID: 1}, HOST, False, lambda i: None, layouts=memory)
    return results[1], time.time() - start, left


calc = ["96%", "96%"]
os.chdir(tmp)
mem = g.LayoutMemory(os.path.join(tmp, "layouts.json"))

r, t, left = read(drawn("periods", None, calc), mem)
check("first time: waits, then reads the calculated grade", r["pct"] == 96 and t >= 0.35, (r, t))
check("  ...and remembers 'periods'", mem.data[CID] == {"layout": "periods", "runs": 0}, mem.data)

r, t, left = read(drawn("periods", None, calc), mem)
check("known 'periods' class: no waiting", r["pct"] == 96 and t < 0.3, (r, t))
check("  ...and counts the run", mem.data[CID]["runs"] == 1, mem.data)

mem.data[CID]["runs"] = g.RECHECK_EVERY
r, t, left = read(drawn("periods", None, calc), mem)
check("re-check due: waits and looks carefully", t >= 0.35, t)
check("  ...and resets the counter", mem.data[CID]["runs"] == 0, mem.data)

r, t, left = read(drawn("summary", ["99.5%", "99.5%"], None), mem)
check("teacher turned the cumulative grade on: new layout noticed", mem.data[CID]["layout"] == "summary" and r["pct"] == 99.5, (mem.data, r))

r, t, left = read(drawn("periods", None, calc), mem)
check("known 'summary' class that stops showing it: still read, then re-diagnosed", r["pct"] == 96 and mem.data[CID]["layout"] == "periods" and t >= 0.35, (mem.data, r, t))

r, t, left = read(drawn(None, None, None), mem)
check("page with nothing to read: N/A and the class is forgotten", r["pct"] == "N/A" and CID not in mem.data, (r, mem.data))

mem.record(CID, "periods", True)
mem.save()
again = g.LayoutMemory(os.path.join(tmp, "layouts.json"))
check("saved to layouts.json and loaded back", again.data == mem.data, again.data)
open(os.path.join(tmp, "layouts.json"), "w").write("{not json")
check("a damaged layouts.json is ignored", g.LayoutMemory(os.path.join(tmp, "layouts.json")).data == {})

# ---- 3. the friendly sign-in messages ---------------------------------------------------------
print("Sign-in messages")
common = sys.modules["sgcommon"]
fe = common.explain(RuntimeError("Login did not complete in time"))
check("rejected sign-in offers 'Sign in' first", fe.kind == "sign_in" and fe.actions[0] == "sign_in" and "open_setup" in fe.actions, (fe.kind, fe.actions))
fe = common.explain(g.LoginNeeded("Schoology asked for a login again partway through. Try running again."))
check("signed out mid-check offers Try again and Sign in", fe.kind == "signed_out" and fe.actions == ("try_again", "sign_in"), (fe.kind, fe.actions))

print()
print("ALL PASSED" if not failures else "FAILED: %d -> %s" % (len(failures), failures))
sys.exit(1 if failures else 0)
