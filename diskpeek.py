#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 minn0x
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""
diskpeek

diskpeek.py

A lightweight cross-platform Tkinter GUI for analysing disk usage.
Scan any directory, sort files and folders by size, drill into cached
subdirectories instantly, and inspect top files, top folders, file type
breakdowns, and overview statistics — all without external dependencies.

Usage:
    python diskpeek.py
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import subprocess
import platform
import heapq
import traceback
from collections import defaultdict

sys.setrecursionlimit(5000)

PROGRESS_CALLBACK_INTERVAL = 1000

# ─── Theme Definitions ────────────────────────────────────────────────────────

THEMES = {
    'light': {
        'bg':           '#f0f0f0',
        'bg_secondary': '#ffffff',
        'fg':           '#000000',
        'entry_bg':     '#ffffff',
        'entry_fg':     '#000000',
        'select_bg':    '#0078d7',
        'select_fg':    '#ffffff',
        'tree_dir_fg':  '#0066CC',
        'tree_file_fg': '#333333',
        'btn_theme':    'clam',
    },
    'dark': {
        'bg':           '#1e1e1e',
        'bg_secondary': '#2d2d2d',
        'fg':           '#e0e0e0',
        'entry_bg':     '#3c3c3c',
        'entry_fg':     '#e0e0e0',
        'select_bg':    '#0078d7',
        'select_fg':    '#ffffff',
        'tree_dir_fg':  '#4db8ff',
        'tree_file_fg': '#cccccc',
        'btn_theme':    'clam',
    }
}


def _default_file_type_entry():
    return {'count': 0, 'size': 0}


class DiskAnalyzer:
    """Core logic for calculating directory sizes with caching"""

    def __init__(self, callback=None):
        self.callback = callback
        self.scanned_items = 0
        self.cache: dict = {}
        self.total_size: int = 0
        self._largest_files_heap: list = []
        self._largest_folders_heap: list = []
        self._heap_counter: int = 0
        self._cancel_ref = None
        self.statistics: dict = {}
        self._reset_statistics()

    def _reset_statistics(self):
        self.scanned_items = 0
        self.total_size = 0
        self._largest_files_heap = []
        self._largest_folders_heap = []
        self._heap_counter = 0
        self.statistics = {
            'largest_files': [],
            'largest_folders': [],
            'file_types': defaultdict(_default_file_type_entry),
            'total_files': 0,
            'total_folders': 0
        }

    def scan_full_tree(self, root_path: str) -> int:
        self.cache.clear()
        self._reset_statistics()
        self.total_size = self._scan_and_cache(root_path)
        self._finalize_statistics()
        return self.total_size

    def _track_largest(self, heap: list, entry: dict, key: str, limit: int = 100):
        size = entry[key]
        self._heap_counter += 1
        push_item = (size, self._heap_counter, entry)
        if len(heap) < limit:
            heapq.heappush(heap, push_item)
        elif size > heap[0][0]:
            heapq.heapreplace(heap, push_item)

    def _scan_and_cache(self, path: str) -> int:
        if self._cancel_ref and self._cancel_ref():
            raise InterruptedError("Scan cancelled by user")

        results = []
        total_size = 0

        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if self._cancel_ref and self._cancel_ref():
                        raise InterruptedError("Scan cancelled by user")
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            subdir_size = self._scan_and_cache(entry.path)
                            item = {
                                'name': entry.name,
                                'path': entry.path,
                                'size': subdir_size,
                                'type': 'dir'
                            }
                            results.append(item)
                            total_size += subdir_size
                            self._track_largest(self._largest_folders_heap, item, 'size')
                            self.statistics['total_folders'] += 1

                        elif entry.is_file(follow_symlinks=False):
                            file_size = entry.stat(follow_symlinks=False).st_size
                            item = {
                                'name': entry.name,
                                'path': entry.path,
                                'size': file_size,
                                'type': 'file'
                            }
                            results.append(item)
                            total_size += file_size
                            self._track_largest(self._largest_files_heap, item, 'size')

                            ext = os.path.splitext(entry.name)[1].lower() or '(no extension)'
                            self.statistics['file_types'][ext]['count'] += 1
                            self.statistics['file_types'][ext]['size'] += file_size
                            self.statistics['total_files'] += 1

                            self.scanned_items += 1
                            if self.callback and self.scanned_items % PROGRESS_CALLBACK_INTERVAL == 0:
                                self.callback(self.scanned_items)

                    except (PermissionError, OSError):
                        continue
        except InterruptedError:
            raise
        except (PermissionError, OSError):
            pass

        results.sort(key=lambda x: x['size'], reverse=True)
        self.cache[path] = results
        return total_size

    def _finalize_statistics(self):
        self.statistics['largest_files'] = sorted(
            [entry for _, _c, entry in self._largest_files_heap],
            key=lambda x: x['size'], reverse=True
        )
        self.statistics['largest_folders'] = sorted(
            [entry for _, _c, entry in self._largest_folders_heap],
            key=lambda x: x['size'], reverse=True
        )

    def get_cached_directory(self, path: str) -> list:
        return self.cache.get(path, [])


