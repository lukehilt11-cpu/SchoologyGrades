"""Checks Autopilot: settings, change alerts, the Windows and Mac schedules, notifications and the quiet check.
Usage: python tests/test_autopilot.py      (SG_SOURCE=/path/to/source to test a different copy)
Nothing is really scheduled or shown: the operating system calls are replaced by recorders."""
import importlib.machinery, importlib.util, json, os, plistlib, shutil, sys, tempfile, time
import xml.etree.ElementTree as ET
from datetime import datetime
from unittest.mock import MagicMock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_ORIG = os.environ.get("SG_SOURCE") or os.path.join(HERE, "..", "source")
tmp = tempfile.mkdtemp()
home = os.path.join(tmp, "home"); os.makedirs(home)
os.environ["HOME"] = home; os.environ["USERPROFILE"] = home
work = os.path.join(tmp, "app", "source"); shutil.copytree(SRC_ORIG, work)
os.chdir(work); sys.path.insert(0, work)
for n in ("tkinter", "tkinter.messagebox", "tkinter.ttk"):
    sys.modules[n] = MagicMock()
loader = importlib.machinery.SourceFileLoader("grades", os.path.join(work, "grades.pyw"))
g = importlib.util.module_from_spec(importlib.util.spec_from_loader("grades", loader)); loader.exec_module(g)
sg = sys.modules["sgcommon"]

failures = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label + ("" if ok else "  -> " + str(detail)))
    if not ok:
        failures.append(label)


print("Settings")
check("no file: defaults (off, weekdays, 3:30 PM)", sg.read_auto() == dict(sg.AUTO_DEFAULTS), sg.read_auto())
open(sg.AUTO_FILE, "w").write(json.dumps({"enabled": 1, "days": [1, 1, 9, "x", 6], "times": ["07:30", "7:30", "25:00", "07:30"],
                                          "notify": {"low_below": 500, "changes": 0, "bogus": 1}}))
s = sg.read_auto()
check("odd values are cleaned up", s["enabled"] is True and s["days"] == [1, 6] and s["times"] == ["07:30"] and s["notify"]["low_below"] == 80 and s["notify"]["changes"] is False, s)
open(sg.AUTO_FILE, "w").write("{broken")
check("a damaged file gives the defaults", sg.read_auto() == dict(sg.AUTO_DEFAULTS))
settings = {"enabled": True, "days": [0, 2], "times": ["07:30", "15:30"], "notify": dict(sg.AUTO_DEFAULTS["notify"])}
sg.write_auto(settings)
check("saved settings load back the same", sg.read_auto() == settings)
check("12-hour times", [sg.format_time12(t) for t in ("00:05", "07:30", "12:00", "15:30", "23:45")] == ["12:05 AM", "7:30 AM", "12:00 PM", "3:30 PM", "11:45 PM"])
mon = datetime(2026, 11, 16, 8, 0)  # a Monday
check("next check: later today", sg.next_run(settings, mon) == datetime(2026, 11, 16, 15, 30), sg.next_run(settings, mon))
check("next check: skips days that aren't picked", sg.next_run(settings, datetime(2026, 11, 16, 16, 0)) == datetime(2026, 11, 18, 7, 30))
check("next check: wraps to next week", sg.next_run(settings, datetime(2026, 11, 18, 16, 0)) == datetime(2026, 11, 23, 7, 30))
check("nothing scheduled with no days", sg.next_run(dict(settings, days=[])) is None)

print("What changed")
def res(name, pct, items=(), url=None):
    return {"name": name, "pct": pct, "official": "A", "custom": "", "url": url or "u/" + name, "items": list(items)}
def item(n, s, x=0, d="2026-11-10"): return {"n": n, "d": d, "c": "Classwork", "s": s, "m": 10, "x": x}
base = [res("Algebra", 99.3, [item("HW1", 10), item("HW2", None)]), res("Band", 91.0, [item("A", 9)])]
check("first ever check: nothing to compare", sg.build_alerts(None, base) == [])
check("nothing changed: no alerts", sg.build_alerts(base, base) == [])
now = [res("Algebra", 100.82, [item("HW1", 10), item("HW2", 8), item("HW3", 10)]), res("Band", 91.0, [item("A", 9)])]
lines = sg.build_alerts(base, now)
check("a change and new grades are reported", lines == ["Algebra: 99.3% \u2192 100.82% (+1.52); 2 new grades"], lines)
check("settings can switch parts off", sg.build_alerts(base, now, {"changes": False, "new_grades": False}) == [])
drop = [res("Algebra", 79.5, base[0]["items"]), base[1]]
check("dropping below the limit says so (once, when it crosses)", sg.build_alerts([res("Algebra", 82.0, base[0]["items"]), base[1]], drop) ==
      ["Algebra: 82% \u2192 79.5% (\u22122.5); dropped below 80%"] and "below" not in " ".join(sg.build_alerts([res("Algebra", 78.0, base[0]["items"]), base[1]], drop)))
