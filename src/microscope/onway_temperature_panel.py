"""Modbus/Flask/MQTT temperature backend + TemperatureControlPanel UI."""
import os
import time
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog

import csv
from datetime import datetime, timedelta

# Flask / REST
from flask import Flask, request
from flask_restful import Api, Resource

# Modbus
from pymodbus.client import ModbusSerialClient as ModbusClient
import struct
import serial
import serial.tools.list_ports
from serial import SerialException
from typing import Optional

# Matplotlib (TkAgg)
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.ticker import MaxNLocator

import configparser
import json


from src.microscope.onway_app_config import (
    cfg, CFG_PATH, BASE_FONT, BIG_FONT, UNIT_FONT, ENTRY_FONT, ICON_PATH,
    ACCENT_COLOR, WHITE_BG, APP_BG, POP_FONT, BTN_FONT,
)
from src.microscope.onway_temperature_service import (
    PymodbusTemperatureTransport,
    TemperatureService,
)


# ---- security & logging ----
SECRET_TOKEN = cfg.get("Security", "secret_token", fallback="")
LOG_ENCODING = cfg.get("Logging", "encoding",     fallback="utf-8-sig")



# ---- serial / Modbus ----
SERIAL_OPTS = {
    "port":     cfg.get("Serial", "port",     fallback="COM4"),
    "baudrate": cfg.getint("Serial", "baudrate", fallback=9600),
    "parity":   cfg.get("Serial", "parity",   fallback="N"),
    "stopbits": cfg.getint("Serial", "stopbits", fallback=1),
    "bytesize": cfg.getint("Serial", "bytesize", fallback=8),
}
# Track current COM so we can reopen when needed
CURRENT_COM_PORT = SERIAL_OPTS.get("port", "COM4")

def _new_modbus_client(port_name: str):
    return ModbusClient(
        port     = port_name,
        baudrate = SERIAL_OPTS["baudrate"],
        parity   = SERIAL_OPTS["parity"],
        stopbits = SERIAL_OPTS["stopbits"],
        bytesize = SERIAL_OPTS["bytesize"],
        timeout  = 3,
    )

def _modbus_reopen(port_name: str) -> bool:
    """Safely close & reopen the serial Modbus client on the last port."""
    global client
    try:
        if client:
            try: client.close()
            except Exception: pass
    except Exception:
        pass
    cli = _new_modbus_client(port_name)
    ok = False
    try:
        ok = bool(cli.connect())
    except Exception:
        ok = False
    if ok:
        client = cli
    return ok

def _ensure_modbus_connected() -> bool:
    """Ensure client is open. If not, try to (re)open on CURRENT_COM_PORT."""
    global client
    if client is None:
        return _modbus_reopen(CURRENT_COM_PORT)
    try:
        # pymodbus SerialClient.connect() is idempotent; safe to call
        return bool(client.connect())
    except Exception:
        return _modbus_reopen(CURRENT_COM_PORT)


# ---- constants / addresses (preserved) ----
SET_TEMP_READ_REG  = 18505   # Set Temperature mirror (0.1 °C), read-only
START_SWITCH_COIL  = 10010   # Start Switch (coil), read/write
PARAM_REG_ADDRESSES = {18506, 18507, 18508, 18509, 18523, 18501, 2036}

# Flask & Modbus client globals (preserved)
app = Flask(__name__)
api = Api(app)
client = None
stop_threads = False

# Shared data stores
data_store = {
    "Temperature": None, "SetTemperature": None, "Power": None, "PowerLimit": None,
    "Segment": None, "SegmentLeft": None, "P": None, "I": None, "D": None,
    "Cycle": None, "Correction": None, "Filter": None, "OvertempAlarm": None,
    "manual_override": False
}
data_store_lock = threading.RLock()
last_param_values = {k: None for k in ("P", "I", "D", "Cycle", "Correction", "Filter", "OvertempAlarm")}


def _publish_temperature_update(changes):
    with data_store_lock:
        data_store.update(changes)

def check_token():
    tok = request.headers.get("Authorization")
    return tok == SECRET_TOKEN if tok else True

# --- Modbus helpers (preserved) ---
def read_register(cli, address, slave=10, retries=5):
    try:
        for _ in range(retries):
            rsp = cli.read_holding_registers(address, count=1, device_id=slave)
            if not rsp.isError():
                val = rsp.registers[0] / 10
                return val * 10 if address in [2036, 18523, 2092,
                                               18506, 18507, 18508,
                                               18509, 18501] else val
            time.sleep(0.1)
    except SerialException as e:
        print("[Modbus] serial error:", e)
    return None

def read_register(cli, address, slave=10, retries=5):
    if not _ensure_modbus_connected():
        return None
    try:
        for _ in range(retries):
            rsp = client.read_holding_registers(address, count=1, device_id=slave)
            if not rsp.isError():
                val = rsp.registers[0] / 10
                return val * 10 if address in [2036, 18523, 2092, 18506, 18507, 18508, 18509, 18501] else val
            time.sleep(0.1)
    except SerialException as e:
        # Try one reconnect on invalid handle, then one quick retry
        if _modbus_reopen(CURRENT_COM_PORT):
            try:
                rsp = client.read_holding_registers(address, count=1, device_id=slave)
                if not rsp.isError():
                    val = rsp.registers[0] / 10
                    return val * 10 if address in [2036, 18523, 2092, 18506, 18507, 18508, 18509, 18501] else val
            except Exception:
                pass
        print("[Modbus] serial error:", e)
    except Exception as e:
        print("[Modbus] read error:", e)
    return None

