"""
CustomTkinter GUI Wrapper for the Trakt-to-MAL pipeline.
"""

from __future__ import annotations

import logging
import threading
import sys
from pathlib import Path

try:
    import customtkinter as ctk
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

from .config import Config
from .main import run_pipeline
from .report_generator import ReportGenerator
import json

# Setup global GUI logger handler to capture logs in text box
class GUIHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state="normal")
            self.text_widget.insert("end", msg + "\n")
            self.text_widget.see("end")
            self.text_widget.configure(state="disabled")
        
        # Schedule on main thread
        self.text_widget.after(0, append)


if HAS_GUI:
    ctk.set_appearance_mode("System")  # Modes: "System" (standard), "Dark", "Light"
    ctk.set_default_color_theme("blue")  # Themes: "blue" (standard), "green", "dark-blue"

    class App(ctk.CTk):
        def __init__(self):
            super().__init__()

            self.title("Trakt-to-MAL Anime Exporter")
            self.geometry("900x700")
            self.config = Config()

            # Grid layout (2 rows)
            self.grid_rowconfigure(0, weight=0)
            self.grid_rowconfigure(1, weight=1)
            self.grid_columnconfigure(0, weight=1)

            # ── Top Frame: Setup & Controls ──────────────────────────────
            self.setup_frame = ctk.CTkFrame(self)
            self.setup_frame.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="ew")
            self.setup_frame.grid_columnconfigure(1, weight=1)

            # TMDB Key
            self.tmdb_label = ctk.CTkLabel(self.setup_frame, text="TMDB API Key:")
            self.tmdb_label.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="w")
            self.tmdb_entry = ctk.CTkEntry(self.setup_frame, width=300)
            self.tmdb_entry.grid(row=0, column=1, padx=10, pady=(10, 5), sticky="ew")
            try:
                # Pre-fill if exists
                self.tmdb_entry.insert(0, self.config.tmdb_api_key)
            except ValueError:
                pass

            # Movies File
            self.movies_label = ctk.CTkLabel(self.setup_frame, text="Movies JSON:")
            self.movies_label.grid(row=1, column=0, padx=10, pady=5, sticky="w")
            self.movies_entry = ctk.CTkEntry(self.setup_frame)
            self.movies_entry.grid(row=1, column=1, padx=10, pady=5, sticky="ew")
            self.movies_entry.insert(0, str(self.config.trakt_movies_file))
            self.movies_btn = ctk.CTkButton(self.setup_frame, text="Browse", width=80, command=self.browse_movies)
            self.movies_btn.grid(row=1, column=2, padx=10, pady=5)

            # Shows File
            self.shows_label = ctk.CTkLabel(self.setup_frame, text="Shows JSON:")
            self.shows_label.grid(row=2, column=0, padx=10, pady=5, sticky="w")
            self.shows_entry = ctk.CTkEntry(self.setup_frame)
            self.shows_entry.grid(row=2, column=1, padx=10, pady=5, sticky="ew")
            self.shows_entry.insert(0, str(self.config.trakt_shows_file))
            self.shows_btn = ctk.CTkButton(self.setup_frame, text="Browse", width=80, command=self.browse_shows)
            self.shows_btn.grid(row=2, column=2, padx=10, pady=5)

            # Start Button
            self.start_btn = ctk.CTkButton(self.setup_frame, text="Start Processing", command=self.start_thread)
            self.start_btn.grid(row=3, column=0, columnspan=3, padx=10, pady=(10, 10))

            # ── Bottom Frame: Logs & Output ──────────────────────────────
            self.log_frame = ctk.CTkFrame(self)
            self.log_frame.grid(row=1, column=0, padx=20, pady=(10, 20), sticky="nsew")
            self.log_frame.grid_rowconfigure(1, weight=1)
            self.log_frame.grid_columnconfigure(0, weight=1)

            self.status_label = ctk.CTkLabel(self.log_frame, text="Status: Ready", font=ctk.CTkFont(weight="bold"))
            self.status_label.grid(row=0, column=0, padx=10, pady=5, sticky="w")

            self.log_textbox = ctk.CTkTextbox(self.log_frame, state="disabled")
            self.log_textbox.grid(row=1, column=0, padx=10, pady=(0, 10), sticky="nsew")

            # Setup logging to textbox
            formatter = logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s", datefmt="%H:%M:%S")
            self.gui_handler = GUIHandler(self.log_textbox)
            self.gui_handler.setFormatter(formatter)
            logging.getLogger().addHandler(self.gui_handler)
            logging.getLogger().setLevel(logging.INFO)

        def browse_movies(self):
            path = ctk.filedialog.askopenfilename(filetypes=[("JSON Files", "*.json")])
            if path:
                self.movies_entry.delete(0, "end")
                self.movies_entry.insert(0, path)

        def browse_shows(self):
            path = ctk.filedialog.askopenfilename(filetypes=[("JSON Files", "*.json")])
            if path:
                self.shows_entry.delete(0, "end")
                self.shows_entry.insert(0, path)

        def start_thread(self):
            self.start_btn.configure(state="disabled", text="Processing...")
            self.log_textbox.configure(state="normal")
            self.log_textbox.delete("1.0", "end")
            self.log_textbox.configure(state="disabled")
            
            # Apply user overrides
            import os
            os.environ["TMDB_API_KEY"] = self.tmdb_entry.get()
            self.config._movies_override = Path(self.movies_entry.get())
            self.config._shows_override = Path(self.shows_entry.get())

            self.status_label.configure(text="Status: Running Pipeline...")
            
            thread = threading.Thread(target=self.run_pipeline_wrapper)
            thread.daemon = True
            thread.start()

        def run_pipeline_wrapper(self):
            try:
                run_pipeline(self.config, verbose=False)
                self.status_label.configure(text="Status: Complete! Check output/ folder.")
            except Exception as e:
                logging.error(f"Pipeline failed: {e}")
                self.status_label.configure(text="Status: Failed. See logs.")
            finally:
                self.start_btn.configure(state="normal", text="Start Processing")


def launch_gui():
    if not HAS_GUI:
        print("GUI dependencies not installed. Please run:")
        print("  pip install customtkinter")
        sys.exit(1)
    
    app = App()
    app.mainloop()

if __name__ == "__main__":
    launch_gui()