miss = [res("Algebra", 99.3, base[0]["items"] + [item("Lab", None, 1)]), base[1]]
check("a newly missing assignment is reported, an old one is not", sg.build_alerts(base, miss) == ["Algebra: missing: Lab"] and sg.build_alerts(miss, miss) == [])
old = [{"name": "Algebra", "pct": 99.3, "url": "u/Algebra"}]  # a report saved before assignments were kept
check("an older saved report doesn't cause a flood of 'new grades'", sg.build_alerts(old, now) == ["Algebra: 99.3% \u2192 100.82% (+1.52)"], sg.build_alerts(old, now))
check("new classes are ignored, not alarming", sg.build_alerts(base, now + [res("Art", 80, [item("x", 5)])]) == lines)
title, body = sg.compose_notification(["a", "b", "c", "d", "e", "f"])
check("long notifications are cut short", title == "Grade updates (6 classes)" and body.splitlines()[-1] == "and 2 more" and len(body.splitlines()) == 5, (title, body))
check("one line gets a plain title", sg.compose_notification(["a"]) == ("Grade update", "a"))

print("Schedules")
command = [r"C:\Program Files\Schoology & Co\SchoologyGrades.exe", "--auto"]
root = ET.fromstring(sg.windows_task_xml(settings, command).replace('encoding="UTF-16"', ""))
ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
triggers = root.findall(".//t:CalendarTrigger", ns)
check("Windows: one trigger per time, on the chosen days", len(triggers) == 2 and [e.tag.split("}")[1] for e in triggers[0].find(".//t:DaysOfWeek", ns)] == ["Monday", "Wednesday"]
      and [t.find("t:StartBoundary", ns).text[-8:] for t in triggers] == ["07:30:00", "15:30:00"])
check("Windows: catches up when the computer was off, runs on battery", root.find(".//t:StartWhenAvailable", ns).text == "true" and root.find(".//t:DisallowStartIfOnBatteries", ns).text == "false")
check("Windows: path with spaces and '&' is intact", root.find(".//t:Command", ns).text == command[0] and root.find(".//t:Arguments", ns).text == "--auto")
plist = plistlib.loads(sg.launch_agent_plist(dict(settings, days=[0, 6]), ["/Applications/SchoologyGrades.app/Contents/MacOS/SchoologyGrades", "--auto"]))
check("Mac: Monday is 1 and Sunday is 0 for launchd", sorted((e["Weekday"], e["Hour"], e["Minute"]) for e in plist["StartCalendarInterval"]) ==
      [(0, 7, 30), (0, 15, 30), (1, 7, 30), (1, 15, 30)] and plist["ProgramArguments"][-1] == "--auto" and plist["Label"] == sg.LAUNCH_LABEL, plist)

calls = []
def fake_run(args, timeout=30, env=None):
    calls.append(list(args))
    if args[:2] == ["schtasks", "/Create"]:
        calls[-1].append(open(args[args.index("/XML") + 1], encoding="utf-16").read()[:40])
    return MagicMock(returncode=0, stderr=b"")
with patch.object(sg, "_run", fake_run), patch.object(sg, "auto_command", lambda: command):
    with patch.object(sg.os, "name", "nt"):
        ok, msg = sg.install_schedule(settings)
        check("Windows install: asks Task Scheduler to create the task from a file", ok and calls[-1][:5] == ["schtasks", "/Create", "/TN", sg.TASK_NAME, "/XML"] and calls[-1][-2:][0] == "/F" and "<?xml" in calls[-1][-1], calls)
        remove = sg.remove_schedule()
        check("Windows remove: deletes the task", remove[0] and calls[-1] == ["schtasks", "/Delete", "/TN", sg.TASK_NAME, "/F"], calls[-1])
    calls.clear()
    with patch.object(sg.sys, "platform", "darwin"):
        ok, msg = sg.install_schedule(settings)
        path = os.path.join(home, "Library", "LaunchAgents", sg.LAUNCH_LABEL + ".plist")
        check("Mac install: writes the launch agent and loads it", ok and os.path.isfile(path) and calls[-1][:2] == ["launchctl", "bootstrap"] and calls[0][1] == "bootout", calls)
        check("Mac: reports it as installed", sg.schedule_installed())
        sg.remove_schedule()
        check("Mac remove: unloads it and deletes the file", not os.path.exists(path) and calls[-1][1] == "bootout")
    check("nothing to schedule: says so", sg.install_schedule(dict(settings, days=[]))[0] is False)
    with patch.object(sg, "auto_command", lambda: None):
        check("can't find the program: says so", sg.install_schedule(settings)[0] is False)
