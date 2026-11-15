# Tests

Everything here uses **made-up** class pages (no real students, classes or grades), so it is safe in the public repository.

* `make_test_pages.py` writes the fake pages into `pages/` (one per layout Schoology uses: cumulative line, course row, quarters only, ...). Add a case there when a new layout turns up.
* `test_layouts.py` reads every page the way the program does (live page and direct-HTML shortcut), and checks the remembered-layout and sign-in behaviour.

Run (needs `pip install playwright` then `playwright install chromium`):

    python tests/test_layouts.py

Real saved class pages (Setup -> Help -> Save class pages) land in `data/pages/`, which Git ignores. Don't copy them into this folder.
