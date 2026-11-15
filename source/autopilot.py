"""Schoology Autopilot

Checks your grades by itself at the times you choose and sends a notification only when
something changed. It sets up the computer's own scheduler (Task Scheduler on Windows, a
launch agent on macOS) to run "Schoology Grades --auto", so nothing has to stay open.
Settings are saved in data/auto.json.
"""

import os
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

# Shared code. Importing it also checks that Selenium is installed and moves the
# program into its "data" folder, so keep it above anything that reads files.
import sgcommon as sg

BG, CARD, LINE = "#eef7f1", "#ffffff", "#cfe5d6"
GREEN, GREEN_DARK, GREEN_OFF = "#15803d", "#166534", "#9ac7a8"
INK, MUTED = "#14321f", "#4b6b57"
FONT = "Segoe UI"

TIMES = [f"{h:02d}:{m:02d}" for h in range(5, 23) for m in (0, 30)]
NOT_USED = "Not used"
LABEL_TO_TIME = {sg.format_time12(t): t for t in TIMES}


def describe_next(settings):
    """'Mon 3:30 PM' for the next scheduled check, or ''."""
    moment = sg.next_run(settings)
    if moment is None:
        return ""
    day = "Today" if moment.date() == datetime.now().date() else sg.DAY_NAMES[moment.weekday()]
    return f"{day} {sg.format_time12(moment.strftime('%H:%M'))}"