def write_register(client_unused, address, value, slave=10):
    if not _ensure_modbus_connected():
        return False
    try:
        return client.write_register(address, int(round(value * 10)), device_id=slave)
    except SerialException as e:
        if _modbus_reopen(CURRENT_COM_PORT):
            try:
                return client.write_register(address, int(round(value * 10)), device_id=slave)
            except Exception:
                pass
        print("[Modbus] serial error:", e)
        return False
    except Exception as e:
        print("[Modbus] write error:", e)
        return False

def write_coil(client_unused, address, value, slave=10):
    if not _ensure_modbus_connected():
        return False
    try:
        rsp = client.write_coil(address, bool(value), device_id=slave)
        return not rsp.isError()
    except SerialException as e:
        if _modbus_reopen(CURRENT_COM_PORT):
            try:
                rsp = client.write_coil(address, bool(value), device_id=slave)
                return not rsp.isError()
            except Exception:
                pass
        print("[Modbus] serial error:", e)
        return False
    except Exception as e:
        print("[Modbus] coil error:", e)
        return False


# The combined-control UI uses TemperatureService below. Keep the older full
# panel and Flask API safe as well by serializing their legacy helper calls.
_legacy_modbus_io_lock = threading.RLock()
_legacy_modbus_reopen = _modbus_reopen
_legacy_read_register = read_register
_legacy_write_register = write_register
_legacy_write_coil = write_coil


def _modbus_reopen(*args, **kwargs):
    with _legacy_modbus_io_lock:
        return _legacy_modbus_reopen(*args, **kwargs)


def read_register(*args, **kwargs):
    with _legacy_modbus_io_lock:
        return _legacy_read_register(*args, **kwargs)


def write_register(*args, **kwargs):
    with _legacy_modbus_io_lock:
        return _legacy_write_register(*args, **kwargs)


def write_coil(*args, **kwargs):
    with _legacy_modbus_io_lock:
        return _legacy_write_coil(*args, **kwargs)


def _timestamp_parts():
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m"), now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")

def _log_csv(prefix, header, row, log_dir):
    ts, month, date_s, time_s = _timestamp_parts()
    fn = os.path.join(log_dir, f"{prefix}_{month}.csv")
    is_new = not os.path.exists(fn)
    try:
        with open(fn, "a", newline="", encoding=LOG_ENCODING) as f:
            w = csv.writer(f)
            if is_new: w.writerow(header)
            w.writerow([ts, date_s, time_s] + row)
    except PermissionError:
        print(f"[Warning] Cannot write to '{fn}' – file is open?")

def update_modbus_values_loop(app_ref):
    global stop_threads
    try:
        while not stop_threads and app_ref.running:
            temp = read_register(client, 18504)
            set_mirror = read_register(client, SET_TEMP_READ_REG)
            power = read_register(client, 2036)
            new_params = {
                "P": read_register(client, 18506),
                "I": read_register(client, 18507),
                "D": read_register(client, 18508),
                "Cycle": read_register(client, 18509),
                "Correction": read_register(client, 18550),
                "Filter": read_register(client, 18501),
                "OvertempAlarm": read_register(client, 2490),
            }
            if set_mirror is not None:
                data_store["SetTemperature"] = set_mirror
            data_store.update(Temperature=temp, Power=power, **new_params)

            if app_ref.data_save_var.get() and temp is not None and power is not None:
                _log_csv("TEMP_LOG", ["Timestamp","Date","Time","Temperature","Power"], [temp, power], app_ref.log_dir.get())
                if any(new_params[k] is not None and last_param_values[k] != new_params[k] for k in new_params):
                    _log_csv(
                        "PARAMETER_LOG",
                        ["Timestamp","Date","Time"] + list(new_params.keys()),
                        list(new_params.values()),
                        app_ref.log_dir.get()
                    )
                    last_param_values.update(new_params)
            time.sleep(0.1)
    except Exception as e:
        print("[Modbus] loop stopped:", e)

class DataAPI(Resource):
    def get(self):
        return data_store
    def post(self):
        val = request.json.get("SetTemperature")
        if val is not None:
            write_register(client, 3000, float(val))
            data_store["SetTemperature"] = float(val)
            return {"message": "Set Temperature updated", "SetTemperature": val}
        return {"error": "Invalid input"}, 400

class SetpointAPI(Resource):
    def post(self):
        if not check_token():
            return {"error": "Unauthorized"}, 401
        val = request.json.get("SetTemperature")
        if val is not None:
            write_register(client, 3000, float(val))
            data_store["SetTemperature"] = float(val)
            print(f"[✅] Setpoint manually updated to {val}°C (manual override)")
            return {"message": "Set Temperature updated", "SetTemperature": val}
        return {"error": "Invalid input"}, 400

api.add_resource(DataAPI, "/api/data")
api.add_resource(SetpointAPI, "/setpoint")

def run_flask_app(port=5000):
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

