#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
bambu3mf_gui.py - Drag-and-drop desktop GUI for bambu3mf_to_glb.

Drop a .3mf project onto the window (or pick it with "Browse..."), choose where the
.glb should go, press Convert. Drop several .3mf files at once to convert them all
into the output folder.

Run with:  python bambu3mf_gui.py      (or pythonw bambu3mf_gui.py to hide the console)
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, ttk

# Make sure the converter next to this file is importable even when launched elsewhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bambu3mf_to_glb import convert  # noqa: E402

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_AVAILABLE = True
except ImportError:  # drag and drop is optional; browsing still works
    DND_FILES = None
    TkinterDnD = None
    _DND_AVAILABLE = False


def _split_dropped_paths(data: str) -> list[str]:
    """tkdnd hands over a Tcl list: paths with spaces are wrapped in {braces}."""
    root = tk.Tk() if tk._default_root is None else tk._default_root  # type: ignore[attr-defined]
    return list(root.tk.splitlist(data))


class ConverterApp:
    def __init__(self) -> None:
        self.root = TkinterDnD.Tk() if _DND_AVAILABLE else tk.Tk()
        self.root.title("Bambu 3MF → GLB")
        self.root.minsize(560, 520)
        self.root.geometry("640x560")

        self.inputs: list[str] = []
        self.output_var = tk.StringVar()
        self.units_var = tk.StringVar(value="m")
        self.keep_pos_var = tk.BooleanVar(value=False)
        self.output_edited = False
        self.busy = False
        self.last_outputs: list[str] = []
        self._events: queue.Queue = queue.Queue()  # worker thread -> GUI thread

        self._build_ui()
        self.root.after(100, self._poll_events)
        if _DND_AVAILABLE:
            for widget in (self.root, self.drop_zone):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
                widget.dnd_bind("<<DragEnter>>", lambda e: self._set_drop_highlight(True))
                widget.dnd_bind("<<DragLeave>>", lambda e: self._set_drop_highlight(False))

    # ------------------------------------------------------------------ UI --
    def _build_ui(self) -> None:
        pad = {"padx": 12, "pady": 6}
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        # Drop zone
        self.drop_zone = tk.Label(
            self.root,
            text=("Drop a .3mf project here" if _DND_AVAILABLE else
                  "Drag & drop unavailable (pip install tkinterdnd2)\nClick to browse for a .3mf project"),
            relief="groove", bd=2, height=6, cursor="hand2",
            bg="#f3f6fa", fg="#4a5568", font=("Segoe UI", 12),
        )
        self.drop_zone.pack(fill="x", padx=12, pady=(12, 6))
        self.drop_zone.bind("<Button-1>", lambda e: self.browse_input())

        # Input row
        frm_in = ttk.Frame(self.root)
        frm_in.pack(fill="x", **pad)
        ttk.Label(frm_in, text="Input .3mf:", width=12).pack(side="left")
        self.input_entry = ttk.Entry(frm_in, state="readonly")
        self.input_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(frm_in, text="Browse...", command=self.browse_input).pack(side="left")

        # Output row
        frm_out = ttk.Frame(self.root)
        frm_out.pack(fill="x", **pad)
        ttk.Label(frm_out, text="Output .glb:", width=12).pack(side="left")
        self.output_entry = ttk.Entry(frm_out, textvariable=self.output_var)
        self.output_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.output_entry.bind("<KeyRelease>", lambda e: setattr(self, "output_edited", True))
        ttk.Button(frm_out, text="Save as...", command=self.browse_output).pack(side="left")

        # Options row
        frm_opt = ttk.Frame(self.root)
        frm_opt.pack(fill="x", **pad)
        ttk.Label(frm_opt, text="Units:", width=12).pack(side="left")
        ttk.Radiobutton(frm_opt, text="metres (glTF standard)", value="m", variable=self.units_var).pack(side="left")
        ttk.Radiobutton(frm_opt, text="millimetres", value="mm", variable=self.units_var).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(frm_opt, text="Keep build-plate position", variable=self.keep_pos_var).pack(side="left", padx=(20, 0))

        # Buttons
        frm_btn = ttk.Frame(self.root)
        frm_btn.pack(fill="x", **pad)
        self.convert_btn = ttk.Button(frm_btn, text="Convert", command=self.start_convert, state="disabled")
        self.convert_btn.pack(side="left")
        self.open_btn = ttk.Button(frm_btn, text="Open output folder", command=self.open_output_folder, state="disabled")
        self.open_btn.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(frm_btn, mode="indeterminate", length=160)
        self.progress.pack(side="right")

        # Log
        ttk.Label(self.root, text="Log:").pack(anchor="w", padx=12)
        frm_log = ttk.Frame(self.root)
        frm_log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log_text = tk.Text(frm_log, height=10, wrap="word", state="disabled", font=("Consolas", 9))
        scroll = ttk.Scrollbar(frm_log, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _set_drop_highlight(self, on: bool) -> None:
        self.drop_zone.configure(bg="#dbeafe" if on else "#f3f6fa")

    # -------------------------------------------------------------- events --
    def _on_drop(self, event) -> None:
        self._set_drop_highlight(False)
        paths = [p for p in _split_dropped_paths(event.data) if p.lower().endswith(".3mf")]
        if not paths:
            messagebox.showwarning("Not a 3MF", "Please drop one or more .3mf files.")
            return
        self.set_inputs(paths)

    def browse_input(self) -> None:
        if self.busy:
            return
        paths = filedialog.askopenfilenames(
            title="Select Bambu Studio 3MF project(s)",
            filetypes=[("3MF project", "*.3mf"), ("All files", "*.*")],
        )
        if paths:
            self.set_inputs(list(paths))

    def browse_output(self) -> None:
        if self.busy:
            return
        if len(self.inputs) > 1:
            folder = filedialog.askdirectory(title="Select output folder for the .glb files",
                                             initialdir=os.path.dirname(self.output_var.get()) or None)
            if folder:
                self.output_var.set(folder)
                self.output_edited = True
            return
        initial = self.output_var.get()
        path = filedialog.asksaveasfilename(
            title="Save GLB as",
            defaultextension=".glb",
            filetypes=[("Binary glTF", "*.glb")],
            initialdir=os.path.dirname(initial) or None,
            initialfile=os.path.basename(initial) or None,
        )
        if path:
            self.output_var.set(path)
            self.output_edited = True

    def set_inputs(self, paths: list[str]) -> None:
        self.inputs = paths
        self.input_entry.configure(state="normal")
        self.input_entry.delete(0, "end")
        self.input_entry.insert(0, paths[0] if len(paths) == 1 else f"{len(paths)} files: " + "; ".join(os.path.basename(p) for p in paths))
        self.input_entry.configure(state="readonly")
        if not self.output_edited or not self.output_var.get():
            if len(paths) == 1:
                self.output_var.set(os.path.splitext(paths[0])[0] + ".glb")
            else:
                self.output_var.set(os.path.dirname(paths[0]))
        self.drop_zone.configure(text=(f"✓ {os.path.basename(paths[0])}" if len(paths) == 1 else f"✓ {len(paths)} files selected")
                                 + "\nDrop another file to replace")
        self.convert_btn.configure(state="normal")
        self.log(f"Selected: {', '.join(paths)}")

    # ---------------------------------------------------------- conversion --
    def start_convert(self) -> None:
        if self.busy or not self.inputs:
            return
        out = self.output_var.get().strip()
        if not out:
            messagebox.showwarning("No output", "Choose an output location first.")
            return
        self.busy = True
        self.convert_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        # Read Tk variables here, on the GUI thread; the worker must never touch Tk objects.
        opts = {"units": self.units_var.get(), "keep_position": self.keep_pos_var.get()}
        threading.Thread(target=self._convert_worker, args=(list(self.inputs), out, opts), daemon=True).start()

    def _poll_events(self) -> None:
        """Drain messages posted by the worker thread (runs on the GUI thread)."""
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "done":
                    self._convert_done(*payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _convert_worker(self, inputs: list[str], out: str, opts: dict) -> None:
        outputs: list[str] = []
        try:
            for src in inputs:
                if len(inputs) > 1 or os.path.isdir(out):
                    dst = os.path.join(out, os.path.splitext(os.path.basename(src))[0] + ".glb")
                else:
                    dst = out
                os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
                convert(src, dst, units=opts["units"], keep_position=opts["keep_position"],
                        log_fn=lambda msg: self._events.put(("log", msg)))
                outputs.append(dst)
            self._events.put(("done", (outputs, None)))
        except Exception as exc:  # show the error in the GUI instead of dying silently
            self._events.put(("done", (outputs, f"{exc}\n\n{traceback.format_exc()}")))

    def _convert_done(self, outputs: list[str], error: str | None) -> None:
        self.busy = False
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)  # fully clears the bar; stop() alone leaves the block
        self.convert_btn.configure(state="normal")
        self.last_outputs = outputs
        if outputs:
            self.open_btn.configure(state="normal")
        if error:
            self.log("ERROR: " + error)
            messagebox.showerror("Conversion failed", error.splitlines()[0])
        else:
            self.log("Done.")
            self.root.bell()

    def open_output_folder(self) -> None:
        if not self.last_outputs:
            return
        target = os.path.abspath(self.last_outputs[-1])
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", target])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", target])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(target)])

    # ------------------------------------------------------------------ log --
    def log(self, msg: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def run(self) -> None:
        # Allow launching with a file already: python bambu3mf_gui.py model.3mf
        args = [a for a in sys.argv[1:] if a.lower().endswith(".3mf")]
        if args:
            self.set_inputs(args)
        self.root.mainloop()


if __name__ == "__main__":
    ConverterApp().run()
