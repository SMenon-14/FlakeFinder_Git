"""pythonnet/MCC6 motion controller, motion/scan constants, Pulsedl panels."""
import sys
import os
import time
import tkinter as tk
from tkinter import ttk, messagebox

# pythonnet (.NET)
try:
    import clr
except Exception:
    print("pythonnet is required. Install: pip install pythonnet", file=sys.stderr)
    raise

import System
from System import Byte as _Byte, Single as _Single
from System import Array as _Array


def B(x: int):
    return _Byte(int(x))


def S(x: float):
    return _Single(float(x))


def ARR(t, n: int):
    return _Array.CreateInstance(t, int(n))


# -------------------------
# Defaults
# -------------------------
DEFAULT_PORT = "COM5"
DEFAULT_DLL_PATH = r"C:\Users\zhula\Desktop\TransferStage2\MCC6DLL.dll"

DEFAULT_SPEED_PARAM_INDEX = 1
DEFAULT_PULSEDL_PARAM_INDEX = 1

# MotionConfig.ini shows Axis_R and Axis_Z.
# Best-guess mapping (change if your controller uses different axis indices):
AXIS_INDEX = {"X":0, "Y":1, "Z":2, "A":3, "B":4, "C":5}


DEFAULT_AXIS_SPEED = {
    "X": 0.5,
    "Y": 0.5,
    "Z": 0.1,
    "A": 0.5,
    "B": 0.5,
    "C": 0.5,
}

DEFAULT_JOG_ACCEL = 1.0
MAX_EDGE_PROBE_STEPS = 200
MIN_HITS_PER_ROW = 2
DEFAULT_PINK_FRACTION_THRESHOLD = 0.01
DEFAULT_PINK_CONNECTED_THRESHOLD = 0.005
SAVE_WAIT_TIMEOUT_S = 12.0
SAVE_WAIT_POLL_DELAY_S = 0.05
SAVE_WAIT_STABLE_POLLS = 1
IMAGE_READ_RETRIES = 10
IMAGE_READ_RETRY_DELAY_S = 0.1
SAVE_RETRY_ATTEMPTS = 3
SAVE_DIALOG_OPEN_DELAY_S = 0.7
SAVE_PATH_PASTE_DELAY_S = 0.03
SAVE_SUBMIT_DELAY_S = 0.15
SAVE_OVERWRITE_CONFIRM_DELAY_S = 0.15
SAVE_RETRY_BACKOFF_S = 0.2
SAVE_CLOSE_WINDOW_DELAY_S = 0.1
SCAN_MOVE_TIMEOUT_S = 20.0
SCAN_MOVE_POLL_DELAY_S = 0.05
SCAN_MOVE_STABLE_POLLS = 3

SCAN_CORNER_ORDER = (
    ("Top Left", "TL"),
    ("Top Right", "TR"),
    ("Bottom Left", "BL"),
    ("Bottom Right", "BR"),
)