class DiskSizeGUI:
    """GUI Application for disk space analysis"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DiskPeek")
        self.root.geometry("1200x800")
        self.root.minsize(800, 500)

        self.analyzer = DiskAnalyzer(
            callback=lambda count: self.root.after(0, self.update_progress, count)
        )
        self.root_path: str | None = None
        self.current_path: str | None = None
        self.scanning = threading.Event()
        self.path_stack: list = []
        self.item_paths: dict = {}
        self._cancel_scan = False
        self._current_theme = 'light'

        self.style = ttk.Style(self.root)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Build all widgets first, then apply theme so apply_theme
        # can safely reference every widget without risk of AttributeError.
        self.setup_ui()
        self.apply_theme('light')

    # ─── Theme ───────────────────────────────────────────────────────────────

    def apply_theme(self, theme_name: str):
        t = THEMES[theme_name]
        self._current_theme = theme_name
        self.style.theme_use(t['btn_theme'])

        self.style.configure('.',
            background=t['bg'],
            foreground=t['fg'],
            fieldbackground=t['entry_bg'],
            troughcolor=t['bg_secondary'],
            bordercolor=t['bg_secondary'],
            darkcolor=t['bg_secondary'],
            lightcolor=t['bg'],
        )
        self.style.configure('TFrame', background=t['bg'])
        self.style.configure('TLabel', background=t['bg'], foreground=t['fg'])
        self.style.configure('TButton',
            background=t['bg_secondary'],
            foreground=t['fg'],
            borderwidth=1,
        )
        self.style.map('TButton',
            background=[('active', t['select_bg']), ('pressed', t['select_bg'])],
            foreground=[('active', t['select_fg']), ('pressed', t['select_fg'])],
        )
        self.style.configure('TEntry',
            fieldbackground=t['entry_bg'],
            foreground=t['entry_fg'],
            insertcolor=t['fg'],
        )
        self.style.configure('TNotebook', background=t['bg'], borderwidth=0)
        self.style.configure('TNotebook.Tab',
            background=t['bg_secondary'],
            foreground=t['fg'],
            padding=[8, 3],
        )
        self.style.map('TNotebook.Tab',
            background=[('selected', t['select_bg'])],
            foreground=[('selected', t['select_fg'])],
        )
        self.style.configure('TScrollbar', background=t['bg_secondary'], troughcolor=t['bg'])
        self.style.configure('TPanedwindow', background=t['bg'])
        self.style.configure('Sash', sashrelief=tk.FLAT, sashpad=3)

        self.style.configure('Treeview',
            background=t['bg_secondary'],
            foreground=t['fg'],
            fieldbackground=t['bg_secondary'],
            rowheight=22,
        )
        self.style.configure('Treeview.Heading',
            background=t['bg'],
            foreground=t['fg'],
            relief=tk.FLAT,
        )
        self.style.map('Treeview',
            background=[('selected', t['select_bg'])],
            foreground=[('selected', t['select_fg'])],
        )
        self.style.map('Treeview.Heading',
            background=[('active', t['select_bg'])],
            foreground=[('active', t['select_fg'])],
        )

        self.style.configure('TProgressbar',
            background=t['select_bg'],
            troughcolor=t['bg_secondary'],
        )

        # tk (non-ttk) widgets
        self.root.config(bg=t['bg'])

        # Polish fix #4: apply info_frame relief consistently and colour it
        self.info_frame.config(bg=t['bg'], relief=tk.SUNKEN)
        self.info_label.config(bg=t['bg'], fg=t['fg'])

        self.overview_text.config(
            bg=t['bg_secondary'],
            fg=t['fg'],
            insertbackground=t['fg'],
            selectbackground=t['select_bg'],
            selectforeground=t['select_fg'],
        )
        self.context_menu.config(
            bg=t['bg_secondary'],
            fg=t['fg'],
            activebackground=t['select_bg'],
            activeforeground=t['select_fg'],
        )

        self.tree.tag_configure('directory', foreground=t['tree_dir_fg'],  font=('Arial', 9, 'bold'))
        self.tree.tag_configure('file',      foreground=t['tree_file_fg'], font=('Arial', 9))

        self.theme_btn.config(text="☀ Light Mode" if theme_name == 'dark' else "🌙 Dark Mode")

    def toggle_theme(self):
        self.apply_theme('light' if self._current_theme == 'dark' else 'dark')

    # ─── UI Setup ────────────────────────────────────────────────────────────

    def setup_ui(self):
        top_frame = ttk.Frame(self.root, padding="5")
        top_frame.pack(fill=tk.X)

        self.back_btn = ttk.Button(top_frame, text="← Back", command=self.navigate_back, state=tk.DISABLED)
        self.back_btn.pack(side=tk.LEFT, padx=2)

        self.up_btn = ttk.Button(top_frame, text="↑ Up", command=self.navigate_up, state=tk.DISABLED)
        self.up_btn.pack(side=tk.LEFT, padx=2)

        self.explorer_btn = ttk.Button(top_frame, text="📁 Open in Explorer", command=self.open_in_explorer, state=tk.DISABLED)
        self.explorer_btn.pack(side=tk.LEFT, padx=2)

        ttk.Separator(top_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=5)

        ttk.Label(top_frame, text="Path:").pack(side=tk.LEFT, padx=5)
        self.path_var = tk.StringVar()
        path_entry = ttk.Entry(top_frame, textvariable=self.path_var, width=50)
        path_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        self.browse_btn = ttk.Button(top_frame, text="Browse", command=self.browse_directory)
        self.browse_btn.pack(side=tk.LEFT, padx=2)

        self.scan_btn = ttk.Button(top_frame, text="Scan", command=self.start_scan)
        self.scan_btn.pack(side=tk.LEFT, padx=2)

        self.cancel_btn = ttk.Button(top_frame, text="Cancel", command=self.cancel_scan, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=2)

        self.clear_cache_btn = ttk.Button(top_frame, text="Clear Cache", command=self.clear_cache, state=tk.DISABLED)
        self.clear_cache_btn.pack(side=tk.LEFT, padx=2)

        ttk.Separator(top_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=5)

        self.theme_btn = ttk.Button(top_frame, text="🌙 Dark Mode", command=self.toggle_theme)
        self.theme_btn.pack(side=tk.LEFT, padx=2)

        progress_frame = ttk.Frame(self.root, padding="5")
        progress_frame.pack(fill=tk.X)

        self.progress_label = ttk.Label(progress_frame, text="Ready")
        self.progress_label.pack(side=tk.LEFT, padx=5)

        self.progress_bar = ttk.Progressbar(progress_frame, mode='indeterminate')
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        # info_frame is tk.Frame so apply_theme can set its bg directly
        self.info_frame = tk.Frame(self.root, relief=tk.SUNKEN, bd=1)
        self.info_frame.pack(fill=tk.X)

        self.info_label = tk.Label(self.info_frame, text="", font=('Arial', 9), anchor=tk.W)
        self.info_label.pack(side=tk.LEFT, padx=5, pady=2)

        main_paned = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        tree_frame = ttk.Frame(main_paned, padding="5")
        main_paned.add(tree_frame, weight=3)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical")
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        hsb = ttk.Scrollbar(tree_frame, orient="horizontal")
        hsb.pack(side=tk.BOTTOM, fill=tk.X)

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("size", "size_bytes", "percentage", "items"),
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set
        )
        self.tree.pack(fill=tk.BOTH, expand=True)

        vsb.config(command=self.tree.yview)
        hsb.config(command=self.tree.xview)

        self.tree.heading("#0", text="Name ▼", anchor=tk.W, command=lambda: self.sort_by_column("#0", False))
        self.tree.heading("size", text="Size ▼", anchor=tk.E, command=lambda: self.sort_by_column("size", False))
        self.tree.heading("size_bytes", text="Size (Bytes)", anchor=tk.E, command=lambda: self.sort_by_column("size_bytes", False))
        self.tree.heading("percentage", text="Percentage", anchor=tk.E, command=lambda: self.sort_by_column("percentage", False))
        self.tree.heading("items", text="Type", anchor=tk.CENTER, command=lambda: self.sort_by_column("items", False))

        self.tree.column("#0", width=450, minwidth=200)
        self.tree.column("size", width=120, minwidth=100, anchor=tk.E)
        self.tree.column("size_bytes", width=130, minwidth=100, anchor=tk.E)
        self.tree.column("percentage", width=100, minwidth=80, anchor=tk.E)
        self.tree.column("items", width=80, minwidth=60, anchor=tk.CENTER)

        self.sort_columns = {
            "#0": False, "size": False, "size_bytes": False,
            "percentage": False, "items": False
        }

        self.tree.bind('<Double-1>', self.on_double_click)
        self.tree.bind('<Button-3>', self.show_context_menu)
        self.root.bind('<Button-1>', lambda e: self.context_menu.unpost() if hasattr(self, 'context_menu') else None)

        stats_frame = ttk.Frame(main_paned)
        main_paned.add(stats_frame, weight=1)

        ttk.Label(stats_frame, text="Statistics & Insights", font=('Arial', 10, 'bold')).pack(anchor=tk.W, padx=5, pady=5)

        self.stats_notebook = ttk.Notebook(stats_frame)
        self.stats_notebook.pack(fill=tk.BOTH, expand=True)

        self.create_largest_files_tab()
        self.create_largest_folders_tab()
        self.create_file_types_tab()
        self.create_overview_tab()

        # context_menu created last so the <Button-1> guard above is safe
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Open in Explorer", command=self.open_selected_in_explorer)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy Path", command=self.copy_selected_path)

        status_frame = ttk.Frame(self.root, relief=tk.SUNKEN)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)

        self.status_label = ttk.Label(status_frame, text="Ready - Scan a directory to begin", anchor=tk.W)
        self.status_label.pack(fill=tk.X, padx=5, pady=2)

    def create_largest_files_tab(self):
        tab = ttk.Frame(self.stats_notebook)
        self.stats_notebook.add(tab, text="Top Files")

        columns = ("size", "path")
        self.largest_files_tree = ttk.Treeview(tab, columns=columns, show='headings', height=8)
        self.largest_files_tree.heading("size", text="Size")
        self.largest_files_tree.heading("path", text="Path")
        self.largest_files_tree.column("size", width=120, anchor=tk.E)
        self.largest_files_tree.column("path", width=600)

        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.largest_files_tree.yview)
        self.largest_files_tree.configure(yscrollcommand=scrollbar.set)
        self.largest_files_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.largest_files_tree.bind('<Double-1>', lambda e: self.open_stats_item_in_explorer(self.largest_files_tree))

    def create_largest_folders_tab(self):
        tab = ttk.Frame(self.stats_notebook)
        self.stats_notebook.add(tab, text="Top Folders")

        columns = ("size", "path")
        self.largest_folders_tree = ttk.Treeview(tab, columns=columns, show='headings', height=8)
        self.largest_folders_tree.heading("size", text="Size")
        self.largest_folders_tree.heading("path", text="Path")
        self.largest_folders_tree.column("size", width=120, anchor=tk.E)
        self.largest_folders_tree.column("path", width=600)

        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.largest_folders_tree.yview)
        self.largest_folders_tree.configure(yscrollcommand=scrollbar.set)
        self.largest_folders_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.largest_folders_tree.bind('<Double-1>', lambda e: self.navigate_to_stats_folder(self.largest_folders_tree))

    def create_file_types_tab(self):
        tab = ttk.Frame(self.stats_notebook)
        self.stats_notebook.add(tab, text="File Types")

        columns = ("extension", "count", "size", "percentage")
        self.file_types_tree = ttk.Treeview(tab, columns=columns, show='headings', height=8)
        self.file_types_tree.heading("extension", text="Extension")
        self.file_types_tree.heading("count", text="Files")
        self.file_types_tree.heading("size", text="Total Size")
        self.file_types_tree.heading("percentage", text="% of Total")

        self.file_types_tree.column("extension", width=150)
        self.file_types_tree.column("count", width=100, anchor=tk.E)
        self.file_types_tree.column("size", width=120, anchor=tk.E)
        self.file_types_tree.column("percentage", width=100, anchor=tk.E)

        scrollbar = ttk.Scrollbar(tab, orient="vertical", command=self.file_types_tree.yview)
        self.file_types_tree.configure(yscrollcommand=scrollbar.set)
        self.file_types_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def create_overview_tab(self):
        tab = ttk.Frame(self.stats_notebook)
        self.stats_notebook.add(tab, text="Overview")

        self.overview_text = tk.Text(tab, wrap=tk.WORD, height=8, font=('Courier', 9))
        self.overview_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    # ─── Statistics Display ───────────────────────────────────────────────────

    def update_statistics_display(self):
        stats = self.analyzer.statistics
        total = self.analyzer.total_size

        self.largest_files_tree.delete(*self.largest_files_tree.get_children())
        for item in stats['largest_files'][:10]:
            self.largest_files_tree.insert('', tk.END, values=(
                self.format_size(item['size']),
                item['path']
            ), tags=(item['path'],))

        self.largest_folders_tree.delete(*self.largest_folders_tree.get_children())
        for item in stats['largest_folders'][:10]:
            self.largest_folders_tree.insert('', tk.END, values=(
                self.format_size(item['size']),
                item['path']
            ), tags=(item['path'],))

        self.file_types_tree.delete(*self.file_types_tree.get_children())
        file_types_sorted = sorted(stats['file_types'].items(), key=lambda x: x[1]['size'], reverse=True)
        for ext, data in file_types_sorted[:20]:
            percentage = (data['size'] / total * 100) if total > 0 else 0
            self.file_types_tree.insert('', tk.END, values=(
                ext,
                f"{data['count']:,}",
                self.format_size(data['size']),
                f"{percentage:.2f}%"
            ))

        self.overview_text.config(state=tk.NORMAL)
        self.overview_text.delete('1.0', tk.END)
        largest_file   = stats['largest_files'][0]   if stats['largest_files']   else None
        largest_folder = stats['largest_folders'][0] if stats['largest_folders'] else None
        most_common    = file_types_sorted[0]         if file_types_sorted        else None

        mc_count = f"{most_common[1]['count']:,}"           if most_common else 'N/A'
        mc_size  = self.format_size(most_common[1]['size']) if most_common else 'N/A'

        overview = f"""