class MQTTManager:
    def __init__(self, *, host, port, topic_pub,
                 client_id, username, password,
                 setpoint_cmd_topic, discovery_prefix,
                 qos=0, retain=False, enable=True,
                 gui_ref=None):
        self.enable = enable
        if not self.enable:
            self.client = None
            return
        import paho.mqtt.client as mqtt
        self.topic_pub          = topic_pub
        self.setpoint_cmd_topic = setpoint_cmd_topic
        self.discovery_prefix   = discovery_prefix.rstrip('/')
        self.qos     = int(qos)
        self.retain  = bool(retain)
        self.gui_ref = gui_ref
        if not client_id:
            client_id = f"VCLPublisher_{os.getpid()}"
        self.client = mqtt.Client(client_id=client_id, clean_session=True)
        if username:
            self.client.username_pw_set(username, password or None)
        self.client.on_connect    = self._on_connect
        self.client.on_message    = self._on_message
        self.client.on_disconnect = lambda c, u, r: print("[MQTT] disconnected")
        try:
            self.client.connect(host, int(port), keepalive=60)
            self.client.loop_start()
        except Exception as e:
            print(f"[MQTT] connect error: {e}")
            self.enable = False

    def publish_discovery(self):
        if not self.enable:
            return
        if hasattr(self.gui_ref, "cfg"):
            dev = self.gui_ref.cfg.get
            device_info = {
                "identifiers":  [dev("Device","identifiers",  fallback="onway_tempctl")],
                "name":         dev("Device","name",         fallback="Onway Temperature Controller"),
                "manufacturer": dev("Device","manufacturer", fallback="ONWAY"),
                "model":        dev("Device","model",        fallback="OTC-9600"),
            }
        else:
            device_info = {
                "identifiers": ["onway_tempctl"],
                "name": "Onway Temperature Controller",
                "manufacturer": "ONWAY",
                "model": "OTC-9600"
            }
        # sensors
        sensors = {
            "Temperature": {"unit": "°C", "device_class": "temperature"},
            "Power":       {"unit": "%",  "device_class": None},
            "Setpoint":    {"unit": "°C", "device_class": None},
        }
        for key, conf in sensors.items():
            topic = f"{self.discovery_prefix}/sensor/onway_{key.lower()}/config"
            payload = {
                "name": f"Onway {key}",
                "state_topic": self.topic_pub,
                "value_template": f"{{{{ value_json.{key} }}}}",
                "unique_id": f"onway_{key.lower()}",
                "unit_of_measurement": conf["unit"],
                "device_class": conf["device_class"],
                "device": device_info
            }
            self.client.publish(topic, json.dumps(payload), retain=True)
        # writable number
        num_topic = f"{self.discovery_prefix}/number/onway_setpoint/config"
        payload = {
            "name": "Onway Setpoint",
            "state_topic":  self.topic_pub,
            "command_topic":self.setpoint_cmd_topic,
            "command_template": '{"Setpoint": {{ value }} }',
            "value_template": "{{ value_json.Setpoint }}",
            "unit_of_measurement": "°C",
            "min": 0, "max": 1200, "step": 1,
            "mode": "box",
            "unique_id": "onway_setpoint_number",
            "device": device_info
        }
        self.client.publish(num_topic, json.dumps(payload), retain=True)

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("[MQTT] connected")
            client.subscribe(self.setpoint_cmd_topic, qos=self.qos)
            self.publish_discovery()
        else:
            print(f"[MQTT] connect rc={rc}")

    def _on_message(self, client, userdata, msg):
        try:
            raw = msg.payload.decode()
            try:
                obj = json.loads(raw)
                new_sv = float(obj["Setpoint"]) if isinstance(obj, dict) else float(raw)
            except (json.JSONDecodeError, KeyError, ValueError):
                new_sv = float(raw)
            print(f"[MQTT] received new set-point {new_sv}")
            if self.gui_ref:
                self.gui_ref.apply_remote_setpoint(new_sv)
        except Exception as e:
            print(f"[MQTT] msg error: {e}")

    def publish(self, payload_dict):
        if self.enable and self.client:
            self.client.publish(
                self.topic_pub, json.dumps(payload_dict),
                qos=self.qos, retain=self.retain
            )

API_PORT = cfg.getint("General", "api_port", fallback=5000)



