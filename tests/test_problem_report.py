"""Checks the "Report a problem" file: what goes in, what is blanked, and that nothing private leaks.
Usage: python tests/test_problem_report.py      (SG_SOURCE=/path/to/source to test a different copy)"""
import importlib.machinery, importlib.util, json, os, re, shutil, sys, tempfile, zipfile
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_ORIG = os.environ.get("SG_SOURCE") or os.path.join(HERE, "..", "source")
sys.path.insert(0, HERE)
import make_test_pages as pages_mod  # noqa: E402

pages_mod.main()
tmp = tempfile.mkdtemp()
home = os.path.join(tmp, "home"); os.makedirs(os.path.join(home, "Desktop"))
os.environ["HOME"] = home; os.environ["USERPROFILE"] = home
work = os.path.join(tmp, "app", "source"); shutil.copytree(SRC_ORIG, work)
os.chdir(work); sys.path.insert(0, work)
for n in ("tkinter", "tkinter.messagebox", "tkinter.ttk"):
    sys.modules[n] = MagicMock()
loader = importlib.machinery.SourceFileLoader("grades", os.path.join(work, "grades.pyw"))
g = importlib.util.module_from_spec(importlib.util.spec_from_loader("grades", loader)); loader.exec_module(g)
common = sys.modules["sgcommon"]

failures = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  -> " + str(detail)))
    if not ok:
        failures.append(label)


print("Scrubbing a page")
html = pages_mod.CASES["summary_two_quarters"][0].replace("Demo Algebra", "Mrs Jones Secret Class")
clean = common.scrub_html(html)
check("names and titles are blanked", "Jones" not in clean and "Secret" not in clean and "Warm-up" not in clean and "Worksheet" not in clean)
check("scores are blanked (digits become 0), ids kept", not re.search(r">\s*[1-9]", clean) and 'data-id="I-' in clean and 'class="report-row period-row"' in clean)
check("words that describe the page are kept ('Missing', 'Due')", 'title="Missing"' in clean and ">Due " in clean)
check("scripts, head and links are gone", "<script" not in clean and "<title" not in clean and 'href=""' in clean)
kept = common.scrub_html(html, keep_numbers=True)
check("with grades included, numbers stay but names don't", "84.5" in kept and "Jones" not in kept)

print("Making the file")
data = common.DATA_DIR
os.makedirs(os.path.join(data, "pages"), exist_ok=True)
open(os.path.join(data, "pages", "class_1_1.html"), "w", encoding="utf-8").write(html)
open(os.path.join(data, "config.json"), "w").write(json.dumps({"email": "kid@school.org", "password": "hunter2", "headless": True, "courses": ["a", "b"]}))
open(os.path.join(data, "layouts.json"), "w").write(json.dumps({"classes": {"9000001": {"layout": "summary", "runs": 0}}}))
open(os.path.join(data, "error_log.txt"), "w").write("[2026-11-15]\nTraceback in C:\\Users\\Sam Student\\app\nkid@school.org could not sign in\n")
open(os.path.join(data, "last_report.json"), "w").write(json.dumps({"saved": 1, "use_custom": False, "results": [
    {"name": "Mrs Jones Secret Class", "pct": 84.5, "url": "https://x.schoology.com/course/9000001/student_grades", "periods": [{}, {}], "items": [{}] * 3, "cats": [{}]}]}))
path, notes = common.make_problem_report(False)
check("the file is on the Desktop", os.path.dirname(path) == os.path.join(home, "Desktop") and path.endswith(".zip"), path)
z = zipfile.ZipFile(path)
text = "\n".join(z.read(n).decode("utf-8", "replace") for n in z.namelist())
check("it holds about, layouts, log and the scrubbed page", {"about.txt", "layouts.json", "error_log.txt", "what-the-program-read.json", "pages/class_1_1.html"} <= set(z.namelist()), z.namelist())
check("no password, email, name, class name or grade anywhere", not any(w in text for w in ("hunter2", "kid@school.org", "Sam Student", "Jones", "Secret", "84.5")), [w for w in ("hunter2", "kid@school.org", "Sam Student", "Jones", "Secret", "84.5") if w in text])
check("the error log is still readable", "<email> could not sign in" in text and "Users\\USER" in text)
shape = json.loads(z.read("what-the-program-read.json"))
check("it says what the program read without naming the class", shape == [{"layout": "summary", "has_grade": True, "periods": 2, "assignments": 3, "categories": 1}], shape)
check("no config.json or profile is included", not any(n.startswith(("config", "selenium", "report_profile", "debug")) for n in z.namelist()))
shutil.rmtree(os.path.join(data, "pages"))
path2, notes2 = common.make_problem_report(True)
check("with no saved pages it explains how to add them", notes2 and "Save class pages" in notes2[0], notes2)

print()
print("ALL PASSED" if not failures else "FAILED: %s" % failures)
sys.exit(1 if failures else 0)