SCAN SUMMARY
═══════════════════════════════════════════════════════════════

Total Size:          {self.format_size(total)} ({total:,} bytes)
Total Files:         {stats['total_files']:,}
Total Folders:       {stats['total_folders']:,}
Scanned Items:       {self.analyzer.scanned_items:,}

TOP SPACE CONSUMERS
═══════════════════════════════════════════════════════════════

Largest Single File: {self.format_size(largest_file['size']) if largest_file else 'N/A'}
                     {largest_file['path'] if largest_file else ''}

Largest Folder:      {self.format_size(largest_folder['size']) if largest_folder else 'N/A'}
                     {largest_folder['path'] if largest_folder else ''}

Most Common Type:    {most_common[0] if most_common else 'N/A'}
                     ({mc_count} files, {mc_size})

Average File Size:   {self.format_size(total // stats['total_files']) if stats['total_files'] > 0 else 'N/A'}
"""
        self.overview_text.insert('1.0', overview)
        self.overview_text.config(state=tk.DISABLED)

    # ─── Explorer Integration ─────────────────────────────────────────────────

    def _open_path_in_explorer(self, item_path: str):
        try:
            system = platform.system()
            if system == "Windows":
                if os.path.isfile(item_path):
                    subprocess.run(['explorer', '/select,', item_path])
                else:
                    os.startfile(item_path)
            elif system == "Darwin":
                if os.path.isfile(item_path):
                    subprocess.run(['open', '-R', item_path], timeout=5)
                else:
                    subprocess.run(['open', item_path], timeout=5)
            else:
                path_to_open = os.path.dirname(item_path) if os.path.isfile(item_path) else item_path
                subprocess.run(['xdg-open', path_to_open], timeout=5)
        except subprocess.TimeoutExpired:
            messagebox.showerror("Error", "File manager took too long to respond.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open Explorer: {e}")

    def open_stats_item_in_explorer(self, tree_widget: ttk.Treeview):
        selection = tree_widget.selection()
        if not selection:
            return
        tags = tree_widget.item(selection[0], 'tags')
        if tags:
            self._open_path_in_explorer(tags[0])

    def open_in_explorer(self):
        if not self.current_path or not os.path.exists(self.current_path):
            messagebox.showerror("Error", "Invalid directory path")
            return
        self._open_path_in_explorer(self.current_path)

    def open_selected_in_explorer(self):
        selection = self.tree.selection()
        if not selection:
            return
        item_path = self.item_paths.get(selection[0])
        if item_path:
            self._open_path_in_explorer(item_path)

    # ─── Sorting ──────────────────────────────────────────────────────────────

    def sort_by_column(self, col: str, reverse: bool):
        items = [
            (self.tree.set(item, col) if col != "#0" else self.tree.item(item, "text"), item)
            for item in self.tree.get_children('')
        ]

        if col == "size_bytes":
            items = [(int(val.replace(',', '')) if val else 0, item) for val, item in items]
        elif col == "percentage":
            items = [(float(val.replace('%', '')) if val else 0, item) for val, item in items]
        elif col == "size":
            items = [(self.parse_size(val), item) for val, item in items]

        items.sort(reverse=reverse)
        for index, (_, item) in enumerate(items):
            self.tree.move(item, '', index)

        self.sort_columns[col] = not reverse
        self.update_column_headers(col, reverse)
        self.tree.heading(col, command=lambda: self.sort_by_column(col, not reverse))

    def update_column_headers(self, sorted_col: str, descending: bool):
        headers = {
            "#0": "Name", "size": "Size", "size_bytes": "Size (Bytes)",
            "percentage": "Percentage", "items": "Type"
        }
        for col, text in headers.items():
            arrow = (" ▼" if descending else " ▲") if col == sorted_col else ""
            self.tree.heading(col, text=text + arrow)

    def parse_size(self, size_str: str) -> float:
        if not size_str or size_str == "0 B":
            return 0.0
        units = {'B': 1, 'KB': 1024, 'MB': 1024**2, 'GB': 1024**3, 'TB': 1024**4, 'PB': 1024**5}
        try:
            number, unit = size_str.split()
            return float(number) * units.get(unit, 1)
        except (ValueError, KeyError):
            return 0.0

    # ─── Scanning ────────────────────────────────────────────────────────────

    def browse_directory(self):
        directory = filedialog.askdirectory()
        if directory:
            self.path_var.set(directory)

    def start_scan(self):
        path = self.path_var.get()

        if not path or not os.path.exists(path):
            messagebox.showerror("Error", "Please select a valid directory")
            return

        if self.scanning.is_set():
            messagebox.showwarning("Warning", "Scan already in progress")
            return

        self.scanning.set()
        self._cancel_scan = False
        self.analyzer._cancel_ref = lambda: self._cancel_scan

        self.root_path = path
        self.current_path = path
        self.path_stack = []
        self.item_paths = {}
        self.tree.delete(*self.tree.get_children())
        self.info_label.config(text="")

        self.scan_btn.config(state=tk.DISABLED)
        self.browse_btn.config(state=tk.DISABLED)
        self.clear_cache_btn.config(state=tk.DISABLED)
        self.back_btn.config(state=tk.DISABLED)
        self.up_btn.config(state=tk.DISABLED)
        self.explorer_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)

        self.progress_bar.start(10)
        self.progress_label.config(text="Scanning...")
        self.status_label.config(text="Scanning directory tree...")

        thread = threading.Thread(target=self.scan_directory, daemon=True)
        thread.start()

    def cancel_scan(self):
        self._cancel_scan = True
        self.progress_label.config(text="Cancelling...")

    def scan_directory(self):
        try:
            self.analyzer.scan_full_tree(self.root_path)
            self.root.after(0, self.display_current_directory)
            self.root.after(0, self.update_statistics_display)

        except InterruptedError:
            self.root.after(0, lambda: self.status_label.config(text="Scan cancelled"))
            self.root.after(0, lambda: self.progress_label.config(text="Cancelled"))
            self.root.after(0, self._scan_cleanup)

        except Exception as e:
            traceback.print_exc()
            self.root.after(0, lambda: messagebox.showerror("Error", f"Scan failed: {e}"))
            self.root.after(0, self._scan_cleanup)

    # ─── Display ──────────────────────────────────────────────────────────────

    def display_current_directory(self):
        self.tree.delete(*self.tree.get_children())
        self.item_paths = {}

        results = self.analyzer.get_cached_directory(self.current_path)
        total = self.analyzer.total_size

        for item in results:
            percentage = (item['size'] / total * 100) if total > 0 else 0
            tag = 'directory' if item['type'] == 'dir' else 'file'

            iid = self.tree.insert(
                "", tk.END,
                text=item['name'],
                values=(
                    self.format_size(item['size']),
                    f"{item['size']:,}",
                    f"{percentage:.2f}%",
                    "Folder" if item['type'] == 'dir' else "File"
                ),
                tags=(tag,)
            )
            self.item_paths[iid] = item['path']

        self.info_label.config(
            text=f"Total Size: {self.format_size(total)} ({total:,} bytes) | "
                 f"Path: {self.current_path} | Items: {len(results)}"
        )
        self.path_var.set(self.current_path)

        can_go_up = bool(self.root_path and self.current_path and self.current_path != self.root_path)
        self.up_btn.config(state=tk.NORMAL if can_go_up else tk.DISABLED)
        self.back_btn.config(state=tk.NORMAL if self.path_stack else tk.DISABLED)
        self.explorer_btn.config(state=tk.NORMAL)
        self.update_column_headers("size", True)

        cache_size = len(self.analyzer.cache)
        self.status_label.config(
            text=f"Displaying {len(results)} items | {cache_size} directories cached | Click headers to sort"
        )

        if self.scanning.is_set():
            self._scan_cleanup()

    def _scan_cleanup(self):
        self.scanning.clear()
        self.progress_bar.stop()
        self.progress_label.config(text="Scan complete - Cache ready")
        self.scan_btn.config(state=tk.NORMAL)
        self.browse_btn.config(state=tk.NORMAL)
        self.clear_cache_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)

    # ─── Navigation ──────────────────────────────────────────────────────────

    def on_double_click(self, event):
        item = self.tree.selection()
        if not item:
            return
        values = self.tree.item(item[0], 'values')
        if len(values) >= 4 and values[3] == "Folder":
            new_path = self.item_paths.get(item[0])
            if new_path and os.path.exists(new_path) and os.path.isdir(new_path):
                self.path_stack.append(self.current_path)
                self.current_path = new_path
                self.display_current_directory()

    def navigate_back(self):
        if self.path_stack:
            self.current_path = self.path_stack.pop()
            self.display_current_directory()

    def navigate_up(self):
        if self.current_path and self.root_path and self.current_path != self.root_path:
            parent = os.path.dirname(self.current_path)
            if parent in self.analyzer.cache:
                self.path_stack.append(self.current_path)
                self.current_path = parent
                self.display_current_directory()

    def navigate_to_stats_folder(self, tree_widget: ttk.Treeview):
        selection = tree_widget.selection()
        if not selection:
            return
        tags = tree_widget.item(selection[0], 'tags')
        if tags:
            folder_path = tags[0]
            if folder_path in self.analyzer.cache and os.path.isdir(folder_path):
                # Bug fix #2: guard against None current_path (e.g. after clear_cache)
                if self.current_path:
                    self.path_stack.append(self.current_path)
                self.current_path = folder_path
                self.display_current_directory()

    # ─── Context Menu ─────────────────────────────────────────────────────────

    def show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def copy_selected_path(self):
        selection = self.tree.selection()
        if not selection:
            return
        item_path = self.item_paths.get(selection[0])
        if item_path:
            self.root.clipboard_clear()
            self.root.clipboard_append(item_path)
            self.status_label.config(text=f"Copied to clipboard: {item_path}")

    # ─── Cache ────────────────────────────────────────────────────────────────

    def clear_cache(self):
        self.analyzer.cache.clear()
        self.tree.delete(*self.tree.get_children())
        self.item_paths = {}
        self.largest_files_tree.delete(*self.largest_files_tree.get_children())
        self.largest_folders_tree.delete(*self.largest_folders_tree.get_children())
        self.file_types_tree.delete(*self.file_types_tree.get_children())
        self.overview_text.config(state=tk.NORMAL)
        self.overview_text.delete('1.0', tk.END)
        self.overview_text.config(state=tk.DISABLED)
        self.root_path = None
        self.current_path = None
        self.path_stack = []
        self.info_label.config(text="")
        self.status_label.config(text="Cache cleared - Ready to scan")
        self.clear_cache_btn.config(state=tk.DISABLED)
        self.back_btn.config(state=tk.DISABLED)
        self.up_btn.config(state=tk.DISABLED)
        self.explorer_btn.config(state=tk.DISABLED)
        self.progress_label.config(text="Ready")

    # ─── Utilities ────────────────────────────────────────────────────────────

    def format_size(self, size: int) -> str:
        if size <= 0:
            return "0 B"
        for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} EB"

    def update_progress(self, count: int):
        self.progress_label.config(text=f"Scanned {count:,} items...")

    def on_close(self, attempts: int = 0):
        if self.scanning.is_set() and attempts < 25:
            self._cancel_scan = True
            self.root.after(200, lambda: self.on_close(attempts + 1))
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    app = DiskSizeGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