class AutopilotApp:
    def __init__(self, root):
        self.root = root
        self.settings = sg.read_auto()

        root.title(f"Schoology Autopilot {sg.app_version()}")
        sg.set_window_icon(root, "auto")
        root.resizable(False, False)
        root.configure(bg=BG)
        self.make_styles()
        sg.add_help_menu(root)
        try:
            root.createcommand("tk::mac::Quit", root.destroy)  # macOS: Cmd-Q and Dock > Quit
        except Exception:
            pass

        self.enabled = tk.BooleanVar(value=self.settings["enabled"])
        self.days = [tk.BooleanVar(value=d in self.settings["days"]) for d in range(7)]
        times = [sg.format_time12(t) for t in self.settings["times"]] + [NOT_USED] * 3
        self.times = [tk.StringVar(value=times[i]) for i in range(3)]
        notify = self.settings["notify"]
        self.on_change = tk.BooleanVar(value=notify["changes"])
        self.on_new = tk.BooleanVar(value=notify["new_grades"])
        self.on_missing = tk.BooleanVar(value=notify["missing"])
        self.on_low = tk.BooleanVar(value=notify["low"])
        self.low_below = tk.StringVar(value=str(int(notify["low_below"])))
        self.status = tk.StringVar()
        self.last = tk.StringVar()

        self.build()
        self.refresh()

        root.lift()
        root.attributes("-topmost", True)
        root.after(500, lambda: root.attributes("-topmost", False))
        root.focus_force()

    # ---- look -----------------------------------------------------------
    def make_styles(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=INK, font=(FONT, 10))
        style.configure("TFrame", background=BG)
        style.configure("Inner.TFrame", background=CARD, borderwidth=0, relief="flat")
        style.configure("Card.TFrame", background=CARD, relief="solid", borderwidth=1, bordercolor=LINE)
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Card.TLabel", background=CARD)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
        style.configure("Title.TLabel", font=(FONT, 18, "bold"), foreground=GREEN_DARK)
        style.configure("Head.TLabel", background=CARD, font=(FONT, 10, "bold"))
        style.configure("Status.TLabel", font=(FONT, 10, "bold"), foreground=GREEN_DARK)
        style.configure("TCheckbutton", background=CARD, foreground=INK, focuscolor=CARD)
        style.map("TCheckbutton", background=[("active", CARD)], indicatorcolor=[("selected", GREEN), ("!selected", CARD)])
        style.configure("Switch.TCheckbutton", background=BG, font=(FONT, 11, "bold"))
        style.map("Switch.TCheckbutton", background=[("active", BG)], indicatorcolor=[("selected", GREEN), ("!selected", CARD)])
        style.configure("Day.Toolbutton", background=CARD, foreground=INK, padding=(7, 5), borderwidth=1, bordercolor=LINE, focuscolor=CARD)
        style.map("Day.Toolbutton", background=[("selected", GREEN), ("active", "#e7f5ec")], foreground=[("selected", "white")])
        style.configure("TButton", background=CARD, foreground=INK, padding=(12, 6), bordercolor=LINE, focuscolor=CARD)
        style.map("TButton", background=[("active", "#e7f5ec")])
        style.configure("Accent.TButton", background=GREEN, foreground="white", padding=(16, 7), borderwidth=0, font=(FONT, 10, "bold"))
        style.map("Accent.TButton", background=[("active", GREEN_DARK), ("disabled", GREEN_OFF)])
        style.configure("Link.TButton", background=BG, foreground=GREEN_DARK, borderwidth=0, padding=(0, 2))
        style.map("Link.TButton", foreground=[("active", INK)], background=[("active", BG)])
        style.configure("TCombobox", fieldbackground=CARD, background=CARD, arrowcolor=GREEN_DARK, bordercolor=LINE)
        style.map("TCombobox", fieldbackground=[("readonly", CARD)], foreground=[("readonly", INK)])
        style.configure("TSpinbox", fieldbackground=CARD, background=CARD, arrowcolor=GREEN_DARK, bordercolor=LINE)

    # ---- layout ---------------------------------------------------------
    def build(self):
        outer = ttk.Frame(self.root, padding=(20, 18, 20, 16))
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Autopilot", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer, style="Muted.TLabel", wraplength=420, justify="left",
            text="Checks your grades by itself and tells you when something changes. Nothing has to stay open.",
        ).pack(anchor="w", pady=(2, 12))

        ttk.Checkbutton(outer, text="Check my grades automatically", variable=self.enabled, style="Switch.TCheckbutton").pack(anchor="w")
        ttk.Label(outer, textvariable=self.status, style="Status.TLabel", wraplength=420, justify="left").pack(anchor="w", pady=(4, 12))

        when = ttk.Frame(outer, style="Card.TFrame", padding=14)
        when.pack(fill="x")
        ttk.Label(when, text="When", style="Head.TLabel").grid(row=0, column=0, columnspan=7, sticky="w")
        for i, name in enumerate(sg.DAY_NAMES):
            ttk.Checkbutton(when, text=name, variable=self.days[i], style="Day.Toolbutton", width=4).grid(
                row=1, column=i, padx=(0 if i == 0 else 4, 0), pady=(8, 10))
        row = ttk.Frame(when, style="Inner.TFrame")
        row.grid(row=2, column=0, columnspan=7, sticky="w")
        ttk.Label(row, text="Check at", style="Card.TLabel").pack(side="left", padx=(0, 8))
        choices = [sg.format_time12(t) for t in TIMES]
        for i, var in enumerate(self.times):
            box = ttk.Combobox(row, textvariable=var, values=([NOT_USED] if i else []) + choices, state="readonly", width=9)
            box.pack(side="left", padx=(0, 6))

        tell = ttk.Frame(outer, style="Card.TFrame", padding=14)
        tell.pack(fill="x", pady=(10, 0))
        ttk.Label(tell, text="Tell me when", style="Head.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Checkbutton(tell, text="A grade goes up or down", variable=self.on_change).pack(anchor="w", pady=1)
        ttk.Checkbutton(tell, text="A new grade is posted", variable=self.on_new).pack(anchor="w", pady=1)
        ttk.Checkbutton(tell, text="An assignment is marked missing", variable=self.on_missing).pack(anchor="w", pady=1)
        low = ttk.Frame(tell, style="Inner.TFrame")
        low.pack(anchor="w", pady=1)
        ttk.Checkbutton(low, text="A class drops below", variable=self.on_low).pack(side="left")
        ttk.Spinbox(low, from_=1, to=100, width=4, textvariable=self.low_below, justify="center").pack(side="left", padx=(4, 3))
        ttk.Label(low, text="%", style="Card.TLabel").pack(side="left")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self.save).pack(side="left")
        ttk.Button(buttons, text="Send a test notification", command=self.test_notification).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Check now", command=self.check_now).pack(side="left", padx=(8, 0))

        foot = ttk.Frame(outer)
        foot.pack(fill="x", pady=(12, 0))
        ttk.Label(foot, textvariable=self.last, style="Muted.TLabel", wraplength=300, justify="left").pack(side="left")
        ttk.Button(foot, text="Open latest report", style="Link.TButton", command=self.open_report).pack(side="right")

        self.center()

    def center(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # ---- what the window holds -------------------------------------------
    def gather(self):
        """The settings as the window shows them now."""
        times = sorted({LABEL_TO_TIME[v.get()] for v in self.times if v.get() in LABEL_TO_TIME})
        try:
            low = max(1, min(100, int(float(self.low_below.get()))))
        except ValueError:
            low = 80
        settings = {
            "enabled": self.enabled.get(),
            "days": [i for i, v in enumerate(self.days) if v.get()],
            "times": times,
            "notify": {"changes": self.on_change.get(), "new_grades": self.on_new.get(),
                       "missing": self.on_missing.get(), "low": self.on_low.get(), "low_below": low},
        }
        if "last_signin_notice" in self.settings:
            settings["last_signin_notice"] = self.settings["last_signin_notice"]
        return settings

    def refresh(self):
        s = self.settings
        if not s["enabled"]:
            self.status.set("Autopilot is off.")
        elif not sg.schedule_installed():
            self.status.set("Turned on, but not scheduled yet. Press Save.")
        else:
            nxt = describe_next(s)
            self.status.set("Autopilot is on." + (f" Next check: {nxt}." if nxt else ""))
        line = sg.last_auto_line()
        self.last.set("Last check: " + line if line else "No automatic check has run yet.")

    # ---- buttons ---------------------------------------------------------
    def save(self):
        settings = self.gather()
        if settings["enabled"] and not sg.read_config():
            messagebox.showinfo("Autopilot", "Open Schoology Setup first and choose your classes, then come back here.", parent=self.root)
            return
        if settings["enabled"] and not (settings["days"] and settings["times"]):
            messagebox.showinfo("Autopilot", "Pick at least one day and one time.", parent=self.root)
            return
        self.settings = settings
        if not sg.write_auto(settings):
            messagebox.showerror("Autopilot", "Couldn't save your settings.", parent=self.root)
            return
        ok, message = sg.install_schedule(settings) if settings["enabled"] else sg.remove_schedule()
        if not ok:
            messagebox.showerror("Autopilot", message, parent=self.root)
        self.refresh()

    def test_notification(self):
        if not sg.notify("Autopilot is working", "A grade update will look like this."):
            messagebox.showinfo(
                "Autopilot",
                "The notification didn't show. Check that notifications are allowed for this computer (on Windows, "
                "Focus assist or Do not disturb can hide them; on a Mac, see System Settings > Notifications).",
                parent=self.root,
            )

    def check_now(self):
        command = sg.auto_command()
        if not command:
            messagebox.showinfo("Autopilot", "Couldn't find Schoology Grades next to this program.", parent=self.root)
            return
        if sg.lock_busy():
            messagebox.showinfo("Autopilot", "A check is already running. Give it a minute.", parent=self.root)
            return
        sg._spawn(command)
        self.status.set("Checking in the background...")
        self.poll(0)

    def poll(self, tries):
        """Watch for the background check to finish, then show what it did."""
        if tries < 40 and (tries < 2 or sg.lock_busy()):
            self.root.after(3000, lambda: self.poll(tries + 1))
        else:
            self.refresh()

    def open_report(self):
        path = os.path.join(sg.DATA_DIR, "grade_report.html")
        if os.path.isfile(path):
            webbrowser.open(Path(path).as_uri())
        else:
            messagebox.showinfo("Autopilot", "There's no report yet. Open Schoology Grades once, or use Check now.", parent=self.root)


def main():
    root = tk.Tk()
    AutopilotApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
