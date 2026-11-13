"""Stash for Windows: paste Pinterest, Instagram or X links, click Download. Styled after Cairn."""
import ctypes
import glob
import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont, ttk

from stash_core import PinError, Stopped, download_post, find_links

APP_DATA = os.environ.get("APPDATA", os.path.expanduser("~"))
SETTINGS_FILE = os.path.join(APP_DATA, "Stash", "settings.json")
OLD_SETTINGS_FILE = os.path.join(APP_DATA, "PinterestDownloader", "settings.json")  # Before the rename.
DEFAULT_FOLDER = os.path.join(os.path.expanduser("~"), "Downloads", "Stash")
RESOURCES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

INK = "#070a08"
BASE = "#0a0e0b"
PANEL = "#0e1310"
PANEL_2 = "#121814"
LINE = "#1d2620"
LINE_STRONG = "#2a352d"
FG = "#e9eee5"
SOFT = "#b5bdb2"
MUTED = "#858e84"
LIME = "#d7f25c"
LIME_HOVER = "#e2fa70"
LIME_INK = "#11160a"
ROSE = "#f07a6a"


def load_folder():
    for path in (SETTINGS_FILE, OLD_SETTINGS_FILE):
        try:
            with open(path, encoding="utf-8") as f:
                folder = json.load(f).get("folder")
            if folder:
                return folder
        except (OSError, ValueError):
            continue
    return DEFAULT_FOLDER


def save_folder(folder):
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"folder": folder}, f)
    except OSError:
        pass  # Remembering the folder is a convenience; downloads still work.


def load_bundled_fonts():
    """Makes the bundled fonts available to this app only, without installing them."""
    fr_private = 0x10
    for path in glob.glob(os.path.join(RESOURCES, "fonts", "*.ttf")):
        ctypes.windll.gdi32.AddFontResourceExW(path, fr_private, 0)


def spaced(text):
    """Imitates Cairn's letter-spaced wordmark, which Tk cannot do natively."""
    return " ".join(text.upper())