class MCCController:
    """
    Wrapper around SPLibClass that:
    - Lists available MoCtrCard_* methods
    - Uses timed jogging when available to mimic joystick behavior
    - Prioritizes hard stop so "lift finger = stop NOW"
    """

    def __init__(self):
        self.sp = None
        self.dll_loaded = False
        self.connected = False
        self.dll_dir_added = False
        self.dll_path = None
        self._cache = {}

    def load_dll(self, dll_path: str):
        if not os.path.exists(dll_path):
            raise FileNotFoundError(f"Cannot find DLL: {dll_path}")
        self.dll_path = dll_path

        dll_dir = os.path.dirname(dll_path)
        if dll_dir and not self.dll_dir_added:
            try:
                os.add_dll_directory(dll_dir)
            except Exception:
                os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")
            self.dll_dir_added = True

        clr.AddReference(dll_path)
        from SerialPortLibrary import SPLibClass  # type: ignore

        self.sp = SPLibClass()
        self.dll_loaded = True
        self._cache.clear()

    def list_methods(self, prefix="MoCtrCard_"):
        if not self.sp:
            raise RuntimeError("DLL not loaded (self.sp is None)")
        names = [n for n in dir(self.sp) if n.startswith(prefix)]
        names.sort()
        return names

    def dump_methods(self, out_path: str, prefix="MoCtrCard_"):
        names = self.list_methods(prefix=prefix)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"DLL: {self.dll_path}\n")
            f.write(f"Prefix: {prefix}\n")
            f.write(f"Count: {len(names)}\n\n")
            for n in names:
                f.write(n + "\n")
        return out_path, len(names)

    def _get_first_existing(self, candidates):
        if not self.sp:
            raise RuntimeError("DLL not loaded")
        for name in candidates:
            fn = getattr(self.sp, name, None)
            if callable(fn):
                return name, fn
        return None, None

    def connect(self, com_port: str, logger=lambda m: None):
        if self.sp is None:
            raise RuntimeError("DLL not loaded")
        ret = self.sp.MoCtrCard_Initial(com_port)
        self.connected = True
        logger(f"MoCtrCard_Initial('{com_port}') -> {ret}")
        return ret

    def disconnect(self, logger=lambda m: None):
        if not self.sp:
            return
        for name in ("MoCtrCard_QuiteMotionControl", "MoCtrCard_Unload"):
            fn = getattr(self.sp, name, None)
            if fn:
                try:
                    r = fn()
                    logger(f"{name}() -> {r}")
                except Exception as e:
                    logger(f"{name} error: {e}")
        self.connected = False

    def send_param(self, axis: int, param_index: int, value: float):
        fn = getattr(self.sp, "MoCtrCard_SendPara", None)
        if fn is None:
            raise AttributeError("MoCtrCard_SendPara not found")
        return fn(B(axis), B(param_index), S(value))

    # -------------------------
    # Continuous jog (fallback)
    # -------------------------
    def at_speed(self, axis: int, vel: float, acc: float):
        if "at_speed" in self._cache:
            _name, fn = self._cache["at_speed"]
        else:
            candidates = [
                "MoCtrCard_MCrlAxisMoveAtSpd",
                "MoCtrCard_MCrlAxisMoveAtSpdWithInTime",
                "MoCtrCard_MCrlAxisAtSpd",
                "MoCtrCard_MCtrlAxisAtSpd",
                "MoCtrCard_MCtrlAxisAtSpeed",
                "MoCtrCard_MCrlAxisJog",
                "MoCtrCard_MCtrlAxisJog",
                "MoCtrCard_MCrlAxisVelMove",
                "MoCtrCard_MCtrlAxisVelMove",
                "MoCtrCard_AxisAtSpd",
                "MoCtrCard_AxisJog",
            ]
            _name, fn = self._get_first_existing(candidates)
            if fn is None:
                available = self.list_methods("MoCtrCard_")
                raise AttributeError(
                    "No jog/at-speed function found in DLL.\n" + "\n".join(available[:200])
                )
            self._cache["at_speed"] = (_name, fn)

        try:
            return fn(B(axis), S(vel), S(acc))
        except TypeError:
            try:
                return fn(B(axis), S(vel))
            except TypeError:
                return fn(B(axis), S(vel), S(acc), S(0.0))

    # -------------------------
    # Timed jog (joystick-like)
    # -------------------------
    def at_speed_timed(self, axis: int, vel: float, acc: float, dt_s: float):
        """
        Joystick-like behavior:
        - Command a short jog for dt_s seconds.
        - If you stop sending commands, motion stops quickly (no long coasting).
        """
        if not self.sp:
            raise RuntimeError("DLL not loaded")

        fn = getattr(self.sp, "MoCtrCard_MCrlAxisMoveAtSpdWithInTime", None)
        if fn is None:
            # fallback to continuous jog if timed version doesn't exist
            return self.at_speed(axis, vel, acc)

        try:
            return fn(B(axis), S(vel), S(acc), S(dt_s))
        except TypeError:
            # some variants: (axis, vel, time)
            return fn(B(axis), S(vel), S(dt_s))

    # -------------------------
    # Stop (hard stop priority)
    # -------------------------
    def stop(self, axis: int):
        """
        Priority:
          1) EmergencyStopAxisMov  (hardest stop -> best for "finger up = stop NOW")
          2) ParaStopAxisMov       (ramped stop fallback)
        """
        if "stop" in self._cache:
            _name, fn = self._cache["stop"]
        else:
            candidates = [
                "MoCtrCard_EmergencyStopAxisMov",
                "MoCtrCard_ParaStopAxisMov",
                "MoCtrCard_StopAxisMov",
                "MoCtrCard_StopAxisMove",
                "MoCtrCard_StopAxis",
                "MoCtrCard_Stop",
                "MoCtrCard_MCrlStopAxisMov",
                "MoCtrCard_MCtrlStopAxisMov",
                "MoCtrCard_MCrlAxisStop",
                "MoCtrCard_MCtrlAxisStop",
            ]
            _name, fn = self._get_first_existing(candidates)
            if fn is None:
                available = self.list_methods("MoCtrCard_")
                raise AttributeError(
                    "No stop function found in DLL.\n" + "\n".join(available[:200])
                )
            self._cache["stop"] = (_name, fn)

        try:
            return fn(B(axis))
        except TypeError:
            return fn(B(axis), B(0))

    def move_abs(self, axis: int, position: float, v: float = None, a: float = None):
        fn = getattr(self.sp, "MoCtrCard_MCrlAxisAbsMove", None)
        if fn is None:
            raise AttributeError("MoCtrCard_MCrlAxisAbsMove not found")
        if v is None or a is None:
            return fn(B(axis), S(position))
        return fn(B(axis), S(position), S(v), S(a))

    def get_pos(self, axis: int):
        buf = ARR(System.Single, 1)
        fn = getattr(self.sp, "MoCtrCard_GetAxisPos", None)
        if fn is None:
            raise AttributeError("MoCtrCard_GetAxisPos not found")
        ret = fn(B(axis), buf)
        return ret, float(buf[0])

    def read_param(self, axis: int, param_index: int):
        buf = ARR(System.Single, 1)
        fn = getattr(self.sp, "MoCtrCard_ReadPara", None)
        if fn is None:
            raise AttributeError("MoCtrCard_ReadPara not found")
        ret = fn(B(axis), B(param_index), buf)
        return ret, float(buf[0])



