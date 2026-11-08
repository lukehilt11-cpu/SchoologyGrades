"""Schoology Setup

Signs in to Schoology, finds your classes on the Course Dashboard, lets you pick
the ones you want grades for, and saves everything to data/config.json.
"""

import json
import os
import threading
import tkinter as tk
import traceback
from tkinter import messagebox, ttk
from urllib.parse import urlparse

# Shared code. Importing it also checks that Selenium is installed and moves the
# program into its "data" folder, so keep it above anything that reads files.
import sgcommon as sg

ERROR_COLOR = "#b91c1c"
INFO_COLOR = "#475569"


class SetupApp:
    def __init__(self, root):
        self.root = root
        self.busy = False
        self.courses = []
        self.pending = {}
        self.existing = sg.read_config()

        old_urls = self.existing.get("class_urls") or []
        host = (
            self.existing.get("school")
            or (urlparse(old_urls[0]).netloc if old_urls else "")
            or sg.DEFAULT_HOST
        )
        self.host_var = tk.StringVar(value=host)
        self.email_var = tk.StringVar(value=self.existing.get("email", ""))
        self.pass_var = tk.StringVar()
        self.show_browser_var = tk.BooleanVar(value=not self.existing.get("headless", True))
        self.status_var = tk.StringVar()

        root.title(f"Schoology Grade Checker Setup {sg.app_version()}")
        sg.set_window_icon(root, "setup")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.report_callback_exception = self.on_ui_error

        sg.add_help_menu(root, extras=[("Save class pages for troubleshooting...", self.save_pages)])
        try:
            root.createcommand("tk::mac::Quit", self.on_close)  # macOS: Cmd-Q and Dock > Quit
        except Exception:
            pass

        self.frame = ttk.Frame(root, padding=18)
        self.frame.pack(fill="both", expand=True)
        self.show_form()

        # Make sure the window is in front (on macOS it can open behind other windows).
        root.lift()
        root.attributes("-topmost", True)
        root.after(500, lambda: root.attributes("-topmost", False))
        root.focus_force()

    # ---- small helpers --------------------------------------------------
    def later(self, fn):
        """Run fn on the main thread (Tk is not thread-safe)."""
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    def clear(self):
        for widget in self.frame.winfo_children():
            widget.destroy()

    def center(self):
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 3
        self.root.geometry(f"+{x}+{y}")

    def say(self, text, error=False):
        self.status_var.set(text)
        try:
            self.status_label.config(fg=ERROR_COLOR if error else INFO_COLOR)
        except Exception:
            pass

    def on_close(self):
        if not self.busy:
            self.root.destroy()
            return
        if not messagebox.askyesno("Setup", "Stop looking up your classes and close?", parent=self.root):
            return
        self.say("Closing...")
        self.root.update_idletasks()
        sg.close_browser()  # don't leave Chrome running
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)  # also stops the background lookup

    def on_ui_error(self, exc, val, tb):
        sg.log_error("".join(traceback.format_exception(exc, val, tb)))
        messagebox.showerror("Setup", f"Something went wrong:\n\n{val}\n\n(Details saved in error_log.txt)")

    # ---- Help > Save class pages for troubleshooting ----------------------
    def save_pages(self):
        config = sg.read_config()
        if not (config.get("class_urls") and config.get("email") and config.get("password")):
            messagebox.showinfo("Setup", "Choose and save your classes first, then try this again.", parent=self.root)
            return
        if self.busy:
            return
        if not messagebox.askokcancel(
            "Save class pages",
            "This signs in and saves a copy of each of your class pages in the data folder "
            "(inside a folder called pages), so that a problem can be diagnosed.\n\n"
            "The copies include your name and your grades, so only share them on purpose.\n\nContinue?",
            parent=self.root,
        ):
            return
        self.busy = True
        dialog = sg.ProgressDialog(self.root, "Saving class pages", "Starting...")
        state = {"text": "Starting...", "done": False, "folder": None, "error": None}

        def work():
            try:
                state["folder"] = sg.save_course_pages(config, lambda t: state.__setitem__("text", t))
            except Exception as e:
                sg.log_error(traceback.format_exc())
                state["error"] = sg.explain(e)
            state["done"] = True

        threading.Thread(target=work, daemon=True).start()

        def poll():
            if not state["done"]:
                dialog.set(text=state["text"])
                self.root.after(150, poll)
                return
            dialog.close()
            self.busy = False
            if state["error"] is not None:
                messagebox.showerror("Save class pages", f"{state['error'].title}\n\n{state['error'].message}", parent=self.root)
                return
            messagebox.showinfo("Save class pages", "Done. The folder with the saved pages is opening now.", parent=self.root)
            sg.open_folder(state["folder"])

        self.root.after(150, poll)

    # ---- screen 1: sign in ----------------------------------------------
    def show_form(self, error=""):
        self.clear()
        f = self.frame
        ttk.Label(f, text="Set up Schoology Grade Checker", font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Sign in once and your classes are found for you.",
                  foreground=INFO_COLOR).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))

        self.inputs = []
        fields = (
            ("Schoology address", self.host_var, ""),
            ("School email", self.email_var, ""),
            ("Password", self.pass_var, "\u2022"),
        )
        for i, (label, var, mask) in enumerate(fields):
            ttk.Label(f, text=label).grid(row=2 + i, column=0, sticky="w", pady=4, padx=(0, 12))
            entry = ttk.Entry(f, textvariable=var, width=36, show=mask)
            entry.grid(row=2 + i, column=1, sticky="we", pady=4)
            self.inputs.append(entry)

        if self.existing.get("password"):
            ttk.Label(f, text="Leave the password blank to keep the saved one.",
                      foreground=INFO_COLOR).grid(row=5, column=1, sticky="w")

        box = ttk.Checkbutton(
            f,
            text="Show the browser while signing in\n(turn on if you have to approve the sign-in)",
            variable=self.show_browser_var,
        )
        box.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.inputs.append(box)

        self.status_label = tk.Label(f, textvariable=self.status_var, wraplength=440,
                                     justify="left", anchor="w")
        self.status_label.grid(row=7, column=0, columnspan=2, sticky="we", pady=(10, 0))
        self.progress = ttk.Progressbar(f, mode="indeterminate", length=440)
        self.progress.grid(row=8, column=0, columnspan=2, pady=(6, 0))
        self.progress.grid_remove()

        self.go_button = ttk.Button(f, text="Find my classes", command=self.start_lookup)
        self.go_button.grid(row=9, column=1, sticky="e", pady=(12, 0))

        self.say(error, error=True)
        self.center()

    def set_busy(self, busy):
        self.busy = busy
        for widget in self.inputs + [self.go_button]:
            widget.state(["disabled"] if busy else ["!disabled"])
        if busy:
            self.progress.grid()
            self.progress.start(10)
        else:
            self.progress.stop()
            self.progress.grid_remove()

    def start_lookup(self):
        host = sg.normalize_host(self.host_var.get())
        email = self.email_var.get().strip()
        password = self.pass_var.get()
        saved = self.existing
        if (not password and saved.get("password")
                and email.lower() == str(saved.get("email", "")).strip().lower()):
            password = saved["password"]
        if not host or not email or not password:
            self.say("Please fill in the address, email and password.", error=True)
            return

        self.host_var.set(host)
        self.set_busy(True)
        self.say("Starting Chrome...")
        headless = not self.show_browser_var.get()
        threading.Thread(target=self.lookup, args=(host, email, password, headless), daemon=True).start()

    def lookup(self, host, email, password, headless):
        """Background thread: sign in and read the course list."""
        def status(text):
            self.later(lambda: self.say(text))

        try:
            driver = sg.make_driver(headless, self.existing.get("profile_dir"))
            try:
                courses = sg.fetch_courses(driver, host, email, password, headless, status)
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass
            info = dict(host=host, email=email, password=password, headless=headless)
            self.later(lambda: self.lookup_done(courses, info))
        except Exception as e:
            sg.log_error(traceback.format_exc())
            friendly = sg.explain(e)
            self.later(lambda f=friendly: self.lookup_failed(f))

    def lookup_failed(self, friendly):
        self.set_busy(False)
        text = f"{friendly.title}\n{friendly.message}"
        if "download_chrome" in friendly.actions:
            text += f"\nGet it at {sg.CHROME_URL}"
        self.show_form(error=text)

    def lookup_done(self, courses, info):
        self.set_busy(False)
        if not courses:
            self.show_form(error="Signed in, but no classes were found on your Course Dashboard.")
            return
        self.pending = info
        self.courses = courses
        self.show_picker()

    # ---- screen 2: pick classes -----------------------------------------
    def show_picker(self):
        self.clear()
        f = self.frame
        ttk.Label(f, text="Pick your classes", font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(
            f,
            text="Click each class you want grades for (click again to unpick).\n"
                 "Things like Counselors or Chapel usually don't have grades.",
            foreground=INFO_COLOR, justify="left",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 10))

        previous = {str(c.get("id")) for c in self.existing.get("courses", []) if isinstance(c, dict)}
        previous |= {i for i in (sg.course_id_from_url(u) for u in self.existing.get("class_urls", [])) if i}

        list_frame = ttk.Frame(f)
        list_frame.grid(row=2, column=0, columnspan=4, sticky="we")
        scroll = ttk.Scrollbar(list_frame, orient="vertical")
        self.listbox = tk.Listbox(
            list_frame, selectmode="multiple", exportselection=False, activestyle="none",
            height=min(max(len(self.courses), 4), 14), width=58, yscrollcommand=scroll.set,
        )
        scroll.config(command=self.listbox.yview)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        for i, course in enumerate(self.courses):
            text = course["name"] + (f"   \u2014   {course['section']}" if course["section"] else "")
            self.listbox.insert("end", text)
            if course["id"] in previous:
                self.listbox.selection_set(i)

        self.status_label = tk.Label(f, textvariable=self.status_var, wraplength=440,
                                     justify="left", anchor="w")
        self.status_label.grid(row=3, column=0, columnspan=4, sticky="we", pady=(8, 0))
        self.say("")

        ttk.Button(f, text="Select all", command=lambda: self.listbox.selection_set(0, "end")).grid(
            row=4, column=0, sticky="w", pady=(10, 0))
        ttk.Button(f, text="Clear", command=lambda: self.listbox.selection_clear(0, "end")).grid(
            row=4, column=1, sticky="w", pady=(10, 0), padx=(6, 0))
        f.columnconfigure(2, weight=1)
        ttk.Button(f, text="Back", command=self.show_form).grid(row=4, column=2, sticky="e", pady=(10, 0))
        ttk.Button(f, text="Save", command=self.save).grid(row=4, column=3, sticky="e", pady=(10, 0), padx=(6, 0))
        self.center()

    def save(self):
        picked = [self.courses[i] for i in self.listbox.curselection()]
        if not picked:
            self.say("Pick at least one class.", error=True)
            return

        info = self.pending
        config = dict(self.existing)  # keep any extra settings, such as profile_dir
        config.update(
            email=info["email"],
            password=info["password"],
            school=info["host"],
            headless=info["headless"],
            courses=[{"id": c["id"], "name": c["name"]} for c in picked],
            class_urls=[sg.grade_url(info["host"], c["id"]) for c in picked],
        )
        try:
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4)
        except OSError as e:
            messagebox.showerror("Setup", f"Couldn't save config.json:\n\n{e}", parent=self.root)
            return

        self.show_done(len(picked))

    # ---- screen 3: all set -----------------------------------------------
    def show_done(self, count):
        self.clear()
        f = self.frame
        ttk.Label(f, text="\u2713  You're all set!", font=("Segoe UI", 15, "bold")).grid(
            row=0, column=0, sticky="w")
        ttk.Label(
            f,
            text=f"Saved {count} class{'es' if count != 1 else ''}. From now on, just open "
                 "Schoology Grades whenever you want to see your grades.",
            wraplength=400, justify="left", foreground=INFO_COLOR,
        ).grid(row=1, column=0, sticky="w", pady=(8, 14))
        self.check_now = tk.BooleanVar(value=True)
        ttk.Checkbutton(f, text="Check my grades now", variable=self.check_now).grid(
            row=2, column=0, sticky="w")
        ttk.Button(f, text="Done", command=self.finish).grid(row=3, column=0, sticky="e", pady=(16, 0))
        self.center()

    def finish(self):
        if self.check_now.get() and not sg.launch_sibling("grades"):
            messagebox.showinfo(
                "Setup",
                "Your classes are saved. Open Schoology Grades from your Start menu (or the "
                "program folder) to check your grades.",
                parent=self.root,
            )
        self.root.destroy()


def main():
    root = tk.Tk()
    SetupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
