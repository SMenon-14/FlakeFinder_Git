import os
import sys
import time
import socket
import subprocess
import atexit
import psutil
from datetime import date
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from src.autoscan.watcher_client import start_scan, stop_scan
from src.constants.configs.materials import MATERIALS, SUBSTRATES

script_dir  = os.path.dirname(os.path.abspath(__file__))

def _start_server():
    server_path = 'src.autoscan.watcher_server'
    
    proc = subprocess.Popen([sys.executable, '-m', server_path])

    for attempt in range(20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(("127.0.0.1", 65432))
                s.shutdown(socket.SHUT_RDWR) 
            return proc
        except ConnectionRefusedError:
            time.sleep(0.5)

    return proc


class ScannerUI(tk.Toplevel):
    # Accepts an optional default folder string upon creation
    def __init__(self, parent=None, default_folder=None):
        super().__init__(parent)
        
        # 1. Initialize variables and window layout settings first
        self.title("Scan Parameter Entry")
        self.resizable(False, False)
        self._scanning = False
        self._server_proc = None  # Will be populated by the thread
        
        initial_folder = default_folder if default_folder else "No folder selected"
        self.folder_var = tk.StringVar(value=initial_folder)
        self.dropdown_data = {"materials" : list(MATERIALS), "substrates" : list(SUBSTRATES)}
        
        # 2. Render the layout frames immediately so they can accept mouse clicks
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        
        # 3. FIX: Start the server inside a background thread so it doesn't freeze your clicks
        import threading
        threading.Thread(target=self._async_start_server, daemon=True).start()

    def _async_start_server(self):
        """Runs the blocking socket connection loops safely away from the UI thread."""
        print("Spinning up background server process thread...")
        proc = _start_server()
        self._server_proc = proc
        print("Background server process successfully linked.")


    def _on_close(self):
        self.shutdown()

    def __get_pid_by_port(self, port):
        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr and conn.laddr.port == port:
                    # SKIP dead connections in TIME_WAIT or assigned to system idle
                    if conn.status == 'TIME_WAIT' or conn.pid == 0:
                        continue
                        
                    if conn.pid is not None:
                        try:
                            proc = psutil.Process(conn.pid)
                            return conn.pid, proc.name()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            return conn.pid, "Unknown"
        except psutil.AccessDenied:
            pass
            
        return None, None
    
    def __kill_pid(self, pid):
        """Gracefully terminates a process by PID, falling back to force-kill if needed."""
        try:
            process = psutil.Process(pid)
            name = process.name()
            
            print(f"Sending termination signal to '{name}' (PID: {pid})...")
            process.terminate()
            
            try:
                process.wait(timeout=3)
                print(f"Process {pid} successfully stopped.")
                return True
            except psutil.TimeoutExpired:
                print(f"Process {pid} timed out. Force-killing...")
                process.kill()
                process.wait()
                print(f"Process {pid} was force-killed.")
                return True
                
        except psutil.NoSuchProcess:
            print(f"Error: Process with PID {pid} does not exist.")
            return False
        except psutil.AccessDenied:
            print(f"Permission Error: Cannot kill PID {pid}. Run script with 'sudo'.")
            return False

    def shutdown(self):
        """Public method to safely kill the background server and fully destroy the UI."""
        print("Initiating shutdown sequence for ScannerUI...")
        
        # 1. Update the UI state to let the user know it is waiting for the queue to clear
        self.title("Closing... waiting for processing queue to finish")
        self.update_idletasks()

        # 2. Tell the client to send a network "stop" request over the port.
        # This triggers our previously built server queue-drain block.
        try:
            print("Sending network stop command to drain queue...")
            stop_scan()  # This hits the server socket endpoint
        except Exception as e:
            print(f"Failed to send network stop command: {e}")

        # 3. Give the server loop a moment to exit safely on its own
        if hasattr(self, '_server_proc') and self._server_proc:
            try:
                # Wait for the process to terminate cleanly via server side flag exit
                print("Waiting for server process to exit natively...")
                self._server_proc.wait(timeout=10) 
            except subprocess.TimeoutExpired:
                # Fallback: if it takes too long, fall back to killing the process ID
                print("Server did not exit cleanly within timeout. Forcing shutdown...")
                pid, name = self.__get_pid_by_port(65432)
                if pid:
                    self.__kill_pid(pid)
        
        # 4. Fully destroy the Tkinter UI components
        print("UI successfully destroyed.")
        self.destroy()

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        # ── Folder selection ──────────────────────────────────────────
        folder_frame = ttk.LabelFrame(self, text="Watch folder")
        folder_frame.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 4))

        ttk.Label(folder_frame, textvariable=self.folder_var, width=48,
                  anchor="w").grid(row=0, column=0, padx=8, pady=6)
        ttk.Button(folder_frame, text="Browse…",
                   command=self._browse).grid(row=0, column=1, padx=(0, 8), pady=6)

        # ── Scan parameters ───────────────────────────────────────────
        param_frame = ttk.LabelFrame(self, text="Scan parameters")
        param_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=4)
        param_frame.columnconfigure(1, weight=1)

        # Material (Dropdown)
        ttk.Label(param_frame, text="Material").grid(row=0, column=0, sticky="w", **pad)
        self.material_var = tk.StringVar()
        self.material_cb = ttk.Combobox(param_frame, textvariable=self.material_var, width=28)
        self.material_cb['values'] = self.dropdown_data.get("materials", [])
        self.material_cb.grid(row=0, column=1, sticky="ew", **pad)

        # Substrate (Dropdown)
        ttk.Label(param_frame, text="Substrate").grid(row=1, column=0, sticky="w", **pad)
        self.substrate_var = tk.StringVar()
        self.substrate_cb = ttk.Combobox(param_frame, textvariable=self.substrate_var, width=28)
        self.substrate_cb['values'] = self.dropdown_data.get("substrates", [])
        self.substrate_cb.grid(row=1, column=1, sticky="ew", **pad)

        # Confidence threshold (Slider)
        ttk.Label(param_frame, text="Confidence threshold").grid(row=2, column=0, sticky="w", **pad)
        
        slider_frame = ttk.Frame(param_frame)
        slider_frame.grid(row=2, column=1, sticky="ew", **pad)
        slider_frame.columnconfigure(0, weight=1)
        
        self.confidence_var = tk.DoubleVar(value=0.30)
        self.confidence_scale = ttk.Scale(
            slider_frame, from_=0.0, to=1.0, 
            variable=self.confidence_var, command=self._update_slider_label
        )
        self.confidence_scale.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        
        self.slider_lbl = ttk.Label(slider_frame, text="0.30", width=4)
        self.slider_lbl.grid(row=0, column=1, sticky="e")

        # Minimum size threshold
        ttk.Label(param_frame, text="Min area threshold (μm x μm)").grid(
            row=3, column=0, sticky="w", **pad)
        self.size_threshold_var = tk.StringVar(value=str(10))
        ttk.Entry(param_frame, textvariable=self.size_threshold_var, width=30).grid(
            row=3, column=1, sticky="ew", **pad)

        # Scanned date
        ttk.Label(param_frame, text="Exfoliated date").grid(
            row=4, column=0, sticky="w", **pad)
        self.date_var = tk.StringVar(value=str(date.today()))
        ttk.Entry(param_frame, textvariable=self.date_var, width=30).grid(
            row=4, column=1, sticky="ew", **pad)
        
        # User Name
        ttk.Label(param_frame, text="User Name").grid(
            row=5, column=0, sticky="w", **pad)
        self.user_name = tk.StringVar(value="no user")
        ttk.Entry(param_frame, textvariable=self.user_name, width=30).grid(
            row=5, column=1, sticky="ew", **pad)

        # Workers
        ttk.Label(param_frame, text="Worker threads").grid(row=6, column=0, sticky="w", **pad)
        self.workers_var = tk.IntVar(value=4)
        ttk.Spinbox(param_frame, from_=1, to=16, textvariable=self.workers_var, width=6).grid(row=6, column=1, sticky="w", **pad)

        # Zoom level (Dropdown) ── NEW ─────────────────────────────────
        ttk.Label(param_frame, text="Zoom level").grid(row=7, column=0, sticky="w", **pad)
        self.zoom_var = tk.IntVar(value=20)
        self.zoom_cb = ttk.Combobox(
            param_frame,
            textvariable=self.zoom_var,
            values=[10, 20, 50, 100],
            state="readonly",
            width=28,
        )
        self.zoom_cb.set(20)
        self.zoom_cb.grid(row=7, column=1, sticky="ew", **pad)

         # Sample number
        ttk.Label(param_frame, text="Sample number").grid(row=8, column=0, sticky="w", **pad)
        self.sample_number_var = tk.StringVar(value="")
        ttk.Entry(param_frame, textvariable=self.sample_number_var, width=30).grid(
            row=8, column=1, sticky="ew", **pad)

        # Keep Flakeless Images Checkbox
        self.keep_flakeless_var = tk.BooleanVar(value=True)
        self.keep_flakeless_chk = ttk.Checkbutton(param_frame, text="Keep flakeless images", variable=self.keep_flakeless_var)
        self.keep_flakeless_chk.grid(row=9, column=0, columnspan=2, sticky="w", **pad)

        # Keep Bulk Flakes Checkbox
        #self.keep_bulk_var = tk.BooleanVar(value=True)
        #self.keep_bulk_chk = ttk.Checkbutton(param_frame, text="Keep bulk flakes", variable=self.keep_bulk_var)
        #self.keep_bulk_chk.grid(row=8, column=1, columnspan=2, sticky="w", **pad)

        # ── Buttons ───────────────────────────────────────────────────
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=(4, 12))

        self.start_btn = ttk.Button(btn_frame, text="Submit", command=self._start_scan)
        self.start_btn.grid(row=0, column=0, padx=8, pady=4)

    # ── Helpers ───────────────────────────────────────────────────────

    def _browse(self):
        folder = filedialog.askdirectory(title="Select watch folder")
        if folder:
            self.folder_var.set(folder)

    def _update_slider_label(self, val):
        self.slider_lbl.configure(text=f"{float(val):.2f}")

    def _validate_inputs(self) -> bool:
        if self.folder_var.get() == "No folder selected" or not self.folder_var.get().strip():
            messagebox.showwarning("Missing folder", "Please verify that a valid watch folder is set.")
            return False
        if not self.material_var.get().strip():
            messagebox.showwarning("Missing field", "Please select or enter a material.")
            return False
        if not self.substrate_var.get().strip():
            messagebox.showwarning("Missing field", "Please select or enter a substrate.")
            return False
        return True

    # ── Button handlers ───────────────────────────────────────────────

    def _start_scan(self):
        if not self._validate_inputs():
            return
        print(self.get_exfoliated_date())
        
        self.submitted = True
        
        params = {
            "material":              self.material_var.get().strip(),
            "substrate":             self.substrate_var.get().strip(),
            "confidence_threshold":  round(self.confidence_var.get(), 2),
            "size_threshold": round(float(self.size_threshold_var.get()), 2),
            "scanned_date":          (str(date.today())).strip(),
            "keep_flakeless_images": str(self.keep_flakeless_var.get()),
            "user":                  str(self.user_name.get()),
        }

        try:
            self.grab_release()
        except Exception:
            pass

        # Hide the window immediately
        self.withdraw()
        self.update_idletasks()

        # 1. FIX: Notify launch_and_wait_for_scanner to resume right now
        if hasattr(self, "_wait_gate") and self._wait_gate:
            self._wait_gate.set(True)

        print("Popup hidden. Sending scan payload parameters to server...")
        result = start_scan(
            folder=self.folder_var.get(),
            params=params,
            num_workers=self.workers_var.get(),
        )

        if result.get("status") == "ok":
            self._scanning = True
        else:
            # Re-display the window if the pipeline server breaks or rejects
            self.deiconify() 
            self.grab_set()
            self.submitted = False
            messagebox.showerror("Error", "Failed to start the scan pipeline.")



    def _scan_complete(self):
        """Preserved function logic for future repurposing"""
        result = stop_scan()
        self._scanning = False

    def get_exfoliated_date(self):
        date_str = self.date_var.get().strip()
        mmddyy_str = date_str[5:7] + date_str[8:10] + date_str[2:4]
        return mmddyy_str

    def get_zoom_level(self):
        """Returns the currently selected zoom level as an integer."""
        return int(self.zoom_cb.get())
    
    def get_sample_number(self):
        """Returns the currently entered sample number as a string."""
        return self.sample_number_var.get().strip()


if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw() 
    script_dir  = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    image_default_path = os.path.normpath(os.path.join(script_dir, 'images'))
    popup = ScannerUI(parent=root, default_folder=image_default_path)
    root.protocol("WM_DELETE_WINDOW", popup.shutdown)
    root.createcommand("::tk::mac::Quit", popup.shutdown)
    
    def global_final_cleanup():
        try:
            pid, _ = popup._ScannerUI__get_pid_by_port(65432)
            if pid:
                popup._ScannerUI__kill_pid(pid)
        except Exception:
            pass
            
    atexit.register(global_final_cleanup)
    popup.mainloop()