class PulsedlPanel(ttk.LabelFrame):
    def __init__(self, master, app, axis_key: str, param_index: int):
        super().__init__(master, text=f"{axis_key} PULSEDL", padding=8)
        self.app = app
        self.axis_key = axis_key
        self.param_index = param_index
        self._build()

    def _build(self):
        ttk.Label(self, text="Value").grid(row=0, column=0, sticky="e")
        self.var_val = tk.StringVar(value="0.5")
        ttk.Entry(self, textvariable=self.var_val, width=10).grid(row=0, column=1, sticky="w", padx=4)

        self.var_idx = tk.StringVar(value=str(self.param_index))

        self.scale = ttk.Scale(self, from_=0.0, to=1.0, orient="horizontal", command=self._on_scale)
        try:
            self.scale.set(float(self.var_val.get()))
        except Exception:
            self.scale.set(0.5)
        self.scale.grid(row=1, column=0, columnspan=4, sticky="we", padx=4, pady=(4, 2))

        ttk.Button(self, text="Apply", command=self._apply).grid(row=0, column=3, padx=6)
        self.columnconfigure(2, weight=1)

    def _on_scale(self, val):
        try:
            self.var_val.set(f"{float(val):.3f}")
        except Exception:
            pass

    def _apply(self):
        try:
            val = float(self.var_val.get())
            pidx = int(self.var_idx.get())
            axis = AXIS_INDEX[self.axis_key]
        except Exception as e:
            messagebox.showerror(f"{self.axis_key} PULSEDL Error", str(e))
            return
        future = self.app.controller.submit("send_param", axis, pidx, val)
        self.app._watch_motion_future(
            future,
            on_success=lambda result: self.app.log(
                f"[{self.axis_key}] PULSEDL {val} (param {pidx}) -> ret {result}"
            ),
            error_title=f"{self.axis_key} PULSEDL Error",
            error_prefix=f"[{self.axis_key} PULSEDL]",
        )


class PairPulsedlPanel(ttk.LabelFrame):
    def __init__(self, master, app, title: str, axis_keys: tuple, param_index: int):
        super().__init__(master, text=title, padding=8)
        if len(axis_keys) != 2:
            raise ValueError("axis_keys must contain exactly 2 axis labels")
        self.app = app
        self.axis_keys = axis_keys
        self.param_index = param_index
        self._build()

    def _build(self):
        ttk.Label(self, text="Value").grid(row=0, column=0, sticky="e")
        self.var_val = tk.StringVar(value="0.5")
        ttk.Entry(self, textvariable=self.var_val, width=10).grid(row=0, column=1, sticky="w", padx=4)

        self.var_idx = tk.StringVar(value=str(self.param_index))

        self.scale = ttk.Scale(self, from_=0.0, to=1.0, orient="horizontal", command=self._on_scale)
        try:
            self.scale.set(float(self.var_val.get()))
        except Exception:
            self.scale.set(0.5)
        self.scale.grid(row=1, column=0, columnspan=4, sticky="we", padx=4, pady=(4, 2))

        ttk.Button(self, text="Apply", command=self._apply).grid(row=0, column=3, padx=6)
        self.columnconfigure(2, weight=1)

    def _on_scale(self, val):
        try:
            self.var_val.set(f"{float(val):.3f}")
        except Exception:
            pass

    def _apply(self):
        try:
            val = float(self.var_val.get())
            pidx = int(self.var_idx.get())
        except Exception as e:
            messagebox.showerror(f"{self['text']} PULSEDL Error", str(e))
            return
        calls = [
            ("send_param", (AXIS_INDEX[key], pidx, val), {})
            for key in self.axis_keys
        ]
        future = self.app.controller.submit_batch(calls)

        def _applied(results):
            msg = ", ".join(
                f"{key}:{result}"
                for key, result in zip(self.axis_keys, results)
            )
            self.app.log(
                f"[{self.axis_keys[0]}{self.axis_keys[1]}] "
                f"PULSEDL {val} (param {pidx}) -> {msg}"
            )

        self.app._watch_motion_future(
            future,
            on_success=_applied,
            error_title=f"{self['text']} PULSEDL Error",
            error_prefix=f"[{self['text']} PULSEDL]",
        )
