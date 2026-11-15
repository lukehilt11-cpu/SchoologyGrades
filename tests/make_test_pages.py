"""Writes made-up Schoology class pages into tests/pages/ (no real names, classes or grades).

Each page copies the parts of a real class page that the grade reader looks at, in one of the
layouts we have seen. Run:  python tests/make_test_pages.py
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "pages")

NO_GRADE = '<span class="no-grade">\u2014</span>'


def badge(pct, letter=None):
    """The grade cell contents Schoology draws: just a percent, or a letter with the percent."""
    pct_text = ("%g" % round(pct, 2)) + "%"
    inner = '<span class="rounded-grade" title="%s%%">%s</span>' % (repr(pct), pct_text)
    if letter:
        return ('<span class="awarded-grade"><span class="alpha-grade primary-grade">%s</span> '
                '<span class="numeric-grade secondary-grade">(%s)</span></span>' % (letter, inner))
    return '<span class="awarded-grade"><span class="numeric-grade primary-grade">%s</span></span>' % inner


def row(kind, title, grade_html, weight=None, cid="9000001", rid="1"):
    contrib = ' <span class="percentage-contrib">(%s%%)</span>' % weight if weight else ""
    return (
        '<tr class="report-row %s" data-id="%s" data-parent-id="%s">'
        '<th class="title-column" scope="row"><div class="td-content-wrapper">'
        '<span class="title" role="button">%s</span>%s</div></th>'
        '<td class="grade-column"><div class="td-content-wrapper">%s</div></td>'
        '<td class="comment-column"><div class="td-content-wrapper"></div></td></tr>'
        % (kind, rid, "" if kind == "course-row" else cid, title, contrib, grade_html)
    )


def item_row(title, due, score, mx, parent, n, missing=False):
    """An assignment row. due: '9/02/26 11:19am'. score None = not graded."""
    grade = ('<span class="awarded-grade"><span class="rounded-grade" title="%g">%g</span></span>'
             '<span class="max-grade"> / %g</span>' % (score, score, mx)) if score is not None else NO_GRADE
    return (
        '<tr class="report-row item-row" data-id="I-%s" data-parent-id="%s"><th class="title-column" scope="row">'
        '<div class="reportSpacer-3"><div class="td-content-wrapper"><span class="title" role="button">'
        '<a href="/assignment/%s">%s<span class="visually-hidden">assignment</span></a></span> '
        '<span class="due-date"><span class="visually-hidden">Due </span>%s</span></div></div></th>'
        '<td class="grade-column"><div class="td-content-wrapper">%s%s</div></td>'
        '<td class="comment-column"><div class="td-content-wrapper"></div></td></tr>'
        % (n, parent, n, title, due, grade, '<span class="exception-icon missing" title="Missing"></span>' if missing else "")
    )


def category(name, weight, parent, n, pct=None):
    return (
        '<tr class="report-row category-row hidden" data-id="%s-%s" data-parent-id="%s"><th class="title-column" scope="row">'
        '<div class="reportSpacer-2"><div class="td-content-wrapper"><span class="title" role="button">%s'
        '<span class="visually-hidden">Category</span></span> <span class="percentage-contrib">(%s%%)</span></div></div></th>'
        '<td class="grade-column"><div class="td-content-wrapper">%s</div></td><td class="comment-column"></td></tr>'
        % (parent, n, parent, name, weight, badge(pct) if pct is not None else "")
    )


def page(title, periods, course_row=None, summary=None, hide_course_row=False, cid="9000001", items=None):
    """periods: [(name, weight, pct or None)]. course_row / summary: pct, or None to leave it out."""
    rows = []
    if course_row is not None:
        rows.append(row("course-row", "Course Grade", badge(course_row), cid=cid, rid=cid))
    elif course_row is None and summary is None and hide_course_row:
        rows.append(row("course-row", "Course Grade", "", cid=cid, rid=cid))  # present but empty
    for i, (name, weight, pct) in enumerate(periods, start=1):
        rows.append(row("period-row", name, badge(pct) if pct is not None else NO_GRADE, weight, cid, str(100 + i)))
        for cat_name, cat_weight, cat_pct, work in (items or {}).get(i, []):  # assignments under this period
            rows.append(category(cat_name, cat_weight, str(100 + i), len(rows), cat_pct))
            for k, (t, due, score, mx, *flag) in enumerate(work):
                rows.append(item_row(t, due, score, mx, "%d-%d" % (100 + i, len(rows) - 1), "%d%d%d" % (i, len(rows), k), bool(flag)))
    summary_html = ""
    if summary is not None:
        summary_html = (
            '<div class="summary-course"><div class="course-grade-wrapper">'
            '<span class="course-grade-text">Course Grade: </span>'
            '<span class="course-grade-value">%s</span></div></div>' % badge(summary)
        )
    style = "<style>tr.course-row{display:none}</style>" if hide_course_row else ""
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8"><title>Grades</title>%s</head><body>'
        '<h1 class="page-title">%s</h1>'
        '<div class="gradebook-course-grades"><table><tbody>%s</tbody></table></div>%s'
        "</body></html>" % (style, title, "".join(rows), summary_html)
    )


# name -> (html, what a correct reader should say: (layout, percent) or (None, None))
CASES = {
    # A "Course Grade" line under the grades, as most classes show it. The table's own Course Grade row is
    # hidden on screen, like on the real pages.
    "summary_two_quarters": (
        page("Demo Algebra: DEMO A - 1", [("26-27 Q1", 50, 77.0), ("26-27 Q2", 50, 92.0)],
             course_row=84.5, summary=84.5, hide_course_row=True, cid="9000001",
             items={1: [("Classwork", 40, 110.0, [("Warm-up", "9/02/26 11:19am", 10, 10), ("Extra credit quiz", "9/09/26 8:00am", 1, 0)]),
                        ("Tests", 60, 55.0, [("Unit 1 Test", "9/20/26 1:45pm", 55, 100)])],
                    2: [("Classwork", 40, None, [("Worksheet A", "11/09/26 12:08pm", None, 10), ("Worksheet B", "11/30/36 12:00pm", None, 10),
                                                 ("Lab write-up", "11/05/26 9:00am", None, 20, "missing")]),
                        ("Tests", 60, 92.0, [("Unit 2 Test", "11/04/26 1:45pm", 92, 100)])]}),
        ("summary", 84.5)),
    "summary_one_quarter": (
        page("Demo Health: DEMO H - 2", [("26-27 Q1", 50, 97.39), ("26-27 Q2", 50, None)],
             course_row=97.39, summary=97.39, cid="9000002"),
        ("summary", 97.39)),
    # The teacher hides the cumulative grade: an empty Course Grade row, no line underneath.
    "periods_only_two": (
        page("Demo Band: DEMO B - 3", [("26-27 Q1", 50, 99.0), ("26-27 Q2", 50, 100.0)],
             hide_course_row=False, cid="9000003").replace("</tbody>",
             row("course-row", "Course Grade", "", cid="9000003", rid="9000003") + "</tbody>"),
        ("periods", 99.5)),
    "periods_only_one": (
        page("Demo English: DEMO E - 4", [("26-27 Q1", 50, None), ("26-27 Q2", 50, 96.0)], cid="9000004"),
        ("periods", 96.0)),
    # Quarters that count for different amounts.
    "periods_uneven_weights": (
        page("Demo Science: DEMO S - 5", [("26-27 Q1", 40, 80.0), ("26-27 Q2", 60, 95.0)], cid="9000005"),
        ("periods", 89.0)),
    # A Course Grade row in the table and no line underneath.
    "course_row_only": (
        page("Demo Art: DEMO A - 6", [("26-27 Q1", 50, 88.0), ("26-27 Q2", 50, 92.0)],
             course_row=90.0, cid="9000006"),
        ("course_row", 90.0)),
    # Nothing graded yet anywhere.
    "nothing_graded": (
        page("Demo Shop: DEMO P - 7", [("26-27 Q1", 50, None), ("26-27 Q2", 50, None)], cid="9000007"),
        (None, None)),
    # A page that shows a letter as well as a percent.
    "summary_with_letter": (
        page("Demo History: DEMO H - 8", [("26-27 Q1", 50, 91.5), ("26-27 Q2", 50, 93.5)],
             summary=None, cid="9000008").replace("</body>",
             '<div class="summary-course"><div class="course-grade-wrapper"><span class="course-grade-text">Course Grade: </span>'
             '<span class="course-grade-value">%s</span></div></div></body>' % badge(92.5, "A")),
        ("summary", 92.5)),
}


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, (html, _) in CASES.items():
        with open(os.path.join(OUT, name + ".html"), "w", encoding="utf-8") as f:
            f.write(html)
    print("wrote %d pages to %s" % (len(CASES), OUT))


if __name__ == "__main__":
    main()