check("the quiet command from source points at grades.pyw", sg.auto_command()[-2:] == [os.path.join(work, "grades.pyw"), "--auto"], sg.auto_command())

print("Notifications")
calls.clear()
with patch.object(sg, "_run", fake_run):
    with patch.object(sg.os, "name", "nt"):
        sg.notify("T", "B")
        check("Windows: PowerShell toast, text passed as variables", calls[-1][0] == "powershell" and "ToastNotification" in calls[-1][-1] and "-WindowStyle" in calls[-1], calls[-1][:3])
    with patch.object(sg.sys, "platform", "darwin"):
        sg.notify("T", "B")
        check("Mac: osascript, text passed as variables", calls[-1][0] == "osascript" and "SG_BODY" in calls[-1][-1], calls[-1])
captured = {}
def fake_run_env(args, timeout=30, env=None):
    captured.update(env or {}); return MagicMock(returncode=0)
with patch.object(sg, "_run", fake_run_env), patch.object(sg.sys, "platform", "darwin"):
    ok = sg.notify('It\'s "99%"', "Algebra: 99% \u2192 100%")
    check("odd characters travel safely and success is reported", ok and captured["SG_TITLE"] == 'It\'s "99%"' and captured["SG_BODY"].startswith("Algebra"))
with patch.object(sg, "_run", lambda *a, **k: None), patch.object(sg.sys, "platform", "darwin"):
    check("a failing system call is reported as False", sg.notify("T", "B") is False)

print("The quiet check (grades --auto)")
data = sg.DATA_DIR
config = {"email": "x", "class_urls": ["u/Algebra", "u/Band"], "headless": True}
sent = []
def run_with(results, error=None):
    def fake_scrape(cfg, status):
        if error:
            raise error
        return [dict(r) for r in results], False
    with patch.object(g, "scrape_all", fake_scrape), patch.object(g, "load_config", lambda: config), patch.object(g, "notify", lambda t, m: sent.append((t, m)) or True):
        return g.run_auto()
for f in ("last_report.json", "grade_report.html", sg.LOCK_FILE, sg.AUTO_LOG):
    if os.path.exists(f): os.remove(f)
sg.write_auto(dict(sg.AUTO_DEFAULTS, enabled=True))
check("first run: succeeds, saves the report, says nothing", run_with(base) == 0 and not sent and os.path.isfile("grade_report.html") and os.path.isfile("last_report.json"), sent)
check("second run with a change: one notification", run_with(now) == 0 and len(sent) == 1 and "Algebra" in sent[0][1], sent)
check("the lock is released afterwards", not os.path.exists(sg.LOCK_FILE))
check("the log says what happened", "told you about 1" in sg.last_auto_line(), sg.last_auto_line())
open(sg.LOCK_FILE, "w").write("1")
sent.clear()
check("another check running: skips quietly", run_with(now) == 0 and not sent and "Skipped" in sg.last_auto_line() and os.path.exists(sg.LOCK_FILE))
os.remove(sg.LOCK_FILE)
bad = g.FriendlyError("x", "y", ("try_again",), "", kind="sign_in") if hasattr(g, "FriendlyError") else None
err = RuntimeError("Login did not complete in time")
check("sign-in problem: tells you once", run_with(base, err) == 1 and [t for t, _ in sent] == ["Please sign in again"], sent)
check("...and not again the same day", run_with(base, err) == 1 and len(sent) == 1, sent)
check("a failed check doesn't leave the lock behind", not os.path.exists(sg.LOCK_FILE))
check("a failed check is in the log", "Couldn't check" in sg.last_auto_line())
check("an interactive check waits for a scheduled one, then goes", (lambda: (open(sg.LOCK_FILE, "w").write("1"), os.utime(sg.LOCK_FILE, (time.time() - 3600,) * 2), sg.lock_busy())[-1])() is False)

print()
print("ALL PASSED" if not failures else "FAILED: %s" % failures)
sys.exit(1 if failures else 0)