class TemperatureControlPanel(tk.Frame):
    """Frame-wrapped version of TemperatureControlApp; algorithms preserved."""
    def __init__(self, master):
        super().__init__(master, bg=WHITE_BG)
        self.pack(fill="both", expand=True)

        self.cfg = cfg
        self.mqtt_mgr = None
        self.running = True

        # shared state
        self.client = None
        self.api_thread = None
        self.modbus_thread = None

        # UI state
        self.set_point_var = tk.StringVar()
        self.com_var       = tk.StringVar(value=SERIAL_OPTS["port"])
        self.api_on_var    = tk.BooleanVar(value=self.cfg.getboolean("General", "api_enabled", fallback=True))
        self.data_save_var = tk.BooleanVar(value=self.cfg.getboolean("General", "save_logs",  fallback=True))
        self.port_var      = tk.StringVar(value=str(API_PORT))
        self.log_dir       = tk.StringVar(value=self.cfg.get("Logging", "directory", fallback=os.getcwd()))

        # plotting buffers
        self.times = []
        self.temps = []
        self.set_vals = []
        self.last_plot_ts = datetime.min

        # build UI (unchanged structure)
        self._after_id = None
        self._build_layout()
        self._build_left_panel()
        self._build_config_panel()
        self._build_chart_panel()
        self._build_status_bar()
        self._bind_keys()
        self._schedule_ui_refresh()

    # utility: get toplevel for title operations
    def _root(self):
        return self.winfo_toplevel()

    def _build_status_bar(self):
        bar = tk.Frame(self, bg=WHITE_BG, bd=1, relief="solid")
        bar.pack(side=tk.BOTTOM, fill="x")
        self.status_label = tk.Label(bar, text="Ready", bg=WHITE_BG, fg="black", font=BASE_FONT)
        self.status_label.pack(side=tk.LEFT, padx=8, pady=2)

    def _schedule_ui_refresh(self):
        # Guard against callbacks firing after the widget is destroyed
        if not self.running or not self.winfo_exists():
            return
        try:
            self._refresh_readouts()
        except Exception:
            return
        if self.running and self.winfo_exists():
            self._after_id = self.after(250, self._schedule_ui_refresh)

    def _build_layout(self):
        main = tk.Frame(self, bg=WHITE_BG)
        main.pack(fill="both", expand=True)

        # (Title removed as requested)

        content = tk.Frame(main, bg=WHITE_BG)
        content.pack(fill="both", expand=True)

        self.left_frame  = tk.Frame(content,  bg=WHITE_BG)
        self.right_frame = tk.Frame(content,  bg=WHITE_BG, bd=1, relief="solid")

        self.left_frame.pack(side=tk.LEFT,  fill="y",   padx=12, pady=10)
        self.right_frame.pack(side=tk.RIGHT, fill="both", expand=True, padx=12, pady=10)

    def _build_left_panel(self):
        temp_f = tk.Frame(self.left_frame, bg=WHITE_BG)
        temp_f.pack(pady=6)
        self.temp_label_number = tk.Label(temp_f, text="--", font=BIG_FONT,  bg=WHITE_BG)
        self.temp_label_number.pack(side=tk.LEFT)
        self.temp_label_unit = tk.Label(temp_f, text="°C", font=UNIT_FONT,  bg=WHITE_BG)
        self.temp_label_unit.pack(side=tk.LEFT, padx=(10, 0))

        ttk.Separator(self.left_frame, orient="horizontal").pack(fill="x", padx=6, pady=(4,4))

        pow_f = tk.Frame(self.left_frame,  bg=WHITE_BG)
        pow_f.pack(pady=6)
        tk.Label(pow_f, text="Power:", font=BASE_FONT,  bg=WHITE_BG).pack(side=tk.LEFT)
        self.power_bar = ttk.Progressbar(pow_f, orient="horizontal", length=100, mode="determinate")
        self.power_bar["maximum"] = 100
        self.power_bar.pack(side=tk.LEFT, padx=5)
        self.power_percent_label = tk.Label(pow_f, text="--%", font=ENTRY_FONT,  bg=WHITE_BG)
        self.power_percent_label.pack(side=tk.LEFT, padx=5)

        sp_f = tk.Frame(self.left_frame,  bg=WHITE_BG)
        sp_f.pack(pady=6)
        tk.Label(sp_f, text="SetPoint:", font=BASE_FONT,  bg=WHITE_BG).pack(side=tk.LEFT, padx=5)
        self.set_point_entry = tk.Entry(sp_f, textvariable=self.set_point_var,
                                        font=ENTRY_FONT, width=8, justify="center")
        self.set_point_entry.pack(side=tk.LEFT)
        self.set_point_entry.bind("<Return>", self.on_enter_set_point)
        self.entry_typing = False
        self.set_point_entry.bind("<Key>",    self._on_entry_keypress)
        self.set_point_entry.bind("<FocusOut>", self._on_entry_focus_out)
        tk.Label(sp_f, text="°C", font=BASE_FONT,  bg=WHITE_BG).pack(side=tk.LEFT, padx=5)

    def _on_entry_keypress(self, event): self.entry_typing = True
    def _on_entry_focus_out(self, event): self.entry_typing = False

    def _build_config_panel(self):
        # a small titled group so it doesn't "blend in"
        cfgf = ttk.LabelFrame(self.left_frame, text="Heater Config", padding=4)
        cfgf.pack(pady=(4, 8), fill="x")

        # same styling as before; compact but readable
        std_btn = dict(
            bg=WHITE_BG,
            fg=ACCENT_COLOR,
            font=BTN_FONT,                # uses the temp-panel’s scaled BTN_FONT
            bd=1,
            relief="solid",
            highlightthickness=1,
            highlightbackground="#000000"
        )

        tk.Button(cfgf, text="Connect", **std_btn, command=self.on_init_controller) \
            .grid(row=0, column=0, padx=4, pady=2, sticky="we")

        self.btn_start = tk.Button(cfgf, text="Start: OFF", **std_btn, command=self.on_toggle_start)
        self.btn_start.grid(row=0, column=2, padx=4, pady=2, sticky="we")

        # make buttons expand evenly
        for i in range(3):
            cfgf.columnconfigure(i, weight=1)


    def on_toggle_start(self):
        global client
        if not client:
            print("[Init] Not connected yet.")
            return
        try:
            st = int(self.cfg.get('General', 'station', fallback='10'))
        except Exception:
            st = 10
        desired = not getattr(self, 'start_on', False)
        ok = write_coil(client, START_SWITCH_COIL, desired, slave=st)
        if ok:
            self.start_on = desired
            self.btn_start.config(text=f"Start: {'ON' if self.start_on else 'OFF'}")
            print(f"[Coil] Start Heating -> {self.start_on}")
        else:
            print("[Coil] Failed to toggle Start Heating")

    def show_config_dialog(self):
        dlg = tk.Toplevel(self.master, bg="#FFFFFF")
        dlg.title("Configuration")
        dlg.transient(self.master); dlg.grab_set()
        if ICON_PATH and os.path.exists(ICON_PATH):
            try: dlg.iconbitmap(ICON_PATH)
            except Exception: pass

        # vars
        com_var  = tk.StringVar(value=self.com_var.get())
        port_var = tk.StringVar(value=self.port_var.get())
        api_var  = tk.BooleanVar(value=self.api_on_var.get())
        save_var = tk.BooleanVar(value=self.data_save_var.get())
        dir_var  = tk.StringVar(value=self.log_dir.get())

        row = 0
        def label(txt):
            tk.Label(dlg, text=txt, font=POP_FONT, bg="#FFFFFF", fg="#000000")\
            .grid(row=row, column=0, sticky="e", padx=6, pady=3)

        # COM port
        label("COM Port:")
        tk.Entry(dlg, textvariable=com_var, font=POP_FONT, width=12)\
            .grid(row=row, column=1, sticky="w", padx=6, pady=3)

        # API port
        row += 1; label("API Port:")
        tk.Entry(dlg, textvariable=port_var, font=POP_FONT, width=10)\
            .grid(row=row, column=1, sticky="w", padx=6, pady=3)

        # check-boxes
        row += 1
        tk.Checkbutton(dlg, text="Enable API", variable=api_var,
                    font=POP_FONT, bg="#FFFFFF", fg="#000000",
                    selectcolor="#FFFFFF", activebackground="#FFFFFF")\
            .grid(row=row, column=0, columnspan=2, sticky="w", padx=6, pady=3)
        row += 1
        tk.Checkbutton(dlg, text="Save Logs", variable=save_var,
                    font=POP_FONT, bg="#FFFFFF", fg="#000000",
                    selectcolor="#FFFFFF", activebackground="#FFFFFF")\
            .grid(row=row, column=0, columnspan=2, sticky="w", padx=6, pady=3)

        # log directory
        row += 1; label("Log Directory:")
        tk.Entry(dlg, textvariable=dir_var, font=POP_FONT, width=28)\
            .grid(row=row, column=1, sticky="w", padx=6, pady=3)
        tk.Button(dlg, text="Browse", font=POP_FONT,
                command=lambda: self._browse_dir(dir_var))\
            .grid(row=row, column=2, sticky="w", padx=6, pady=3)

        # buttons
        row += 1
        btn_frm = tk.Frame(dlg, bg="#FFFFFF")
        btn_frm.grid(row=row, column=0, columnspan=3, pady=8)
        tk.Button(btn_frm, text="Apply", font=POP_FONT, width=6,
                bg=ACCENT_COLOR, fg="#FFFFFF",
                command=lambda: self._apply_cfg_changes(
                    dlg, com_var, port_var, api_var, save_var, dir_var))\
            .pack(side=tk.LEFT, padx=8)
        tk.Button(btn_frm, text="Cancel", font=POP_FONT, width=6,
                command=dlg.destroy)\
            .pack(side=tk.LEFT, padx=8)
    def _browse_dir(self, var):
        folder = filedialog.askdirectory(initialdir=var.get() or os.getcwd())
        if folder: var.set(folder)

    def _apply_cfg_changes(self, dlg, com_v, port_v, api_v, save_v, dir_v):
        self.com_var.set(com_v.get().strip())
        self.port_var.set(port_v.get().strip() or "5000")
        self.api_on_var.set(api_v.get())
        self.data_save_var.set(save_v.get())
        self.log_dir.set(dir_v.get())

        self.cfg["Serial"]["port"]         = self.com_var.get()
        self.cfg["General"]["api_port"]    = self.port_var.get()
        self.cfg["General"]["api_enabled"] = str(self.api_on_var.get()).lower()
        self.cfg["General"]["save_logs"]   = str(self.data_save_var.get()).lower()
        self.cfg["Logging"]["directory"]   = self.log_dir.get()
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            self.cfg.write(f)
        dlg.destroy()

    def _build_chart_panel(self):
        # smaller figure just for temp panel
        self.fig, self.ax = plt.subplots(figsize=(4.2, 2.6))
        self.ax.get_xaxis().set_visible(False)  # hide x-axis
        self.ax.set_ylabel("Temperature (°C)")

        (self.line_actual,) = self.ax.plot([], [], marker="o", markersize=2, linestyle="-", label="Actual")
        (self.line_set,)    = self.ax.plot([], [], linestyle="--", label="Set")
        self.ax.legend(loc="upper left", frameon=False)
        self.ax.grid(True, linestyle=":", linewidth=0.8)

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.right_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        from matplotlib.backends.backend_tkagg import NavigationToolbar2Tk
        self.toolbar = NavigationToolbar2Tk(self.canvas, self.right_frame)
        self.toolbar.update()

        def _on_move(event):
            try:
                if event.inaxes == self.ax and event.ydata is not None:
                    self._root().title(f"T = {event.ydata:0.2f} °C")
                else:
                    self._root().title("ONWAY COMBINED CONTROL")
            except Exception:
                pass

        # store cid so we can disconnect on close
        self._motion_cid = self.fig.canvas.mpl_connect('motion_notify_event', _on_move)


    def _bind_keys(self):
        self.bind("<KeyPress-plus>",   self.on_key_up)
        self.bind("<KeyPress-minus>",  self.on_key_down)
        self.focus_set()

    def on_init_controller(self):
        port = self.com_var.get().strip() or "COM4"
        if not self._connect_modbus(port):
            print(f"[Init] ❌  Failed to connect on {port}")
            return

        initial_sp = read_register(client, 3000)
        if initial_sp is not None:
            data_store["SetTemperature"] = initial_sp
            self.set_point_var.set(f"{initial_sp:.1f}")

        self._ensure_log_dir(self.log_dir.get())
        self._start_ui_thread()
        self._start_modbus_thread()

        if self.api_on_var.get():
            try:
                port_num = int(self.port_var.get() or 5000)
            except ValueError:
                port_num = 5000
                self.port_var.set(str(port_num))
            self._start_api_thread(port_num)

        self._init_mqtt()

    def _connect_modbus(self, port_name):
        global CURRENT_COM_PORT
        CURRENT_COM_PORT = port_name  # track latest port for auto-reopen
        new_client = _new_modbus_client(port_name)
        if new_client.connect():
            global client, stop_threads
            client = new_client
            stop_threads = False
            return True
        return False

    def _ensure_log_dir(self, path):
        if not os.path.isdir(path):
            os.makedirs(path)

    def _start_ui_thread(self):
        self.ui_thread = threading.Thread(target=self.ui_update_loop, daemon=True)
        self.ui_thread.start()

    def _start_modbus_thread(self):
        self.modbus_thread = threading.Thread(target=update_modbus_values_loop, args=(self,), daemon=True)
        self.modbus_thread.start()

    def _start_api_thread(self, port_num):
        self.api_thread = threading.Thread(target=run_flask_app, args=(port_num,), daemon=True)
        self.api_thread.start()

    def on_enter_set_point(self, event):
        self.entry_typing = False
        try:
            val = float(self.set_point_var.get().strip())
        except ValueError:
            return
        data_store["SetTemperature"] = val
        threading.Thread(target=write_register, args=(client, 3000, val), daemon=True).start()

    def _bump_set_point(self, delta):
        cur = data_store.get("SetTemperature")
        if cur is None:
            return
        new_val = round(cur + delta, 1)
        data_store["SetTemperature"] = new_val
        threading.Thread(target=write_register, args=(client, 3000, new_val), daemon=True).start()
        self.set_point_var.set(f"{new_val:.1f}")

    def on_key_up(self, event):   self._bump_set_point(+1)
    def on_key_down(self, event): self._bump_set_point(-1)

    def ui_update_loop(self):
        try:
            while not stop_threads and self.running:
                try:
                    self._refresh_readouts()
                except Exception:
                    pass
                if getattr(self, "mqtt_mgr", None):
                    self.mqtt_mgr.publish({
                        "Temperature":  data_store["Temperature"],
                        "Power":        data_store["Power"],
                        "Setpoint":     data_store["SetTemperature"]
                    })
                time.sleep(0.1)
        except Exception as e:
            print("[UI] loop stopped:", e)

    def _refresh_readouts(self):
        temp  = data_store.get("Temperature")
        power = data_store.get("Power")
        spt   = data_store.get("SetTemperature")
        self._update_temp_label(temp)
        self._update_power_display(power)
        self._update_setpoint_entry(spt)
        if temp is not None:
            self.add_temps_to_plot(temp, data_store.get('SetTemperature'))

    def _update_temp_label(self, temp):
        text = f"{temp:.1f}" if temp is not None else "--"
        self.temp_label_number.config(text=text)

    def _update_power_display(self, power):
        if power is not None:
            p_val = max(0, min(100, power))
            self.power_bar["value"] = p_val
            self.power_percent_label.config(text=f"{power:.1f}%")
        else:
            self.power_bar["value"] = 0
            self.power_percent_label.config(text="--%")

    def _update_setpoint_entry(self, spt):
        if not self.entry_typing and spt is not None:
            self.set_point_var.set(f"{spt:.1f}")

    def add_temps_to_plot(self, actual_c, set_c):
        now = datetime.now()
        if (now - self.last_plot_ts).total_seconds() < 1.0:
            return
        self.last_plot_ts = now

        self.times.append(now)
        self.temps.append(actual_c)
        self.set_vals.append(self.set_vals[-1] if (set_c is None and self.set_vals) else set_c)

        # 2-minute window
        cutoff = now - timedelta(minutes=2)
        while self.times and self.times[0] < cutoff:
            self.times.pop(0); self.temps.pop(0); self.set_vals.pop(0)

        ts_for_set, set_series = [], []
        for t, s in zip(self.times, self.set_vals):
            if s is not None:
                ts_for_set.append(t); set_series.append(s)

        self.line_actual.set_data(self.times, self.temps)
        self.line_set.set_data(ts_for_set, set_series)

        self.ax.relim()
        if getattr(self, "toolbar", None) and getattr(self.toolbar, "mode", "") == "":
            self.ax.autoscale_view()

        # x-axis is hidden; no formatter/ticks needed
        self.fig.tight_layout()
        self.canvas.draw_idle()


    def _init_mqtt(self):
        if not self.cfg.getboolean("MQTT", "enabled", fallback=False):
            return
        topic_pub = self.cfg["MQTT"]["topic"]
        self.mqtt_mgr = MQTTManager(
            host      = self.cfg["MQTT"]["host"],
            port      = self.cfg.getint("MQTT", "port"),
            topic_pub = topic_pub,
            client_id = self.cfg["MQTT"].get("client_id", ""),
            username  = self.cfg["MQTT"].get("username", ""),
            password  = self.cfg["MQTT"].get("password", ""),
            setpoint_cmd_topic = f"{topic_pub}/set",
            discovery_prefix   = "homeassistant",
            qos     = self.cfg.getint("MQTT", "qos",    fallback=0),
            retain  = self.cfg.getboolean("MQTT", "retain", fallback=False),
            gui_ref = self
        )

    def apply_remote_setpoint(self, new_sv):
        data_store["SetTemperature"] = new_sv
        self.set_point_var.set(f"{new_sv:.1f}")
        threading.Thread(target=write_register, args=(client, 3000, new_sv), daemon=True).start()

    def close(self):
        global stop_threads
        stop_threads = True
        self.running = False

        # cancel scheduled refresh
        try:
            if getattr(self, "_after_id", None):
                self.after_cancel(self._after_id)
        except Exception:
            pass
        self._after_id = None

        # disconnect mpl events, destroy canvas, close figure
        try:
            if hasattr(self, "fig") and hasattr(self, "_motion_cid") and self._motion_cid:
                try: self.fig.canvas.mpl_disconnect(self._motion_cid)
                except Exception: pass
        except Exception:
            pass
        try:
            if hasattr(self, "canvas"):
                self.canvas.get_tk_widget().destroy()
        except Exception:
            pass
        try:
            if hasattr(self, "fig"):
                import matplotlib.pyplot as _plt
                _plt.close(self.fig)
        except Exception:
            pass

        # close Modbus and MQTT safely
        try:
            if client:
                try: client.close()
                except Exception: pass
        except Exception:
            pass
        try:
            if self.mqtt_mgr and self.mqtt_mgr.client:
                self.mqtt_mgr.client.loop_stop()
                self.mqtt_mgr.client.disconnect()
        except Exception:
            pass

        # destroy frame
        try:
            self.destroy()
        except Exception:
            pass


