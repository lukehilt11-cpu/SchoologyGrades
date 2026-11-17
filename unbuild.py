"""unbuild.py: put this computer back the way it was before the first build, then delete the whole project folder.

    python unbuild.py              show the plan, ask you to type "unbuild", then do it
    python unbuild.py --dry-run    show the plan only; nothing is touched
    python unbuild.py --yes        don't ask (for scripts)
    python unbuild.py --skip-pip   leave the Python packages alone
    python unbuild.py --keep-folder   do everything except deleting the project folder (only unbuild.py goes)
    python unbuild.py --force      delete the folder even if Git says some work isn't pushed to GitHub

Removes: Autopilot's scheduled job, the installed programs and their saved data (settings, the saved
sign-in, grades), the programs and installers this folder built, build leftovers, Inno Setup (Windows),
the Python packages the build installed (only ones nothing else on this computer needs), and finally
THIS WHOLE FOLDER, including source/, tests/ and unbuild.py itself. Clone the repository again to start fresh.
Never touches: Python and Chrome.

Before anything is deleted it checks with Git that everything is committed and pushed, and stops if not.
The folder is only deleted when every earlier step finished without a problem.
"""
import os
import re
import shutil
import stat
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
WIN = os.name == "nt"
MAC = sys.platform == "darwin"
HOME = os.path.expanduser("~")
LOCAL = os.environ.get("LOCALAPPDATA") or ""
TASK = "SchoologyGradeChecker-Autopilot"
AGENT = "local.schoologygrades.autopilot"
PIP_TARGETS = ["pyinstaller", "selenium", "pillow", "numpy", "dmgbuild"]
PIP_KEEP = {"pip", "setuptools", "wheel"}  # never removed, even if only the build needed them


# ---- safety ------------------------------------------------------------------------------------
def check_root():
    for needed in ("source/grades.pyw", "source/sgcommon.py"):
        if not os.path.isfile(os.path.join(ROOT, *needed.split("/"))):
            sys.exit(f"Stopping: this doesn't look like the project folder (missing {needed}).\n"
                     "Put unbuild.py in the same folder as build.py and run it from there.")


def allowed(path):
    """Only ever delete inside the project folder or the program's own install / data / cache places."""
    path = os.path.abspath(path)
    roots = [ROOT, os.path.join(LOCAL, "SchoologyGrades"), os.path.join(LOCAL, "SchoologyGradesBuild"),
             os.path.join(LOCAL, "Programs", "SchoologyGrades"), os.path.join(HOME, "Library", "Application Support", "SchoologyGrades"),
             os.path.join(HOME, "Library", "Caches", "SchoologyGradesBuild"), os.path.join(HOME, ".cache", "SchoologyGradesBuild"),
             "/Applications/SchoologyGrades.app", "/Applications/SchoologySetup.app", "/Applications/SchoologyAutopilot.app",
             os.path.join(HOME, "Applications", "SchoologyGrades.app"), os.path.join(HOME, "Applications", "SchoologySetup.app"),
             os.path.join(HOME, "Applications", "SchoologyAutopilot.app"),
             os.path.join(HOME, "Library", "LaunchAgents", AGENT + ".plist")]
    if not any(r and (path == r or path.startswith(r.rstrip(os.sep) + os.sep)) for r in roots) or path == ROOT:
        return False
    if path.startswith(ROOT + os.sep):  # inside the project, only build output may go: never the code, tests, site or workflow
        parts = os.path.relpath(path, ROOT).split(os.sep)
        return (parts[0] in {"SchoologyGrades.exe", "SchoologySetup.exe", "SchoologyAutopilot.exe", "SchoologyGrades.app",
                             "SchoologySetup.app", "SchoologyAutopilot.app", "releases", "data"}
                or parts[:2] in (["source", "icons"], ["tests", "pages"]) or parts[-1] == "__pycache__")
    return True


def delete(path):
    """Delete a file or folder. Returns None on success or a short reason."""
    if not allowed(path):
        return "refused: outside the places unbuild is allowed to touch"
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            def unlock(func, p, _):  # Windows keeps some files read-only
                os.chmod(p, stat.S_IWRITE)
                func(p)
            shutil.rmtree(path, onerror=unlock)
        else:
            os.remove(path)
        return None
    except OSError as e:
        return f"couldn't remove it ({e.strerror or e}); close the program if it's running, then run unbuild again"


def run(args, timeout=300):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


# ---- what is there to remove -------------------------------------------------------------------
def pip_plan():
    """(packages to remove, {package: other packages that still need it}). Shared packages stay."""
    from importlib import metadata

    canon = lambda n: re.sub(r"[-_.]+", "-", n).lower()
    dists = {}
    for d in metadata.distributions():
        name = d.metadata["Name"]
        if name:
            dists[canon(name)] = d
    needs = {}
    for name, d in dists.items():
        needs[name] = {canon(m.group(0)) for r in (d.requires or []) if "extra ==" not in r
                       for m in [re.match(r"[A-Za-z0-9][A-Za-z0-9_.-]*", r)] if m}
    remove = {t for t in PIP_TARGETS if t in dists}
    changed = True
    while changed:  # also remove what only the removed packages needed
        changed = False
        for name in dists:
            if name in remove or name in PIP_KEEP:
                continue
            if any(name in needs[r] for r in remove) and not [o for o in dists if o not in remove and name in needs[o]]:
                remove.add(name)
                changed = True
    shared = {t: sorted(o for o in dists if o not in remove and t in needs[o]) for t in remove if t in PIP_TARGETS}
    return sorted(remove), {t: o for t, o in shared.items() if o}


