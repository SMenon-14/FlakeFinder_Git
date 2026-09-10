import tkinter as tk

from src.browser.constants import (
    C_BG, C_SURFACE2, C_ACCENT, C_ACCENT2, C_TEXT, C_MUTED,
    C_USED, C_AVAIL,
    FONT_BODY, FONT_SMALL, FONT_HEAD,
)


class StyledButton(tk.Label):
    def __init__(self, parent, text, command=None,
                 active=False, width=None, **kwargs):
        bg = C_ACCENT2 if active else C_SURFACE2
        super().__init__(parent, text=text,
                         bg=bg, fg="#ffffff" if active else C_TEXT,
                         font=FONT_BODY, padx=10, pady=5,
                         cursor="hand2", relief="flat",
                         width=width or 0, **kwargs)
        self._command = command
        self._active  = active
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>",    self._on_enter)
        self.bind("<Leave>",    self._on_leave)

    def _on_click(self, _=None):
        if self._command:
            self._command()

    def _on_enter(self, _=None):
        if not self._active:
            self.config(bg=C_ACCENT, fg="#ffffff")

    def _on_leave(self, _=None):
        if self._active:
            self.config(bg=C_ACCENT2, fg="#ffffff")
        else:
            self.config(bg=C_SURFACE2, fg=C_TEXT)

    def set_active(self, active: bool):
        self._active = active
        self.config(bg=C_ACCENT2 if active else C_SURFACE2,
                    fg="#ffffff" if active else C_TEXT)


class ToggleButton(StyledButton):
    def __init__(self, parent, text, on_toggle=None, initial=False, **kwargs):
        self._toggled   = initial
        self._on_toggle = on_toggle
        super().__init__(parent, text=text, active=initial,
                         command=self._toggle, **kwargs)

    def _toggle(self):
        self._toggled = not self._toggled
        self.set_active(self._toggled)
        if self._on_toggle:
            self._on_toggle(self._toggled)

    def get(self): return self._toggled

    def set(self, value: bool):
        self._toggled = value
        self.set_active(value)


class LabeledField(tk.Frame):
    def __init__(self, parent, label, width=10, **kwargs):
        super().__init__(parent, bg=C_BG, **kwargs)
        tk.Label(self, text=label, bg=C_BG, fg=C_MUTED,
                 font=FONT_HEAD).pack(anchor="w")
        self.var = tk.StringVar()
        tk.Entry(self, textvariable=self.var,
                 bg=C_SURFACE2, fg=C_TEXT, insertbackground=C_TEXT,
                 relief="flat", font=FONT_BODY, width=width).pack(fill="x")

    def get(self):   return self.var.get().strip()
    def clear(self): self.var.set("")


class StatusBadge(tk.Label):
    def __init__(self, parent, used: bool, **kwargs):
        text  = "Used"  if used else "Available"
        color = C_USED  if used else C_AVAIL
        super().__init__(parent, text=text, bg=color,
                         fg="white", font=FONT_SMALL,
                         padx=6, pady=2, **kwargs)