class CompactTemperaturePanel(tk.Frame):
    """Minimal Temperature / Setpoint / Power readout, sized for a narrow sidebar.

    Connects to the heater over Modbus using the default port from
    TempConfig.ini (auto-connect, same pattern as the axis controller), then
    uses TemperatureService for serialized fast telemetry and slow status polls.
    Readouts plus an editable setpoint, and a small rolling temperature chart
    — no config dialog, no Flask/MQTT. Set `external_log` (a callable) after
    construction to route status messages into a host window's log box
    instead of stdout.
    """

    def __init__(self, master):
        super().__init__(master, bg=APP_BG)
        self.pack(fill="x", expand=False)

        self.cfg = cfg

        self.running = True
        self.data_save_var = tk.BooleanVar(value=cfg.getboolean("General", "save_logs", fallback=True))
        self.log_dir = tk.StringVar(value=cfg.get("Logging", "directory", fallback=os.getcwd()))

        self.set_point_var = tk.StringVar(value="--")
        self.entry_typing = False
        self.start_on = False
        self.external_log = None

        try:
            station = int(cfg.get("General", "station", fallback="10"))
        except Exception:
            station = 10
        self.temperature_service = TemperatureService(
            PymodbusTemperatureTransport(_new_modbus_client),
            fast_interval_s=0.25,
            slow_interval_s=5.0,
            slave=station,
            on_update=_publish_temperature_update,
        )
        self._service_ui_queue = queue.Queue()
        self._after_id: Optional[str] = None

        # rolling-window plot buffers
        self.times = []
        self.temps = []
        self.set_vals = []
        self.last_plot_ts = datetime.min

        self._build_ui()
        self._after_id = self.after(100, self._schedule_ui_refresh)
        self.after(400, self._auto_connect)

    def _log(self, msg):
        cb = getattr(self, "external_log", None)
        if cb:
            try:
                cb(msg)
                return
            except Exception:
                pass
        print(msg)

    def _build_ui(self):
        f = ttk.LabelFrame(self, text="Temperature", padding=8)
        f.pack(fill=tk.X, padx=0, pady=4)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Temp:").grid(row=0, column=0, sticky="e", padx=(0, 6), pady=2)
        temp_row = ttk.Frame(f)
        temp_row.grid(row=0, column=1, sticky="w", pady=2)
        self.temp_val_lbl = ttk.Label(temp_row, text="--", font=("Segoe UI", 18, "bold"))
        self.temp_val_lbl.pack(side=tk.LEFT)
        ttk.Label(temp_row, text=" °C").pack(side=tk.LEFT)

        ttk.Label(f, text="Setpoint:").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=2)
        sp_row = ttk.Frame(f)
        sp_row.grid(row=1, column=1, sticky="w", pady=2)
        self.sp_entry = ttk.Entry(sp_row, textvariable=self.set_point_var, width=8)
        self.sp_entry.pack(side=tk.LEFT)
        self.sp_entry.bind("<Return>", self._apply_setpoint)
        self.sp_entry.bind("<Key>", self._on_entry_keypress)
        self.sp_entry.bind("<FocusOut>", self._on_entry_focus_out)
        ttk.Label(sp_row, text=" °C").pack(side=tk.LEFT)
        step_col = ttk.Frame(sp_row)
        step_col.pack(side=tk.LEFT, padx=(6, 0))
        up_btn = ttk.Button(step_col, text="▲", width=2, command=lambda: self._bump_setpoint(+1))
        up_btn.pack(side=tk.TOP)
        down_btn = ttk.Button(step_col, text="▼", width=2, command=lambda: self._bump_setpoint(-1))
        down_btn.pack(side=tk.TOP)

        ttk.Label(f, text="Power:").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=2)
        pow_row = ttk.Frame(f)
        pow_row.grid(row=2, column=1, sticky="we", pady=2)
        pow_row.columnconfigure(0, weight=1)
        self.power_bar = ttk.Progressbar(pow_row, orient="horizontal", mode="determinate", maximum=100)
        self.power_bar.grid(row=0, column=0, sticky="we")
        self.power_pct_lbl = ttk.Label(pow_row, text="--%", width=6, anchor="e")
        self.power_pct_lbl.grid(row=0, column=1, padx=(6, 0))

        self.heating_btn = ttk.Button(
            f,
            text="Start: OFF",
            command=self.on_toggle_start,
            state=tk.DISABLED,
        )
        self.heating_btn.grid(row=3, column=0, columnspan=2, sticky="we", pady=(6, 2))

        chart_row = ttk.Frame(f)
        chart_row.grid(row=4, column=0, columnspan=2, sticky="we", pady=(6, 0))
        self._build_chart(chart_row)

    def _build_chart(self, parent):
        # small, no toolbar — sized to sit in a narrow sidebar column
        self.fig, self.ax = plt.subplots(figsize=(3.2, 1.8), dpi=96)
        self.fig.subplots_adjust(left=0.18, right=0.97, top=0.95, bottom=0.08)
        self.ax.get_xaxis().set_visible(False)
        self.ax.set_ylabel("°C", fontsize=8)
        self.ax.tick_params(labelsize=7)

        (self.line_actual,) = self.ax.plot([], [], linewidth=1.2, label="Actual")
        (self.line_set,) = self.ax.plot([], [], linewidth=1.0, linestyle="--", label="Set")
        self.ax.legend(loc="upper left", frameon=False, fontsize=7)
        self.ax.grid(True, linestyle=":", linewidth=0.6)

        self.canvas = FigureCanvasTkAgg(self.fig, master=parent)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

    def add_temps_to_plot(self, actual_c, set_c):
        now = datetime.now()
        if (now - self.last_plot_ts).total_seconds() < 1.0:
            return
        self.last_plot_ts = now

        self.times.append(now)
        self.temps.append(actual_c)
        self.set_vals.append(self.set_vals[-1] if (set_c is None and self.set_vals) else set_c)

        # 2-minute rolling window
        cutoff = now - timedelta(minutes=2)
        while self.times and self.times[0] < cutoff:
            self.times.pop(0)
            self.temps.pop(0)
            self.set_vals.pop(0)

        ts_for_set, set_series = [], []
        for t, s in zip(self.times, self.set_vals):
            if s is not None:
                ts_for_set.append(t)
                set_series.append(s)

        self.line_actual.set_data(self.times, self.temps)
        self.line_set.set_data(ts_for_set, set_series)

        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw_idle()

    # ── Connection ───────────────────────────────────────────────────────────

    def _auto_connect(self):
        port = SERIAL_OPTS.get("port", "COM4")
        self._connect_worker(port)

    def _post_service_ui(self, callback):
        try:
            self._service_ui_queue.put_nowait(callback)
        except Exception:
            pass

    def _watch_temperature_future(self, future, on_success=None, error_prefix="[Temp]"):
        def _done(completed):
            def _finish():
                try:
                    result = completed.result()
                except Exception as exc:
                    self._log(f"{error_prefix} {exc}")
                    return
                if on_success is not None:
                    on_success(result)

            self._post_service_ui(_finish)

        future.add_done_callback(_done)
        return future

    def _connect_worker(self, port):
        global CURRENT_COM_PORT
        CURRENT_COM_PORT = port
        future = self.temperature_service.connect(port)

        def _connected(_result):
            self._log(f"[Temp] Connected on {port}")
            self.heating_btn.config(state=tk.NORMAL)

        self._watch_temperature_future(
            future,
            on_success=_connected,
            error_prefix=(
                f"[Temp] Could not connect on {port}; temperature control offline:"
            ),
        )

    # ── Readout / setpoint ──────────────────────────────────────────────────

    def _schedule_ui_refresh(self):
        if not self.running or not self.winfo_exists():
            return
        try:
            while True:
                self._service_ui_queue.get_nowait()()
        except queue.Empty:
            pass
        except Exception as exc:
            self._log(f"[Temp UI] {exc}")
        try:
            self._refresh_readouts()
        except Exception:
            pass
        if self.running and self.winfo_exists():
            self._after_id = self.after(250, self._schedule_ui_refresh)

    def _refresh_readouts(self):
        temp = data_store.get("Temperature")
        power = data_store.get("Power")
        spt = data_store.get("SetTemperature")

        self.temp_val_lbl.config(text=f"{temp:.1f}" if temp is not None else "--")

        if power is not None:
            self.power_bar["value"] = max(0, min(100, power))
            self.power_pct_lbl.config(text=f"{power:.1f}%")
        else:
            self.power_bar["value"] = 0
            self.power_pct_lbl.config(text="--%")

        if not self.entry_typing and spt is not None:
            self.set_point_var.set(f"{spt:.1f}")

        if temp is not None:
            self.add_temps_to_plot(temp, spt)

    def _on_entry_keypress(self, _event):
        self.entry_typing = True

    def _on_entry_focus_out(self, _event):
        self.entry_typing = False
        self._apply_setpoint()

    def _apply_setpoint(self, _event=None):
        self.entry_typing = False
        try:
            val = float(self.set_point_var.get().strip())
        except ValueError:
            return
        _publish_temperature_update({"SetTemperature": val})
        self._watch_temperature_future(
            self.temperature_service.set_setpoint(val),
            error_prefix="[Temp] Setpoint write failed:",
        )
        self._log(f"[Temp] Setpoint -> {val:.1f} degC")

    def _bump_setpoint(self, delta):
        cur = data_store.get("SetTemperature")
        if cur is None:
            return
        new_val = round(cur + delta, 1)
        _publish_temperature_update({"SetTemperature": new_val})
        self.set_point_var.set(f"{new_val:.1f}")
        self._watch_temperature_future(
            self.temperature_service.set_setpoint(new_val),
            error_prefix="[Temp] Setpoint write failed:",
        )
        self._log(f"[Temp] Setpoint -> {new_val:.1f} degC")

    def on_toggle_start(self):
        """Toggle the controller's Start Switch without blocking the Tk event loop."""
        if not self.temperature_service.connected:
            self._log("[Init] Not connected yet.")
            return

        desired = not self.start_on
        self.heating_btn.config(state=tk.DISABLED)
        future = self.temperature_service.set_heating(desired)

        def _done(completed):
            def _finish():
                try:
                    ok = bool(completed.result())
                except Exception as exc:
                    self._log(f"[Coil] Failed to toggle Start Heating: {exc}")
                    ok = False
                self._finish_heating_toggle(desired, ok)

            self._post_service_ui(_finish)

        future.add_done_callback(_done)

    def _finish_heating_toggle(self, desired, ok):
        if not self.running or not self.winfo_exists():
            return
        if ok:
            self.start_on = desired
            self.heating_btn.config(
                text=f"Start: {'ON' if self.start_on else 'OFF'}",
                state=tk.NORMAL,
            )
            self._log(f"[Coil] Start Heating -> {self.start_on}")
        else:
            self.heating_btn.config(state=tk.NORMAL)
            self._log("[Coil] Failed to toggle Start Heating")

    # ── Shutdown ─────────────────────────────────────────────────────────────

    def close(self):
        self.running = False
        try:
            if self._after_id:
                self.after_cancel(self._after_id)
        except Exception:
            pass
        self._after_id = None
        try:
            self.temperature_service.shutdown(wait=True, timeout=2.0)
        except Exception:
            pass
        try:
            if hasattr(self, "canvas"):
                self.canvas.get_tk_widget().destroy()
        except Exception:
            pass
        try:
            if hasattr(self, "fig"):
                plt.close(self.fig)
        except Exception:
            pass