def find_iscc_dir():
    if not WIN:
        return None
    for p in (os.path.join(LOCAL, "Programs", "Inno Setup 6"), os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Inno Setup 6"),
              os.path.join(os.environ.get("ProgramFiles", ""), "Inno Setup 6")):
        if os.path.isfile(os.path.join(p, "ISCC.exe")):
            return p
    return None


def git_blockers():
    """Reasons deleting this folder would lose work. An empty list means everything is on GitHub."""
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        return ["there is no Git history here, so this folder is the only copy"]
    if not shutil.which("git"):
        return ["Git isn't installed, so I can't check that your work is pushed"]

    def git(*a):
        return run(["git", "-C", ROOT] + list(a), timeout=60)

    out = []
    status = git("status", "--porcelain")
    if status is None or status.returncode:
        return ["couldn't read the Git status"]
    changed = [l for l in status.stdout.splitlines() if l.strip() and l[3:].strip().strip('"') != "unbuild.py"]
    if changed:
        out.append(f"{len(changed)} file(s) have changes that aren't committed (for example {changed[0][3:]})")
    upstream = git("rev-parse", "--abbrev-ref", "@{u}")
    if upstream is None or upstream.returncode:
        out.append("this branch isn't linked to a GitHub branch")
    else:
        ahead = git("rev-list", "--count", "@{u}..HEAD")
        if ahead is not None and ahead.stdout.strip().isdigit() and int(ahead.stdout) > 0:
            out.append(f"{int(ahead.stdout)} commit(s) haven't been pushed")
    stash = git("stash", "list")
    if stash is not None and stash.stdout.strip():
        out.append("there are stashed changes that exist only here")
    return out


def delete_folder():
    """Delete the whole project folder (this script included). Returns None on success or a reason."""
    parts = [p for p in os.path.normpath(ROOT).split(os.sep) if p]
    if os.path.normpath(ROOT) in (os.path.normpath(HOME), os.path.dirname(HOME)) or len(parts) < 3 or not os.path.isfile(os.path.join(ROOT, "source", "grades.pyw")):
        return "refused: that doesn't look like the project folder"
    os.chdir(os.path.dirname(ROOT))  # a folder can't be deleted while we're standing in it

    def unlock(func, p, _):  # Windows keeps git's files read-only
        os.chmod(p, stat.S_IWRITE)
        func(p)
    try:
        shutil.rmtree(ROOT, onerror=unlock)
    except OSError as e:
        return f"couldn't remove {ROOT} ({e.strerror or e}); close VS Code and any program using it, then delete the folder yourself"
    return None if not os.path.exists(ROOT) else f"some files in {ROOT} are still there; close VS Code and delete the folder yourself"


