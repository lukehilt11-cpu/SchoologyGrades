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

pages_mod.main()  # fresh pages: their dates are relative to today
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
        check("[%s] items older than a month are left out, with a note saying so" % theme,
              not any("Old lab" in n or "Warm-up" in n for n in pg.locator(".arow").all_inner_texts()) and "more than a month ago" in pg.locator("#alist").inner_text())
        pg.click(".arow:has-text('Worksheet A') .linkbtn")
        check("[%s] Dismiss removes an item from Needs attention" % theme, pg.locator(".arow").count() == 2 and "Show dismissed (1)" in pg.locator("#alist").inner_text(), pg.locator(".arow").all_inner_texts())
        pg.reload(); pg.click("#t-assign")
        check("[%s] ...and it stays dismissed after the report is opened again" % theme, pg.locator(".arow").count() == 2)
        pg.click("#alist .linkbtn:has-text('Show dismissed')")
        check("[%s] Show dismissed lists it with a Restore button" % theme, pg.locator(".arow").count() == 3 and pg.locator(".arow:has-text('Worksheet A') .linkbtn").inner_text() == "Restore")
        pg.click(".arow:has-text('Worksheet A') .linkbtn")
        check("[%s] Restore brings it back" % theme, pg.locator(".arow").count() == 3 and "Show dismissed" not in pg.locator("#alist").inner_text())
        pg.click("#view button:has-text('All')")
        check("[%s] 'All' shows every assignment" % theme, pg.locator(".arow").count() == 8, pg.locator(".arow").count())
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

    print("Share")
    ctx = browser.new_context(viewport={"width": 700, "height": 640}, accept_downloads=True)
    ctx.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: {writeText: function (t) { window.__copied = t; return Promise.resolve(); }}, configurable: true});")
    pg = ctx.new_page()
    pg.goto(render("share", [algebra, health], previous=previous))
    pg.click("#share")
    items = pg.locator("#sharemenu .mi").all_inner_texts()
    check("Share opens a menu with text, image, PDF and HTML", items == ["Copy text summary", "Save as image (PNG)", "Save as PDF\u2026", "Save as HTML"], items)
    pg.screenshot(path=os.path.join(tmp, "share_menu.png"))
    pg.keyboard.press("Escape")
    check("Escape closes it", pg.locator("#sharemenu").is_hidden())
    pg.click("#share"); pg.click("h2")
    check("clicking elsewhere closes it", pg.locator("#sharemenu").is_hidden())
    pg.click("#share"); pg.click("#sharemenu .mi >> nth=0")
    copied = pg.evaluate("window.__copied") or ""
    check("text summary: title, date, one line per class, no change chips", copied.splitlines()[0] == "Grade Report (Semester)" and copied.splitlines()[1].startswith("Checked ")
          and "Demo Algebra: 84.5%  B" in copied and "Demo Health: 97.39%  A" in copied and "\u25b2" not in copied, copied)
    check("...and a confirmation appears", "Copied" in pg.locator("#toast").inner_text())
    pg.click("#scope button:has-text('Q1')"); pg.click("#share"); pg.click("#sharemenu .mi >> nth=0")
    copied = pg.evaluate("window.__copied")
    check("sharing follows the period picker", copied.splitlines()[0] == "Grade Report (Q1)" and "Demo Algebra: 77%  " + g.get_official_grade(77) in copied, copied)
    pg.click("#scope button:has-text('Semester')")

    from PIL import Image
    pg.click("#share")
    with pg.expect_download() as dl:
        pg.click("#sharemenu .mi >> nth=1")
    png_path = os.path.join(tmp, "share.png"); dl.value.save_as(png_path)
    im = Image.open(png_path).convert("RGB")
    dark = sum(1 for px in im.getdata() if px != (255, 255, 255)) / (im.width * im.height)
    check("image: a real 2x PNG with content, named by date", im.width == 1200 and im.height > 300 and 0.03 < dark < 0.6 and dl.value.suggested_filename.startswith("Grades-") and dl.value.suggested_filename.endswith(".png"),
          (im.size, dark, dl.value.suggested_filename))
    im.save(os.path.join(tmp, "share_image.png"))

    pg.click("#share")
    with pg.expect_download() as dl:
        pg.click("#sharemenu .mi >> nth=3")
    html_path = os.path.join(tmp, "share.html"); dl.value.save_as(html_path)
    saved_html = open(html_path, encoding="utf-8").read()
    check("HTML copy: self-contained, no scripts, no change chips", "Demo Algebra" in saved_html and "84.5%" in saved_html and "<script" not in saved_html and "\u25b2" not in saved_html)
    pg2 = ctx.new_page(); pg2.goto(Path(html_path).as_uri())
    check("...and opens as a clean page with a row per class", pg2.locator("tbody tr").count() == 2 and pg2.locator(".theme, #share").count() == 0)
    pg2.close()

    pg.evaluate("window.print = function () { window.__printed = true; }")
    pg.click("#share"); pg.click("#sharemenu .mi >> nth=2")
    check("PDF: asks the browser to print a clean copy", pg.evaluate("window.__printed") is True and pg.locator("#sharecard tbody tr").count() == 2)
    pg.emulate_media(media="print")
    check("PDF: when printing, only the clean copy shows", pg.locator(".card").is_hidden() and pg.locator("#sharecard").is_visible() and pg.locator("#share").is_hidden())
    pdf = pg.pdf()
    check("PDF: a real one-page PDF comes out", pdf[:4] == b"%PDF" and len(pdf) > 2000)
    pg.emulate_media(media="screen")
    check("the clean copy never shows on screen", pg.locator("#sharecard").is_hidden())
    ctx.close()

    ctx = browser.new_context(viewport={"width": 700, "height": 560})
    ctx.add_init_script("Object.defineProperty(navigator, 'clipboard', {value: {writeText: function (t) { window.__copied = t; return Promise.resolve(); }}, configurable: true});")
    pg = ctx.new_page(); pg.goto(render("share_plain", [{"name": "Algebra 2", "pct": 94.62, "official": "A", "custom": ""}]))
    pg.click("#share"); pg.click("#sharemenu .mi >> nth=0")
    check("a report with no extra data can be shared too", "Algebra 2: 94.62%  A" in (pg.evaluate("window.__copied") or ""))
    ctx.close()

    print("Grading periods in page order")
    only_q2 = result("Only Q2", 96.0, {"periods": [{"n": "26-27 Q1", "w": 50, "p": None}, {"n": "26-27 Q2", "w": 50, "p": 96.0}], "items": []})
    both = result("Both", 90.0, {"periods": [{"n": "26-27 Q1", "w": 50, "p": 88.0}, {"n": "26-27 Q2", "w": 50, "p": 92.0}], "items": []})
    ctx = browser.new_context(viewport={"width": 700, "height": 560})
    pg = ctx.new_page()
    pg.goto(render("order", [only_q2, both]))
    check("picker lists Semester, Q1, Q2 even when the first class only has Q2", pg.locator("#scope button").all_inner_texts() == ["Semester", "Q1", "Q2"], pg.locator("#scope button").all_inner_texts())
    pg.click("#t-stats")
    check("Quarter to quarter columns also run Q1 then Q2", pg.locator(".qt th").all_inner_texts() == ["Class", "Q1", "Q2", "Change"], pg.locator(".qt th").all_inner_texts())
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
