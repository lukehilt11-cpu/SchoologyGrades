"""Opens the real Autopilot window (needs a display; skips itself if there isn't one) and works its controls.
Usage: python tests/test_autopilot_window.py      (on a server: xvfb-run -a python tests/test_autopilot_window.py)"""
import json, os, shutil, sys, tempfile
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_ORIG = os.environ.get("SG_SOURCE") or os.path.join(HERE, "..", "source")
tmp = tempfile.mkdtemp()
work = os.path.join(tmp, "app", "source"); shutil.copytree(SRC_ORIG, work)
os.chdir(work); sys.path.insert(0, work)
import tkinter as tk  # noqa: E402
try:
    root = tk.Tk()
except tk.TclError:
    print("SKIPPED: no display available"); sys.exit(0)
import autopilot as ap  # noqa: E402
import sgcommon as sg  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  -> " + str(detail)))
    if not ok:
        failures.append(label)


shown = []
box = MagicMock(showinfo=lambda *a, **k: shown.append(a[1]), showerror=lambda *a, **k: shown.append(a[1]))
with patch.object(ap, "messagebox", box):
    app = ap.AutopilotApp(root)
    check("opens switched off, weekdays, one time", app.status.get() == "Autopilot is off." and [v.get() for v in app.days] == [True] * 5 + [False] * 2 and app.times[0].get() == "3:30 PM")
    scheduled = []
    with patch.object(sg, "install_schedule", lambda s: scheduled.append(s) or (True, "ok")), patch.object(sg, "remove_schedule", lambda: (True, "ok")), \
            patch.object(sg, "schedule_installed", lambda: bool(scheduled)):
        app.enabled.set(True)
        app.save()
        check("saving with no classes set up asks for Setup first", "Setup" in shown[-1] and not scheduled, shown)
        open("config.json", "w").write(json.dumps({"class_urls": ["u"]}))
        for v in app.days: v.set(False)
        app.save()
        check("no days picked: asks for one", "one day" in shown[-1] and not scheduled)
        for i in (0, 2, 4): app.days[i].set(True)
        app.times[1].set("7:30 AM"); app.times[2].set("3:30 PM")
        app.on_new.set(False); app.low_below.set("72")
        app.save()
        saved = json.load(open(sg.AUTO_FILE))
        check("saved: days, sorted times (duplicates folded), notify options", saved["enabled"] and saved["days"] == [0, 2, 4] and saved["times"] == ["07:30", "15:30"]
              and saved["notify"]["new_grades"] is False and saved["notify"]["low_below"] == 72, saved)
        check("saved: the schedule was created and the status says when", len(scheduled) == 1 and app.status.get().startswith("Autopilot is on. Next check"), (scheduled, app.status.get()))
        app.enabled.set(False); app.save()
        check("switching off removes the schedule", json.load(open(sg.AUTO_FILE))["enabled"] is False and app.status.get() == "Autopilot is off.")
        app.low_below.set("abc")
        check("a nonsense limit falls back to 80", app.gather()["notify"]["low_below"] == 80)
    with patch.object(sg, "notify", lambda t, m: False):
        app.test_notification()
        check("a notification that doesn't show explains why", "Notifications" in shown[-1] or "notifications" in shown[-1], shown[-1])
root.destroy()
print()
print("ALL PASSED" if not failures else "FAILED: %s" % failures)
sys.exit(1 if failures else 0)