def build_plan(skip_pip, keep_folder=False):
    steps = []  # (heading, [lines], function)

    def step(title, lines, fn):
        if lines:
            steps.append((title, lines, fn))

    # 1. the schedule
    if WIN:
        found = run(["schtasks", "/Query", "/TN", TASK])
        step("Autopilot's scheduled job", ["Windows Task Scheduler: " + TASK] if found and found.returncode == 0 else [],
             lambda: run(["schtasks", "/Delete", "/TN", TASK, "/F"]) and None)
    elif MAC:
        plist = os.path.join(HOME, "Library", "LaunchAgents", AGENT + ".plist")

        def drop_agent():
            run(["launchctl", "bootout", f"gui/{os.getuid()}/{AGENT}"])
            return delete(plist) if os.path.exists(plist) else None
        step("Autopilot's scheduled job", [plist] if os.path.exists(plist) else [], drop_agent)

    # 2. installed copies and their data
    installed = []
    if WIN:
        unins = os.path.join(LOCAL, "Programs", "SchoologyGrades", "unins000.exe")
        if os.path.isfile(unins):
            installed.append(("uninstall", unins))
        installed += [("delete", p) for p in (os.path.join(LOCAL, "Programs", "SchoologyGrades"), os.path.join(LOCAL, "SchoologyGrades")) if os.path.exists(p)]
    if MAC:
        installed += [("delete", p) for p in [f"/Applications/Schoology{n}.app" for n in ("Grades", "Setup", "Autopilot")]
                      + [os.path.join(HOME, "Applications", f"Schoology{n}.app") for n in ("Grades", "Setup", "Autopilot")]
                      + [os.path.join(HOME, "Library", "Application Support", "SchoologyGrades")] if os.path.exists(p)]

    def remove_installed():
        problems = []
        for kind, p in installed:
            if kind == "uninstall":
                run([p, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
            elif os.path.exists(p):
                bad = delete(p)
                if bad:
                    problems.append(f"{p}: {bad}")
        return "; ".join(problems) or None
    step("Installed copies and their saved data", [("run the uninstaller: " if k == "uninstall" else "") + p for k, p in installed], remove_installed)

    # 3. what this folder built
    built = [os.path.join(ROOT, n) for n in ("SchoologyGrades.exe", "SchoologySetup.exe", "SchoologyAutopilot.exe", "SchoologyGrades.app",
                                             "SchoologySetup.app", "SchoologyAutopilot.app", "releases", "data")]
    built += [os.path.join(ROOT, "source", "icons"), os.path.join(ROOT, "tests", "pages")]
    for here, dirs, _ in os.walk(ROOT):
        if ".git" in dirs:
            dirs.remove(".git")
        if "__pycache__" in dirs:
            built.append(os.path.join(here, "__pycache__"))
            dirs.remove("__pycache__")
    for cache in (os.path.join(LOCAL, "SchoologyGradesBuild"), os.path.join(HOME, "Library", "Caches", "SchoologyGradesBuild"),
                  os.path.join(HOME, ".cache", "SchoologyGradesBuild")):
        built.append(cache)
    built = [p for p in built if os.path.exists(p)]

    def remove_built():
        return "; ".join(f"{p}: {bad}" for p in built for bad in [delete(p)] if bad) or None
    step("Built programs, installers, saved data (settings, saved sign-in, grades) and build leftovers",
         [os.path.relpath(p, ROOT) if p.startswith(ROOT) else p for p in built], remove_built)

    # 4. Inno Setup
    iscc = find_iscc_dir()

    def drop_inno():
        if shutil.which("winget"):
            r = run(["winget", "uninstall", "--id", "JRSoftware.InnoSetup", "-e", "--silent", "--accept-source-agreements"])
            if r is not None and r.returncode == 0:
                return None
        unins = os.path.join(iscc, "unins000.exe")
        if os.path.isfile(unins):
            run([unins, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
            return None if not os.path.isfile(os.path.join(iscc, "ISCC.exe")) else "Inno Setup is still installed; remove it in Settings > Apps"
        return "couldn't remove Inno Setup; remove it in Settings > Apps"
    step("Inno Setup (the Windows installer maker)", [iscc] if iscc else [], drop_inno)

    # 5. Python packages
    if not skip_pip:
        packages, shared = pip_plan()
        lines = list(packages)
        for target, users in shared.items():
            lines.append(f"  note: {target} is also used by {', '.join(users)}; those may stop working")
        step("Python packages the build installed", lines,
             lambda: None if (run([sys.executable, "-m", "pip", "uninstall", "-y"] + packages) or subprocess.CompletedProcess([], 1)).returncode == 0
             else "pip couldn't remove everything; run it by hand: python -m pip uninstall " + " ".join(packages))
    if not keep_folder:
        steps.append(("The project folder (this is the last step)", [ROOT + "   <- everything in it, source and tests included"], delete_folder))
    return steps


# ---- main --------------------------------------------------------------------------------------
def main():
    check_root()
    args = set(sys.argv[1:])
    keep_folder = "--keep-folder" in args
    steps = build_plan("--skip-pip" in args, keep_folder)
    blockers = [] if keep_folder or "--force" in args else git_blockers()
    for title, lines, _ in steps:
        print(f"\n{title}:")
        for line in lines:
            print("   ", line)
    print("\nKept: Python and Chrome." + (" (The project folder stays too, apart from unbuild.py.)" if keep_folder else ""))
    if blockers:
        print("\nSTOPPED: deleting the project folder would lose work:")
        for b in blockers:
            print("    -", b)
        print("Commit and push it first (then run unbuild again), or use --keep-folder to leave the folder alone,")
        print("or --force if you really don't need that work.")
        return 1
    if not keep_folder:
        print("\nGit says everything is pushed to GitHub, so you can clone it again afterwards." if "--force" not in args else "\n--force: the Git check was skipped.")
    if "--dry-run" in args:
        print("\nDry run: nothing was changed.")
        return 0
    if steps and "--yes" not in args:
        print("\nThis also deletes your saved settings, saved sign-in and grades (the data folders).")
        if not keep_folder:
            print(f"It then deletes this whole folder: {ROOT}")
        if input('Type "unbuild" to continue: ').strip().lower() != "unbuild":
            print("Cancelled. Nothing was changed.")
            return 1
    problems = []
    for title, _, fn in steps:
        if problems and fn is delete_folder:
            print("\nNot deleting the project folder because an earlier step had a problem.")
            break
        print(f"\n{title}...", end=" ", flush=True)
        problem = fn()
        print("done" if not problem else "PROBLEM")
        if problem:
            problems.append(f"{title}: {problem}")
            print("   ", problem)
    if problems:
        print("\nSome things couldn't be removed (see above). unbuild.py is still here, so you can run it again.")
        return 1
    if keep_folder:
        try:
            os.remove(os.path.abspath(__file__))
            print("\nAll done. unbuild.py has removed itself.")
        except OSError:
            print("\nAll done. You can delete unbuild.py yourself.")
    else:
        print("\nAll done. The project folder is gone. Clone the repository again to start fresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
