"""Opens the report in real Chromium and clicks through the tabs, grading-period picker and filters.
Needs: pip install playwright && playwright install chromium.   Usage: python tests/test_report_tabs.py
Saves screenshots to a temp folder (printed at the end) so you can look at the design."""
import importlib.machinery, importlib.util, os, shutil, sys, tempfile
from pathlib import Path
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_ORIG = os.environ.get("SG_SOURCE") or os.path.join(HERE, "..", "source")
sys.path.insert(0, HERE)
import make_test_pages as pages_mod  # noqa: E402

tmp = tempfile.mkdtemp()
work = os.path.join(tmp, "SchoologyGrades", "source")
shutil.copytree(SRC_ORIG, work)
os.chdir(work)
sys.path.insert(0, work)
for n in ("tkinter", "tkinter.messagebox", "tkinter.ttk"):
    sys.modules[n] = MagicMock()
loader = importlib.machinery.SourceFileLoader("grades", os.path.join(work, "grades.pyw"))
g = importlib.util.module_from_spec(importlib.util.spec_from_loader("grades", loader))
loader.exec_module(g)
from playwright.sync_api import sync_playwright  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  -> " + str(detail)))
    if not ok:
        failures.append(label)


def result(name, pct, data=None, url=None):
    r = {"name": name, "pct": pct, "official": g.get_official_grade(g.round_half_up(pct)), "custom": "", "url": url or "https://x.schoology.com/course/%d/student_grades" % abs(hash(name) % 999)}
    r.update(g.shape_data(data, False))
    return r


with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()
    served = {}

    def data_of(name):
        page.goto(Path(os.path.join(HERE, "pages", name + ".html")).as_uri())
        return page.evaluate("(js) => new Function(js)()", g.DATA_JS)

    algebra = result("Demo Algebra", 84.5, data_of("summary_two_quarters"), "https://x.schoology.com/course/1/student_grades")
    health = result("Demo Health", 97.39, data_of("summary_one_quarter"), "https://x.schoology.com/course/2/student_grades")
    previous = [dict(algebra, pct=83.0), dict(health, pct=97.39)]

    def render(name, results, **kw):
        path = os.path.abspath(name + ".html")
        os.replace(g.write_report(results, False, **kw), path)
        return Path(path).as_uri()

    print("Report with tabs")
    for theme in ("light", "dark"):
        ctx = browser.new_context(viewport={"width": 700, "height": 640}, color_scheme=theme)
        pg = ctx.new_page()
        pg.goto(render("tabs", [algebra, health], previous=previous))
        check("[%s] Grades tab is showing first, with Semester picked" % theme, pg.locator("#grades").is_visible() and not pg.locator("#assign").is_visible()
              and pg.locator("#scope button[aria-pressed=true]").inner_text() == "Semester")
        check("[%s] grade change since last check is marked" % theme, pg.locator("#grades tbody tr").first.locator(".delta.up").inner_text() == "\u25b21.5" and pg.locator("#grades .delta").count() == 1, pg.locator("#grades .delta").all_inner_texts())
        pg.screenshot(path=os.path.join(tmp, "grades_%s.png" % theme))
        pg.click("#scope button:has-text('Q1')")
        cells = pg.locator("#grades tbody tr").first.locator("td").all_inner_texts()
        check("[%s] Q1 picker shows that quarter's grade" % theme, cells[1:3] == ["77%", g.get_official_grade(77)] and pg.locator("#grades .delta").count() == 0, cells)
        pg.click("#scope button:has-text('Semester')")
        check("[%s] back to Semester restores the grade" % theme, pg.locator("#grades tbody tr").first.locator("td").all_inner_texts()[1].startswith("84.5%"))
        pg.click("#t-assign")
        check("[%s] Assignments tab opens" % theme, pg.locator("#assign").is_visible() and not pg.locator("#grades").is_visible())
        names = pg.locator(".arow").all_inner_texts()
        check("[%s] 'Needs attention' lists a low score, a missing item and an overdue ungraded one, not future work" % theme,
              len(names) == 3 and any("Unit 1 Test" in n and "55 / 100" in n for n in names) and any("Worksheet A" in n and "Not graded" in n for n in names)
              and any("Lab write-up" in n and "Missing" in n for n in names), names)
        pg.screenshot(path=os.path.join(tmp, "assign_%s.png" % theme))
        pg.click("#scope button:has-text('Q1')")
        check("[%s] Q1 narrows the list" % theme, pg.locator(".arow").count() == 1, pg.locator(".arow").all_inner_texts())
        pg.click("#scope button:has-text('Semester')")
        pg.click("#view button:has-text('All')")
        check("[%s] 'All' shows every assignment" % theme, pg.locator(".arow").count() == 7, pg.locator(".arow").count())
        pg.click("#t-stats")
        check("[%s] Stats tab: sentence, one bar per class, score spread" % theme,
              "Your average is" in pg.locator(".lede").inner_text() and pg.locator("svg.bars rect.fill").count() == 2 and pg.locator(".stack span").count() >= 1)
        pg.screenshot(path=os.path.join(tmp, "stats_%s.png" % theme))
        pg.click("#scope button:has-text('Q2')")
        check("[%s] Stats follow the period picker (only one class has a Q2 grade)" % theme, pg.locator("svg.bars rect.fill").count() == 1, pg.locator("svg.bars rect.fill").count())
        pg.click("#scope button:has-text('Semester')")
        pg.click("#t-calc")
        check("[%s] Calculator: GPA estimate" % theme, "Estimated GPA" in pg.locator(".result").first.inner_text(), pg.locator(".result").first.inner_text())
        pg.locator(".gpa input").first.fill("2")
        pg.screenshot(path=os.path.join(tmp, "calc_%s.png" % theme))
        pg.locator(".form input").nth(0).fill("10")
        pg.locator(".form input").nth(1).fill("10")
        out = pg.locator(".result").nth(1).inner_text()
        check("[%s] What-if: a 10/10 on classwork lifts the grade as worked out by hand" % theme, "84.5% to 86.1%" in out and "+1.6" in out, out)
        pg.click(".form .seg button:has-text('What do I need')")
        pg.locator(".form input").nth(0).fill("90")
        out = pg.locator(".result").nth(1).inner_text()
        check("[%s] What-if: needed score for a 90%% (by hand: 11.95 of 10)" % theme, "11.95 out of 10" in out, out)
        ctx.close()

    print("Reports without that data")
    plain = [{"name": "Algebra 2", "pct": 94.62, "official": "A", "custom": ""}]
    ctx = browser.new_context(viewport={"width": 700, "height": 560})
    pg = ctx.new_page()
    pg.goto(render("plain", plain))
    check("no periods or assignments: table still there, no Assignments tab, calculator can still do GPA",
          pg.locator("#grades tbody tr").count() == 1 and not pg.locator("#t-assign").is_visible() and pg.locator("#t-calc").is_visible())
    pg.click("#t-calc")
    check("what-if says it isn't available without category data", "Not available yet" in pg.locator("#calcbox").inner_text())
    pg.goto(render("tricky", [result("</script><b>x", 90.0, {"periods": [], "items": [{"n": "<img src=x onerror=alert(1)>", "d": "", "s": None, "m": None, "c": "", "q": ""}]})]))
    pg.click("#t-assign"); pg.click("#view button:has-text('All')")
    check("odd characters in names stay text", pg.locator(".arow img").count() == 0 and "<img" in pg.locator(".arow").first.inner_text())
    ctx.close()
    browser.close()

print("screenshots in", tmp)
print("ALL PASSED" if not failures else "FAILED: %s" % failures)
sys.exit(1 if failures else 0)