def use_dark_title_bar(root):
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        immersive_dark_mode = 20
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, immersive_dark_mode, ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, OSError):
        pass  # Only the title bar colour is affected.


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.stop_flag = threading.Event()
        self.running = False

        # Tk scales fonts for high-DPI displays but not pixel sizes, so scale those here.
        self.scale = root.winfo_fpixels("1i") / 96
        width = min(self.px(760), root.winfo_screenwidth() - 40)
        height = min(self.px(720), root.winfo_screenheight() - 100)
        root.title("Stash")
        root.geometry(f"{width}x{height}")
        root.minsize(min(self.px(620), width), min(self.px(620), height))
        root.configure(bg=BASE)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        try:
            root.iconbitmap(default=os.path.join(RESOURCES, "assets", "icon.ico"))
        except tk.TclError:
            pass
        self.apply_style()

        page = tk.Frame(root, bg=BASE, padx=self.px(20), pady=self.px(16))
        page.pack(fill="both", expand=True)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(3, weight=1)

        self.build_header(page)
        self.build_hero(page)
        self.build_tool(page)
        self.build_results(page)

        self.links.focus_set()
        root.after(100, self.pump_events)

    # ---- appearance --------------------------------------------------------

    def px(self, size):
        return round(size * self.scale)

    def apply_style(self):
        families = set(tkfont.families())
        sans = "Manrope" if "Manrope" in families else "Segoe UI"
        mono = "JetBrains Mono" if "JetBrains Mono" in families else "Consolas"
        self.font_body = (sans, 10)
        self.font_button = (sans, 10, "bold")
        self.font_title = (sans, 27, "bold")
        self.font_label = (mono, 8)
        self.font_word = (mono, 10, "bold")

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=BASE, foreground=FG, font=self.font_body,
                        bordercolor=LINE_STRONG, lightcolor=BASE, darkcolor=BASE, focuscolor=LIME)
        style.configure("TButton", background=PANEL_2, foreground=FG, padding=(self.px(14), self.px(7)), font=self.font_body,
                        bordercolor=LINE_STRONG, lightcolor=PANEL_2, darkcolor=PANEL_2, relief="flat", focusthickness=1)
        style.map("TButton",
                  background=[("disabled", PANEL), ("active", LINE_STRONG)],
                  foreground=[("disabled", MUTED)],
                  lightcolor=[("disabled", PANEL), ("active", LINE_STRONG)],
                  darkcolor=[("disabled", PANEL), ("active", LINE_STRONG)],
                  bordercolor=[("disabled", LINE), ("active", MUTED)])
        style.configure("Lime.TButton", background=LIME, foreground=LIME_INK, font=self.font_button,
                        bordercolor=LIME, lightcolor=LIME, darkcolor=LIME)
        style.map("Lime.TButton",
                  background=[("disabled", LINE_STRONG), ("active", LIME_HOVER)],
                  foreground=[("disabled", MUTED)],
                  lightcolor=[("disabled", LINE_STRONG), ("active", LIME_HOVER)],
                  darkcolor=[("disabled", LINE_STRONG), ("active", LIME_HOVER)],
                  bordercolor=[("disabled", LINE_STRONG), ("active", LIME_HOVER)])
        style.configure("TEntry", fieldbackground=INK, foreground=FG, insertcolor=LIME, padding=self.px(6),
                        bordercolor=LINE_STRONG, lightcolor=INK, darkcolor=INK)
        style.map("TEntry", bordercolor=[("focus", LIME)])

        row_height = tkfont.Font(font=self.font_body).metrics("linespace") + self.px(8)
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=SOFT, rowheight=row_height,
                        borderwidth=0, font=self.font_body)
        style.map("Treeview", background=[("selected", PANEL)], foreground=[("selected", SOFT)])
        style.configure("Treeview.Heading", background=PANEL, foreground=MUTED, font=self.font_label, padding=(8, 8),
                        relief="flat", borderwidth=0, bordercolor=LINE, lightcolor=PANEL, darkcolor=PANEL)
        style.map("Treeview.Heading", background=[("active", PANEL)], foreground=[("active", MUTED)])
        style.configure("Vertical.TScrollbar", background=LINE_STRONG, troughcolor=PANEL, arrowcolor=MUTED,
                        bordercolor=PANEL, lightcolor=LINE_STRONG, darkcolor=LINE_STRONG, relief="flat")
        style.map("Vertical.TScrollbar", background=[("active", MUTED)])
        style.configure("Lime.Horizontal.TProgressbar", background=LIME, troughcolor=LINE_STRONG, thickness=self.px(3),
                        bordercolor=LINE_STRONG, lightcolor=LIME, darkcolor=LIME)

    def label(self, parent, text, color=MUTED, bg=PANEL):
        return tk.Label(parent, text=text.upper(), font=self.font_label, fg=color, bg=bg)

    def panel(self, parent):
        return tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightcolor=LINE, highlightthickness=1)

    def panel_head(self, panel, left, right):
        head = tk.Frame(panel, bg=PANEL, padx=self.px(14), pady=self.px(8))
        head.pack(fill="x")
        tk.Label(head, text="●", font=(self.font_label[0], 6), fg=LIME, bg=PANEL).pack(side="left", padx=(0, 8))
        self.label(head, left, SOFT).pack(side="left")
        self.label(head, right).pack(side="right")
        tk.Frame(panel, bg=LINE, height=1).pack(fill="x")

    def build_header(self, page):
        header = tk.Frame(page, bg=INK, padx=self.px(12), pady=self.px(7), highlightbackground=LINE, highlightthickness=1)
        header.grid(sticky="ew")
        tile_size, inner = self.px(24), self.px(8)
        tile = tk.Canvas(header, width=tile_size, height=tile_size, bg=LIME, highlightthickness=0)
        start = (tile_size - inner) // 2
        tile.create_rectangle(start, start, start + inner, start + inner, fill=LIME_INK, outline=LIME_INK)
        tile.pack(side="left", padx=(0, self.px(10)))
        tk.Label(header, text=spaced("Stash"), font=self.font_word, fg=FG, bg=INK).pack(side="left")
        tk.Label(header, text="Windows app", font=self.font_body, fg=SOFT, bg=INK).pack(side="right")
        tk.Label(header, text="●", font=(self.font_label[0], 6), fg=LIME, bg=INK).pack(side="right", padx=(0, 8))

    def build_hero(self, page):
        hero = tk.Frame(page, bg=BASE)
        hero.grid(sticky="ew", pady=(self.px(14), self.px(12)))
        self.label(hero, "Pinterest · Instagram · X / 01", LIME, BASE).pack(anchor="w", pady=(0, self.px(4)))
        line = tk.Frame(hero, bg=BASE)
        line.pack(anchor="w")
        tk.Label(line, text="Paste. Download.", font=self.font_title, fg=FG, bg=BASE).pack(side="left")
        tk.Label(line, text="Save.", font=self.font_title, fg=LIME_INK, bg=LIME, padx=self.px(7)).pack(side="left", padx=(self.px(9), 0))

    def build_tool(self, page):
        tool = self.panel(page)
        tool.grid(sticky="ew")
        self.panel_head(tool, "New download", "Photo · Video")
        body = tk.Frame(tool, bg=PANEL, padx=self.px(14), pady=self.px(12))
        body.pack(fill="x")
        body.columnconfigure(0, weight=1)

        self.label(body, "Post links, one per line").grid(sticky="w", pady=(0, self.px(5)))
        self.links = tk.Text(body, height=3, wrap="none", font=self.font_body, bg=INK, fg=FG, insertbackground=LIME,
                             selectbackground=LIME, selectforeground=LIME_INK, relief="flat",
                             padx=self.px(8), pady=self.px(8),
                             highlightthickness=1, highlightbackground=LINE_STRONG, highlightcolor=LIME)
        self.links.grid(sticky="ew")

        self.label(body, "Save to").grid(sticky="w", pady=(self.px(10), self.px(5)))
        folder_row = tk.Frame(body, bg=PANEL)
        folder_row.grid(sticky="ew")
        folder_row.columnconfigure(0, weight=1)
        self.folder = tk.StringVar(value=load_folder())
        ttk.Entry(folder_row, textvariable=self.folder, font=self.font_body).grid(row=0, column=0, sticky="ew")
        ttk.Button(folder_row, text="Browse…", command=self.browse).grid(row=0, column=1, padx=(self.px(8), 0))

        actions = tk.Frame(body, bg=PANEL)
        actions.grid(sticky="ew", pady=(self.px(10), 0))
        ttk.Button(actions, text="Paste", command=self.paste).pack(side="left")
        self.download_button = ttk.Button(actions, text="Download  ↗", style="Lime.TButton", command=self.start)
        self.download_button.pack(side="left", padx=self.px(8))
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Button(actions, text="Open folder", command=self.open_folder).pack(side="right")

    def build_results(self, page):
        results = self.panel(page)
        results.grid(sticky="nsew", pady=(self.px(12), 0))
        self.panel_head(results, "Downloads", "Status")

        table = tk.Frame(results, bg=PANEL, padx=self.px(6), pady=self.px(4))
        table.pack(fill="both", expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.rows = ttk.Treeview(table, columns=("video", "status"), show="", selectmode="none", height=4)
        self.rows.column("video", width=self.px(280), anchor="w")
        self.rows.column("status", width=self.px(390), anchor="w")
        self.rows.tag_configure("good", foreground=LIME)
        self.rows.tag_configure("bad", foreground=ROSE)
        self.rows.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, command=self.rows.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.rows.configure(yscrollcommand=scroll.set)

        self.bar = ttk.Progressbar(results, maximum=100, style="Lime.Horizontal.TProgressbar")
        self.bar.pack(fill="x")
        self.message = tk.StringVar(value="Ready.")
        tk.Label(results, textvariable=self.message, font=self.font_body, fg=SOFT, bg=PANEL, anchor="w",
                 padx=self.px(14), pady=self.px(8)).pack(fill="x")

    # ---- buttons ----------------------------------------------------------

    def browse(self):
        chosen = filedialog.askdirectory(initialdir=self.folder.get(), title="Choose where to save downloads")
        if chosen:
            self.folder.set(os.path.normpath(chosen))

    def paste(self):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            self.message.set("The clipboard is empty.")
            return
        current = self.links.get("1.0", "end").strip()
        self.links.insert("end", ("\n" if current else "") + text.strip() + "\n")

    def open_folder(self):
        folder = self.folder.get().strip() or DEFAULT_FOLDER
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    def stop(self):
        self.stop_flag.set()
        self.stop_button.configure(state="disabled")
        self.message.set("Stopping…")

    def on_close(self):
        self.stop_flag.set()
        self.root.destroy()

    def start(self):
        links = find_links(self.links.get("1.0", "end"))
        if not links:
            self.message.set("No supported links found. Paste a Pinterest, Instagram or X post link.")
            return
        folder = self.folder.get().strip() or DEFAULT_FOLDER
        self.folder.set(folder)
        save_folder(folder)

        self.links.delete("1.0", "end")
        for row in self.rows.get_children():
            self.rows.delete(row)
        jobs = [(self.rows.insert("", "end", values=(link, "Waiting")), link) for link in links]

        self.stop_flag.clear()
        self.running = True
        self.download_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.bar["value"] = 0
        threading.Thread(target=self.worker, args=(jobs, folder), daemon=True).start()

    # ---- background download thread ----------------------------------------

    def worker(self, jobs, folder):
        done = failed = 0
        for index, (row, link) in enumerate(jobs):
            if self.stop_flag.is_set():
                self.events.put(("row", row, None, "Not started", ""))
                continue
            self.events.put(("row", row, None, "Starting…", ""))

            def progress(fraction, row=row, index=index):
                self.events.put(("row", row, None, f"Downloading {fraction:.0%}", ""))
                self.events.put(("bar", (index + fraction) / len(jobs) * 100))

            try:
                paths, had_it = download_post(link, folder, progress, self.stop_flag.is_set)
                name = os.path.basename(paths[0]) + (f"  (+{len(paths) - 1} more)" if len(paths) > 1 else "")
                count = f"{len(paths)} files" if len(paths) > 1 else ""
                status = ("Already downloaded " if had_it else "Saved ") + count
                self.events.put(("row", row, name, status.strip(), "good"))
                done += 1
            except Stopped:
                self.events.put(("row", row, None, "Stopped", ""))
            except PinError as error:
                self.events.put(("row", row, None, str(error), "bad"))
                failed += 1
            except Exception as error:  # Never let one bad link end the whole batch.
                self.events.put(("row", row, None, f"Unexpected problem: {error}"[:160], "bad"))
                failed += 1
            self.events.put(("bar", (index + 1) / len(jobs) * 100))
        self.events.put(("finished", done, failed))

    def pump_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "row":
                    _, row, name, status, tag = event
                    values = (name or self.rows.set(row, "video"), status)
                    self.rows.item(row, values=values, tags=(tag,) if tag else ())
                    self.rows.see(row)
                    if not tag:
                        self.message.set(status)
                elif event[0] == "bar":
                    self.bar["value"] = event[1]
                elif event[0] == "finished":
                    self.finish(event[1], event[2])
        except queue.Empty:
            pass
        self.root.after(100, self.pump_events)

    def finish(self, done, failed):
        self.running = False
        self.download_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if self.stop_flag.is_set():
            self.message.set(f"Stopped. {done} saved. Paste a link again to resume it.")
        elif failed:
            self.message.set(f"Finished: {done} saved, {failed} failed.")
        else:
            self.message.set(f"Done: {done} saved to {self.folder.get()}")


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Keeps text sharp on scaled displays.
    except (AttributeError, OSError):
        pass
    load_bundled_fonts()
    root = tk.Tk()
    app = App(root)
    use_dark_title_bar(root)
    # Links passed on the command line start downloading right away.
    if len(sys.argv) > 1:
        app.links.insert("1.0", "\n".join(sys.argv[1:]))
        app.start()
    root.mainloop()


if __name__ == "__main__":
    main()
