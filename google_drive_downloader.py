"""Google Drive batch downloader using the Drive API and per-user OAuth."""
from __future__ import annotations

import os
import mimetypes
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

URL_RE = re.compile(r"https?://(?:drive|docs)\.google\.com/[^\s]+", re.I)
ID_RE = re.compile(
    r"(?:/folders/|/file/d/|/(?:document|spreadsheets|presentation)/d/|/d/|[?&]id=)([\w-]+)",
    re.I,
)
FOLDER_MIME = "application/vnd.google-apps.folder"
EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
    "image/tiff": ".tif", "image/heic": ".heic", "image/heif": ".heif",
    "image/avif": ".avif", "image/svg+xml": ".svg",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv",
    "video/webm": ".webm", "video/x-msvideo": ".avi", "video/x-ms-wmv": ".wmv",
    "video/mpeg": ".mpeg", "video/3gpp": ".3gp", "video/x-flv": ".flv",
    "video/x-m4v": ".m4v", "video/m4v": ".m4v",
}


def is_folder_link(url: str) -> bool:
    return bool(re.search(r"/folders/[\w-]+", url, re.I))


def get_drive_id(url: str) -> str:
    match = ID_RE.search(url)
    if not match:
        raise ValueError(f"无法从链接中读取 Google Drive 文件 ID：{url}")
    return match.group(1)


def safe_filename(name: str) -> str:
    name = name.strip().replace("/", "_").replace("\\", "_")
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).rstrip(" .")
    if not name or name in {".", ".."}:
        raise ValueError("自定义名称不能为空，也不能是 . 或 ..")
    return name


def resolve_file_target(target: Path) -> tuple[Path, bool]:
    """Avoid treating a same-named directory as an already-downloaded file.

    Windows cannot store a file and directory at the exact same path. If a
    folder occupies the requested file path, keep the folder and give the file
    a clear ``（文件）`` suffix instead of incorrectly skipping it.
    """
    if not target.exists() or not target.is_dir():
        return target, False

    stem = target.stem if target.suffix else target.name
    suffix = target.suffix
    index = 1
    while True:
        label = "（文件）" if index == 1 else f"（文件 {index}）"
        candidate = target.with_name(f"{stem}{label}{suffix}")
        if not candidate.exists():
            return candidate, True
        if candidate.is_file():
            # An actual file with the disambiguated name already exists.
            return candidate, True
        index += 1


def add_missing_extension(name: str, mime_type: str) -> str:
    """Preserve a supplied suffix, or infer one from the Drive MIME type."""
    if Path(name).suffix:
        return name
    extension = MIME_EXTENSIONS.get(mime_type.lower())
    if not extension:
        extension = mimetypes.guess_extension(mime_type, strict=False)
    return name + extension if extension else name


def credentials_file(custom_path: str | None = None) -> Path | None:
    """Return the path to credentials.json from user configuration.

    Args:
        custom_path: Optional custom path to credentials.json file

    Returns:
        Path to credentials.json file, or None if not configured
    """
    # First priority: custom path provided by caller
    if custom_path:
        return Path(custom_path)

    # Second priority: user-configured path from settings
    user_configured = load_credentials_path()
    if user_configured:
        return Path(user_configured)

    # No default path - user must configure it
    return None


def config_dir() -> Path:
    """Return the application configuration directory."""
    root = Path(os.environ.get("APPDATA", Path.home())) / "GoogleDriveDownloader"
    root.mkdir(parents=True, exist_ok=True)
    return root


def token_file() -> Path:
    return config_dir() / "token.json"


def config_file() -> Path:
    """Return the path to the configuration file storing user settings."""
    return config_dir() / "config.json"


def load_credentials_path() -> str | None:
    """Load the user-configured credentials.json path from config file."""
    import json
    cfg = config_file()
    if not cfg.is_file():
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        return data.get("credentials_path")
    except Exception:
        return None


