import tkinter as tk
from tkinter import ttk
import subprocess, sys, os

from src.browser.constants import C_BG, C_SURFACE2, C_ACCENT, C_TEXT
from src.browser.database import FlakeDatabase
from src.browser.pages.search_page import SearchPage
from src.browser.pages.detail_page import DetailPage
from src.paths import IMAGES_DIR


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Flake Browser")
        self.geometry("1200x780")
        self.configure(bg=C_BG)
        self.minsize(900, 600)
        self._apply_styles()

        self.db = FlakeDatabase(IMAGES_DIR, autoload=False)

        self._search_page = SearchPage(self, self.db,
                                       on_select_flake=self._open_detail,
                                       on_close=self._close)
        self._detail_page = DetailPage(self, self.db,
                                       on_back=self._back_to_search)
        self._show_search()

        # Scanning images/ can be slow (large folders, first-time AV scans),
        # so load it in the background and refresh once it's ready.
        self.db.reload_async(on_done=self._search_page.run_search, root=self)

    def _apply_styles(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TCombobox",
                        fieldbackground=C_SURFACE2,
                        background=C_SURFACE2,
                        foreground=C_TEXT,
                        selectbackground=C_ACCENT,
                        selectforeground="#ffffff",
                        arrowcolor=C_TEXT,
                        borderwidth=0)
        style.map("TCombobox",
                  fieldbackground=[("readonly", C_SURFACE2)],
                  foreground     =[("readonly", C_TEXT)])
        style.configure("TScrollbar",
                        troughcolor=C_BG,
                        background=C_SURFACE2,
                        borderwidth=0,
                        arrowsize=12)

    def _show_search(self):
        self._detail_page.pack_forget()
        self._search_page.pack(fill="both", expand=True)

    def _open_detail(self, flake: dict):
        self._search_page.pack_forget()
        self._detail_page.load(flake)
        self._detail_page.pack(fill="both", expand=True)

    def _back_to_search(self):
        self._detail_page.pack_forget()
        self._search_page.run_search()
        self._search_page.pack(fill="both", expand=True)

    def _close(self):
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
