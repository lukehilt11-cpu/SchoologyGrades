"""Build everything: runs source/build.bat on Windows or source/build.sh on a Mac.

    python build.py            normal build (opens the installer when done)
    python build.py norun      don't open it
    python build.py dev        quick build, programs only

Anything you type after build.py is passed straight to the build script.
"""
import os
import subprocess
import sys

source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "source")
args = sys.argv[1:]

if os.name == "nt":
    # build.bat waits for a key press when started like this; with no keyboard attached it just carries on
    result = subprocess.run([os.path.join(source, "build.bat")] + args, cwd=source, stdin=subprocess.DEVNULL)
else:
    result = subprocess.run(["bash", os.path.join(source, "build.sh")] + args, cwd=source)
sys.exit(result.returncode)