def save_credentials_path(path: str) -> None:
    """Save the user-configured credentials.json path to config file."""
    import json
    cfg = config_file()
    data = {"credentials_path": path}
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def authorize_drive(custom_credentials_path: str | None = None):
    """Load/refresh the local user's token, or open the first-run OAuth flow.

    Args:
        custom_credentials_path: Optional path to credentials.json file

    Returns:
        Tuple of (service, credentials)
    """
    from google.auth.transport.requests import Request
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    client_file = credentials_file(custom_credentials_path)

    if client_file is None:
        raise FileNotFoundError(
            "未配置 credentials.json 文件路径。\n\n"
            "请通过菜单栏【设置】→【配置 Credentials 文件路径】指定您的 OAuth 客户端 JSON 文件路径。\n\n"
            "步骤：\n"
            "1. 在您自己的 Google Cloud 项目中创建 OAuth 2.0 客户端 ID（应用类型：桌面应用）\n"
            "2. 下载 credentials.json 文件\n"
            "3. 在本程序的设置菜单中指定该文件的完整路径"
        )

    saved_token = token_file()
    if not client_file.is_file():
        raise FileNotFoundError(
            f"配置的 credentials.json 文件不存在：\n{client_file}\n\n"
            "请通过菜单栏【设置】→【配置 Credentials 文件路径】重新配置正确的文件路径。\n\n"
            "如果您还没有 credentials.json 文件，请先：\n"
            "1. 在您自己的 Google Cloud 项目中创建 OAuth 2.0 客户端 ID（应用类型：桌面应用）\n"
            "2. 下载 credentials.json 文件\n"
            "3. 在设置中指定该文件的完整路径"
        )

    creds = None
    if saved_token.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(saved_token), scopes)
        except (ValueError, OSError):
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # Revoked/expired refresh token: obtain a fresh authorization.
            creds = None

    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_file), scopes)
        creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)

    saved_token.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds, cache_discovery=False), creds


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Google Drive 下载器")
        self.geometry("1180x760")
        self.minsize(900, 600)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.running = False
        self.total_items = 0
        self.finished_items = 0
        self.stop_event = threading.Event()
        self.record_item_map: dict[str, str] = {}
        self.dest = tk.StringVar(value=str(Path.home() / "Downloads"))
        self.credentials_path = load_credentials_path()  # Load user-configured credentials path
        self.setup_modern_dark_theme()
        self._build()
        self.after(100, self._poll)

    def setup_modern_dark_theme(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        bg, fg, entry_bg = "#25272B", "#E7E9EC", "#34373C"
        self.configure(bg=bg)
        style.configure(".", background=bg, foreground=fg, font=("Microsoft YaHei", 10))
        style.configure("TFrame", background=bg)
        style.configure("TLabelframe", background=bg, foreground=fg, bordercolor="#333333", relief="solid")
        style.configure("TLabelframe.Label", background=bg, foreground="#C9D0D7", font=("Microsoft YaHei", 10, "bold"))
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TButton", background="#2C2C2C", foreground="#FFFFFF", bordercolor="#444444", padding=(10, 5))
        style.map("TButton", background=[("active", "#3A3A3A"), ("pressed", "#555555")], foreground=[("disabled", "#666666")])
        style.configure("Accent.TButton", background="#287DB5", foreground="#FFFFFF", font=("Microsoft YaHei", 10, "bold"))
        style.map("Accent.TButton", background=[("active", "#358FC8"), ("pressed", "#1E689A")])
        style.configure("TEntry", fieldbackground=entry_bg, foreground=fg, insertcolor=fg)
        style.configure("Treeview", background="#2E3034", foreground="#F0F0F0", fieldbackground="#2E3034", rowheight=26)
        style.configure("Treeview.Heading", background="#474A4F", foreground="#FFFFFF", font=("Microsoft YaHei", 10, "bold"))
        style.map("Treeview", background=[("selected", "#315F7D")], foreground=[("selected", "#FFFFFF")])
        style.configure("Horizontal.TProgressbar", background="#358FC8", troughcolor="#3A3D42")

    def _build(self) -> None:
        # Create menu bar
        menubar = tk.Menu(self, bg="#2C2C2C", fg="#FFFFFF", activebackground="#287DB5", activeforeground="#FFFFFF")
        self.config(menu=menubar)

        settings_menu = tk.Menu(menubar, tearoff=0, bg="#2C2C2C", fg="#FFFFFF", 
                                activebackground="#287DB5", activeforeground="#FFFFFF")
        menubar.add_cascade(label="设置", menu=settings_menu)
        settings_menu.add_command(label="配置 Credentials 文件路径", command=self._show_settings)
        settings_menu.add_separator()
        settings_menu.add_command(label="清除当前授权", command=self._clear_authorization)
        settings_menu.add_separator()
        settings_menu.add_command(label="查看当前配置", command=self._show_current_config)

        main = ttk.Frame(self, padding=12); main.pack(fill="both", expand=True)
        header = ttk.Frame(main); header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="Google Drive 下载器", font=("Microsoft YaHei", 17, "bold"), foreground="#F1F3F5").pack(side="left")
        body = ttk.Panedwindow(main, orient="horizontal"); body.pack(fill="both", expand=True)
        left = ttk.Frame(body, padding=(0, 0, 8, 0)); right = ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(left, weight=3); body.add(right, weight=2)
        link_frame = ttk.LabelFrame(left, text="下载列表", padding=8); link_frame.pack(fill="both", expand=True, pady=(0, 8))
        table_frame = ttk.Frame(link_frame); table_frame.pack(fill="both", expand=True)
        self.link_tree = ttk.Treeview(table_frame, columns=("url", "name"), show="headings", selectmode="extended")
        self.link_tree.heading("url", text="链接"); self.link_tree.heading("name", text="文件名（可选）")
        self.link_tree.column("url", width=450, minwidth=220, anchor="w"); self.link_tree.column("name", width=190, minwidth=120, anchor="w")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.link_tree.yview)
        self.link_tree.configure(yscrollcommand=scroll.set); self.link_tree.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        bottom = ttk.Frame(link_frame); bottom.pack(fill="x", pady=(8, 0))
        ttk.Button(bottom, text="添加", command=self._add_link_row).pack(side="left", padx=(0, 6))
        ttk.Button(bottom, text="删除选中", command=self._delete_selected_rows).pack(side="left", padx=6)
        ttk.Button(bottom, text="清空", command=self._clear_link_rows).pack(side="left", padx=6)
        self.rename_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bottom, text="使用自定义名称", variable=self.rename_var).pack(side="right", padx=5)
        self.link_tree.bind("<Double-1>", self._edit_link_cell)
        self.link_tree.bind("<Control-v>", self._paste_links); self.link_tree.bind("<Control-V>", self._paste_links)
        self._create_context_menu()
        folder_frame = ttk.LabelFrame(left, text="保存位置", padding=8); folder_frame.pack(fill="x", pady=(0, 8))
        path_row = ttk.Frame(folder_frame); path_row.pack(fill="x")
        ttk.Entry(path_row, textvariable=self.dest).pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(path_row, text="浏览…", command=self._choose).pack(side="right")
        actions = ttk.Frame(left); actions.pack(fill="x")
        self.start_btn = ttk.Button(actions, text="开始下载", command=self._start, style="Accent.TButton")
        self.start_btn.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 5))
        self.stop_btn = ttk.Button(actions, text="停止下载", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", fill="x", expand=True, ipady=5, padx=(5, 0))
        status_frame = ttk.LabelFrame(right, text="下载状态", padding=10); status_frame.pack(fill="x", pady=(0, 8))
        self.status = ttk.Label(status_frame, text="就绪", font=("Microsoft YaHei", 10, "bold")); self.status.pack(anchor="w", pady=(0, 7))
        self.progress = ttk.Progressbar(status_frame, orient="horizontal", mode="determinate"); self.progress.pack(fill="x")
        record_frame = ttk.LabelFrame(right, text="运行记录", padding=8); record_frame.pack(fill="both", expand=True)
        self.record_tree = ttk.Treeview(record_frame, columns=("seq", "name", "state", "path"), show="headings", selectmode="browse")
        self.record_tree.tag_configure("success", foreground="#4CAF50")
        self.record_tree.tag_configure("failure", foreground="#F05A5A")
        for col, title in (("seq", "序号"), ("name", "文件名"), ("state", "状态/进度"), ("path", "保存路径")):
            self.record_tree.heading(col, text=title)
        self.record_tree.column("seq", width=55, minwidth=45, anchor="center", stretch=False)
        self.record_tree.column("name", width=140, minwidth=100, anchor="w")
        self.record_tree.column("state", width=105, minwidth=90, anchor="center")
        self.record_tree.column("path", width=280, minwidth=160, anchor="w")
        rec_scroll = ttk.Scrollbar(record_frame, orient="vertical", command=self.record_tree.yview)
        self.record_tree.configure(yscrollcommand=rec_scroll.set); self.record_tree.pack(side="left", fill="both", expand=True); rec_scroll.pack(side="right", fill="y")

    def _create_context_menu(self):
        self.context_menu = tk.Menu(self, tearoff=0, bg="#303338", fg="#F0F0F0", activebackground="#287DB5", activeforeground="#FFFFFF")
        self.context_menu.add_command(label="粘贴表格内容 (Ctrl+V)", command=self._paste_links)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="添加新空白行", command=self._add_link_row)
        self.context_menu.add_command(label="删除选中行", command=self._delete_selected_rows)
        self.context_menu.add_command(label="清空所有列表", command=self._clear_link_rows)
        self.link_tree.bind("<Button-3>", self._show_context_menu)

    def _show_context_menu(self, event):
        item = self.link_tree.identify_row(event.y)
        if item and item not in self.link_tree.selection(): self.link_tree.selection_set(item)
        try: self.context_menu.tk_popup(event.x_root, event.y_root)
        finally: self.context_menu.grab_release()

    def _choose(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.dest.get() or str(Path.home()))
        if chosen: self.dest.set(chosen)

    def _write(self, text: str) -> None: self.status.configure(text=text)

    def _add_record(self, sequence: int, name: str, state: str, path: str) -> str:
        tag = "success" if state.startswith("已完成") else "failure" if state == "失败" else ""
        return self.record_tree.insert("", "end", values=(sequence, name, state, path), tags=(tag,) if tag else ())

    def _update_record(self, item: str, name: str | None = None, state: str | None = None, path: str | None = None) -> None:
        if not self.record_tree.exists(item): return
        values = list(self.record_tree.item(item, "values"))
        if name is not None: values[1] = name
        if state is not None: values[2] = state
        if path is not None: values[3] = path
        tag = "success" if str(values[2]).startswith("已完成") else "failure" if values[2] == "失败" else ""
        self.record_tree.item(item, values=values, tags=(tag,) if tag else ())

    def _stop(self) -> None:
        if self.running:
            self.stop_event.set(); self.stop_btn.configure(state="disabled")
            self.status.configure(text="正在停止当前下载…")

    def _add_link_row(self): self.link_tree.insert("", "end", values=("", ""))
    def _delete_selected_rows(self):
        for item in self.link_tree.selection(): self.link_tree.delete(item)
    def _clear_link_rows(self):
        for item in self.link_tree.get_children(): self.link_tree.delete(item)

    def _edit_link_cell(self, event):
        item = self.link_tree.identify_row(event.y); column = self.link_tree.identify_column(event.x)
        if not item or column not in ("#1", "#2"): return
        bbox = self.link_tree.bbox(item, column)
        if not bbox: return
        x, y, width, height = bbox; values = list(self.link_tree.item(item, "values")); index = 0 if column == "#1" else 1
        entry = tk.Entry(self.link_tree, bg="#2A2A2A", fg="#FFFFFF", insertbackground="#FFFFFF", selectbackground="#00C853", relief="flat", borderwidth=0, highlightthickness=1, highlightbackground="#00C853", font=("Microsoft YaHei", 10))
        entry.place(x=x, y=y, width=width, height=height); entry.insert(0, values[index]); entry.select_range(0, tk.END); entry.focus_set()
        def save(event=None):
            if entry.winfo_exists():
                values[index] = entry.get().strip(); self.link_tree.item(item, values=values); entry.destroy()
        def cancel(event=None):
            if entry.winfo_exists(): entry.destroy()
        entry.bind("<Return>", save); entry.bind("<Escape>", cancel); entry.bind("<FocusOut>", save)

    def _paste_links(self, event=None):
        """Append links, pair tabular columns, or fill names into existing rows."""
        try: clipboard = self.clipboard_get()
        except Exception as exc:
            self._write(f"读取剪贴板异常: {exc}"); return "break"
        if not clipboard or not clipboard.strip(): return "break"
        # Ignore blank lines anywhere in pasted content so they do not create
        # empty rows or shift the pairing between links and optional names.
        lines = [
            line for line in clipboard.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            if line.strip()
        ]
        if not lines: return "break"
        parsed = [line.split("\t") for line in lines]
        is_table = any(len(cells) > 1 for cells in parsed)
        is_url = lambda value: bool(URL_RE.search(value.strip()))
        children = list(self.link_tree.get_children())
        one_col = [cells[0].strip() if cells else "" for cells in parsed]
        names_only = not is_table and not any(is_url(value) for value in one_col if value)
        if names_only:
            selected = self.link_tree.selection()
            if selected:
                try: start = children.index(selected[0])
                except ValueError: start = len(children)
            else:
                start = next((i for i, item in enumerate(children) if self.link_tree.item(item, "values")[0] and not self.link_tree.item(item, "values")[1]), len(children))
            while len(children) < start + len(one_col): children.append(self.link_tree.insert("", "end", values=("", "")))
            for offset, name in enumerate(one_col):
                item = children[start + offset]; url, old_name = self.link_tree.item(item, "values")
                self.link_tree.item(item, values=(url, name or old_name))
            self._write("已填入名称。")
        else:
            for cells in parsed:
                self.link_tree.insert("", "end", values=(cells[0].strip() if cells else "", cells[1].strip() if len(cells) > 1 else ""))
            self._write(f"已追加 {len(parsed)} 行。")
        return "break"

    def _show_settings(self) -> None:
        """Show settings dialog for configuring credentials.json path."""
        dialog = tk.Toplevel(self)
        dialog.title("配置设置")
        dialog.geometry("700x280")
        dialog.resizable(False, False)
        dialog.configure(bg="#25272B")
        dialog.transient(self)
        dialog.grab_set()

        # Center the dialog
        dialog.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog.winfo_height()) // 2
        dialog.geometry(f"+{x}+{y}")

        main_frame = ttk.Frame(dialog, padding=20)
        main_frame.pack(fill="both", expand=True)

        # Instructions
        instruction_text = (
            "请在您自己的 Google Cloud 项目中创建 OAuth 2.0 客户端 ID（应用类型：桌面应用），\n"
            "下载 credentials.json 文件，然后在下方指定该文件的完整路径。\n\n"
            "每个用户应使用自己的 Google Cloud 项目和凭据文件。"
        )
        instruction_label = ttk.Label(main_frame, text=instruction_text, 
                                     font=("Microsoft YaHei", 9), 
                                     foreground="#C9D0D7")
        instruction_label.pack(anchor="w", pady=(0, 15))

        # Path input
        path_frame = ttk.Frame(main_frame)
        path_frame.pack(fill="x", pady=(0, 20))

        ttk.Label(path_frame, text="Credentials.json 文件路径：", 
                 font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(0, 5))

        path_entry_frame = ttk.Frame(path_frame)
        path_entry_frame.pack(fill="x")

        path_var = tk.StringVar(value=self.credentials_path or "")
        path_entry = ttk.Entry(path_entry_frame, textvariable=path_var, font=("Microsoft YaHei", 10))
        path_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def browse_file():
            filename = filedialog.askopenfilename(
                title="选择 credentials.json 文件",
                filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
                initialdir=str(Path.home())
            )
            if filename:
                path_var.set(filename)

        ttk.Button(path_entry_frame, text="浏览…", command=browse_file).pack(side="right")

        # Buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(10, 0))

        def save_and_close():
            path = path_var.get().strip()
            if path:
                if not Path(path).is_file():
                    messagebox.showwarning("文件不存在", 
                                         f"指定的文件不存在：\n{path}\n\n请检查路径是否正确。",
                                         parent=dialog)
                    return
                save_credentials_path(path)
                self.credentials_path = path
                messagebox.showinfo("保存成功", 
                                  "Credentials 文件路径已保存！\n\n下次授权时将使用此文件。",
                                  parent=dialog)
                dialog.destroy()
            else:
                # Clear the configuration
                if messagebox.askyesno("确认清除", 
                                      "路径为空，是否清除已保存的配置？",
                                      parent=dialog):
                    cfg = config_file()
                    if cfg.is_file():
                        cfg.unlink()
                    self.credentials_path = None
                    messagebox.showinfo("已清除", "配置已清除。", parent=dialog)
                    dialog.destroy()

        ttk.Button(button_frame, text="保存", command=save_and_close, 
                  style="Accent.TButton").pack(side="right", padx=(5, 0))
        ttk.Button(button_frame, text="取消", command=dialog.destroy).pack(side="right")

    def _show_current_config(self) -> None:
        """Show current configuration information."""
        current_path = self.credentials_path or "未配置"

        message = (
            f"当前配置的 Credentials 文件路径：\n{current_path}\n\n"
            f"Token 保存位置：\n{token_file()}\n\n"
            f"配置文件位置：\n{config_file()}"
        )
        messagebox.showinfo("当前配置", message)

    def _clear_authorization(self) -> None:
        """Clear the current authorization token."""
        if self.running:
            messagebox.showwarning("操作被阻止", "下载任务正在运行中，无法清除授权。\n请先停止下载任务。")
            return

        token_path = token_file()
        if not token_path.is_file():
            messagebox.showinfo("无需清除", "当前没有保存的授权信息。")
            return

        result = messagebox.askyesno(
            "确认清除授权",
            "确定要清除当前保存的授权信息吗？\n\n"
            "清除后，下次下载时需要重新进行 Google 账号授权。\n\n"
            f"将删除文件：\n{token_path}",
            icon="warning"
        )

        if result:
            try:
                token_path.unlink()
                messagebox.showinfo("清除成功", "授权信息已清除。\n\n下次下载时需要重新授权。")
                self._write("授权已清除，下次下载需重新授权")
            except Exception as exc:
                messagebox.showerror("清除失败", f"无法删除授权文件：\n{exc}")

    def _start(self) -> None:
        items = []
        for row in self.record_tree.get_children(): self.record_tree.delete(row)
        self.record_item_map.clear()
        for item in self.link_tree.get_children():
            values = self.link_tree.item(item, "values"); url = values[0].strip() if values else ""; name = values[1].strip() if len(values) > 1 else ""
            if not url: continue
            if not URL_RE.search(url): self._write(f"跳过无效链接格式：{url}"); continue
            items.append((url, name if self.rename_var.get() and name else None))
        if not items:
            messagebox.showwarning("没有有效链接", "请在表格中填写或粘贴至少一个正确的 Google Drive 链接。"); return
        raw_dest = self.dest.get().strip()
        if not raw_dest:
            messagebox.showwarning("没有保存位置", "请选择保存文件夹。"); return
        dest = Path(raw_dest).expanduser()
        rows = []
        for i, (url, name) in enumerate(items, 1):
            label = name or ("Google Drive 文件夹" if is_folder_link(url) else "读取文件名…")
            row = self._add_record(i, label, "等待中", str(dest))
            self.record_item_map[row] = row; rows.append((url, name, row))
        self.running = True; self.stop_event.clear(); self.start_btn.configure(state="disabled"); self.stop_btn.configure(state="normal")
        self.progress.configure(value=0, maximum=100); self.status.configure(text=f"准备处理 {len(items)} 个下载项…")
        self.total_items = len(rows); self.finished_items = 0
        threading.Thread(target=self._download_worker, args=(rows, dest), daemon=True).start()

    def _list_children(self, service, folder_id: str) -> list[dict]:
        files = []; token = None
        while not self.stop_event.is_set():
            result = service.files().list(
                q=f"'{folder_id}' in parents and trashed = false", pageSize=1000, pageToken=token,
                fields="nextPageToken,files(id,name,mimeType,size)", supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute()
            files.extend(result.get("files", [])); token = result.get("nextPageToken")
            if not token: break
        return files

    def _download_media(self, service, creds, file_id: str, target: Path, size: int | None,
                        record_id: str) -> None:
        from google.auth.transport.requests import AuthorizedSession
        session = AuthorizedSession(creds)
        part = target.with_name(target.name + ".part")
        if size is not None and part.exists() and part.stat().st_size > size: part.unlink()
        attempts = 0
        while not self.stop_event.is_set():
            offset = part.stat().st_size if part.exists() else 0
            request = service.files().get_media(fileId=file_id)
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with session.get(request.uri, headers=headers, stream=True, timeout=(30, 90)) as response:
                    if response.status_code in (429, 500, 502, 503, 504):
                        attempts += 1
                        if attempts > 5: response.raise_for_status()
                        wait = min(30, 2 ** attempts)
                        self.events.put(("log", f"服务器暂时限流或繁忙，{wait} 秒后重试…"))
                        for _ in range(wait * 10):
                            if self.stop_event.is_set(): return
                            time.sleep(0.1)
                        continue
                    response.raise_for_status()
                    # If the server ignores Range, restart this partial file safely.
                    mode = "ab" if offset and response.status_code == 206 else "wb"
                    if mode == "wb": offset = 0
                    with part.open(mode) as output:
                        for chunk in response.iter_content(chunk_size=1024 * 1024):
                            if self.stop_event.is_set(): return
                            if chunk: output.write(chunk)
                            current = part.stat().st_size if part.exists() else 0
                            total = size or int(response.headers.get("Content-Length") or 0) + offset
                            if total:
                                percent = min(99.9, current * 100 / total)
                                self.events.put(("record", record_id, None, f"{percent:.1f}%", None))
                    if size is not None and part.stat().st_size != size:
                        raise IOError(f"文件大小不完整：{part.stat().st_size}/{size} 字节")
                    os.replace(part, target)
                    return
            except Exception as exc:
                if self.stop_event.is_set(): return
                attempts += 1
                if attempts > 5: raise RuntimeError(f"下载失败：{exc}") from exc
                wait = min(30, 2 ** attempts)
                self.events.put(("log", f"下载中断，保留部分文件并在 {wait} 秒后续传…"))
                for _ in range(wait * 10):
                    if self.stop_event.is_set(): return
                    time.sleep(0.1)

    def _download_one(self, service, creds, metadata: dict, target_dir: Path, custom_name: str | None,
                      record_id: str) -> tuple[str, str]:
        file_id = metadata["id"]; mime = metadata.get("mimeType", "")
        original_name = metadata.get("name") or file_id
        export = EXPORTS.get(mime)
        if export:
            export_mime, extension = export
            base = safe_filename(custom_name) if custom_name else original_name
            if not Path(base).suffix: base += extension
            target = target_dir / safe_filename(base)
            target, renamed_for_folder = resolve_file_target(target)
            if target.is_file(): return target.name, "已存在，跳过"
            target_dir.mkdir(parents=True, exist_ok=True)
            data = service.files().export(fileId=file_id, mimeType=export_mime).execute()
            target.write_bytes(data)
            return target.name, "已完成（与同名文件夹区分）" if renamed_for_folder else "已完成"

        base = safe_filename(custom_name) if custom_name else safe_filename(original_name)
        # The Drive API's name normally includes an extension, but some shared
        # image/video files omit it. Fall back to the API MIME type in that case.
        if custom_name:
            source_suffix = Path(original_name).suffix
            if not Path(base).suffix and source_suffix:
                base += source_suffix
        base = safe_filename(add_missing_extension(base, mime))
        target = target_dir / base
        target, renamed_for_folder = resolve_file_target(target)
        if target.is_file(): return target.name, "已存在，跳过"
        target_dir.mkdir(parents=True, exist_ok=True)
        size = int(metadata["size"]) if metadata.get("size") is not None else None
        self._download_media(service, creds, file_id, target, size, record_id)
        if self.stop_event.is_set(): return target.name, "已停止"
        return target.name, "已完成（与同名文件夹区分）" if renamed_for_folder else "已完成"

    def _download_worker(self, items: list[tuple[str, str | None, str]], dest: Path) -> None:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            self.events.put(("status", "正在连接 Google Drive 并检查授权…"))
            service, creds = authorize_drive(self.credentials_path)
            sequence = 0
            for i, (url, custom_name, root_record) in enumerate(items, 1):
                if self.stop_event.is_set(): break
                current_name = custom_name or url
                current_path = str(dest)
                self.events.put(("log", f"[{i}/{len(items)}] 正在读取：{url}"))
                self.events.put(("record", root_record, None, "读取中", None))
                try:
                    drive_id = get_drive_id(url)
                    metadata = service.files().get(fileId=drive_id, fields="id,name,mimeType,size", supportsAllDrives=True).execute()
                    current_name = metadata.get("name") or current_name
                    if metadata.get("mimeType") != FOLDER_MIME:
                        sequence += 1
                        rec = root_record
                        self.events.put(("record", rec, current_name, "下载中", str(dest)))
                        name, state = self._download_one(service, creds, metadata, dest, custom_name, rec)
                        self.events.put(("record", rec, name, state, str(dest)))
                        if state.startswith("已完成"): self.events.put(("log", f"已保存：{dest / name}"))
                    else:
                        root_name = safe_filename(custom_name) if custom_name else safe_filename(metadata.get("name", drive_id))
                        root_dir = dest / root_name
                        root_dir.mkdir(parents=True, exist_ok=True)
                        current_name = root_name
                        current_path = str(root_dir)
                        self.events.put(("record", root_record, root_name, "读取文件夹…", str(root_dir)))
                        stack = [(drive_id, root_dir)]; visited = set(); found = 0
                        while stack and not self.stop_event.is_set():
                            folder_id, local_dir = stack.pop()
                            if folder_id in visited: continue
                            visited.add(folder_id)
                            try:
                                children = self._list_children(service, folder_id)
                            except Exception as exc:
                                self.events.put(("log", f"读取子文件夹失败，跳过：{local_dir}；{exc}"))
                                continue
                            for child in children:
                                if self.stop_event.is_set(): break
                                if child.get("mimeType") == FOLDER_MIME:
                                    child_dir = local_dir / safe_filename(child.get("name", child["id"]))
                                    child_dir.mkdir(parents=True, exist_ok=True)
                                    stack.append((child["id"], child_dir))
                                    continue
                                found += 1; sequence += 1
                                rec = root_record if found == 1 else f"{root_record}_file_{found}"
                                name = child.get("name", child["id"])
                                if found == 1:
                                    self.events.put(("record", rec, name, "下载中", str(local_dir)))
                                else:
                                    self.events.put(("record_add", rec, name, "下载中", str(local_dir)))
                                try:
                                    saved_name, state = self._download_one(service, creds, child, local_dir, None, rec)
                                    self.events.put(("record", rec, saved_name, state, str(local_dir)))
                                    if state.startswith("已完成"): self.events.put(("log", f"已保存：{local_dir / saved_name}"))
                                except Exception as exc:
                                    self.events.put(("record", rec, name, "失败", str(local_dir)))
                                    self.events.put(("log", f"下载失败，跳过 {name}：{exc}"))
                        if found == 0 and not self.stop_event.is_set():
                            self.events.put(("record", root_record, root_name, "文件夹为空", str(root_dir)))
                except Exception as exc:
                    self.events.put(("record", root_record, current_name, "失败", current_path))
                    self.events.put(("log", f"任务失败，跳过并继续下一项 {current_name}：{exc}"))
                self.finished_items += 1
                self.events.put(("progress", round(self.finished_items * 100 / self.total_items)))
            self.events.put(("stopped",) if self.stop_event.is_set() else ("done",))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait(); kind = event[0]
                if kind == "log": self._write(event[1])
                elif kind == "status": self.status.configure(text=event[1])
                elif kind == "progress": self.progress.configure(value=event[1])
                elif kind == "record":
                    item = self.record_item_map.get(event[1], event[1]); self._update_record(item, event[2], event[3], event[4])
                elif kind == "record_add":
                    seq = len(self.record_tree.get_children()) + 1; item = self._add_record(seq, event[2], event[3], event[4]); self.record_item_map[event[1]] = item
                elif kind == "done":
                    self.running = False; self.start_btn.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    self.status.configure(text="全部任务处理完成！请检查保存路径。"); messagebox.showinfo("完成", "批量下载任务已全部执行完毕！")
                elif kind == "stopped":
                    self.running = False; self.start_btn.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    self.status.configure(text="下载已停止；已完成的文件和部分文件已保留。")
                elif kind == "error":
                    self.running = False; self.start_btn.configure(state="normal"); self.stop_btn.configure(state="disabled")
                    self.status.configure(text="运行遇到错误"); self._write("错误：" + event[1]); messagebox.showerror("下载错误", event[1])
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
