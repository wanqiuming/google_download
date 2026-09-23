import os
import re
import html
import time
import threading
from urllib.parse import urlparse, parse_qs, unquote, urljoin

import requests
from bs4 import BeautifulSoup

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ============================================================
# 配置
# ============================================================

CHUNK_SIZE = 1024 * 1024
TIMEOUT = 60
MAX_RETRIES = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

VIDEO_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/x-m4v": ".m4v",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/mpeg": ".mpeg",
    "video/3gpp": ".3gp",
    "video/3gpp2": ".3g2",
    "application/mp4": ".mp4",
}

MAGIC_HEADERS = {
    b"\x00\x00\x00": "mp4/mov",
    b"\x1a\x45\xdf\xa3": "webm/mkv",
    b"RIFF": "avi/webm",
    b"\x00\x00\x01\xba": "mpeg",
    b"\x00\x00\x01\xb3": "mpeg",
}


# ============================================================
# 基础工具
# ============================================================

def extract_file_id(url):
    if not url:
        return None

    url = url.strip()

    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)

    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if "id" in query and query["id"]:
            return query["id"][0]
    except Exception:
        pass

    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)

    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", url):
        return url

    return None


def format_size(size):
    if size is None:
        return "未知"

    try:
        size = int(size)
    except Exception:
        return "未知"

    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)

    for unit in units:
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024

    return f"{value:.2f} PB"


def format_speed(speed):
    if not speed or speed <= 0:
        return "--"
    return format_size(speed) + "/s"


def sanitize_filename(filename):
    if not filename:
        return None

    filename = html.unescape(str(filename))
    filename = filename.replace("\x00", "")

    # Windows 禁止字符
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename)

    # 控制字符
    filename = re.sub(r"[\x00-\x1F]", "_", filename)

    # 文件名末尾不能是空格或点
    filename = filename.rstrip(" .")

    if not filename:
        return None

    # Windows 保留名称
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }

    stem = os.path.splitext(filename)[0]
    if stem.upper() in reserved:
        filename = "_" + filename

    return filename


def fix_mojibake(text):
    """
    修复常见的：
    UTF-8 字节被错误按 Latin-1/CP1252 解码的问题。

    例如：
    è´ºç¶
    -> 贺然
    """

    if not text:
        return text

    # 常见 UTF-8 乱码特征
    markers = (
        "Ã", "Â", "â", "ð",
        "è", "é", "ê", "ë",
        "ì", "í", "î", "ï",
        "æ", "ç", "å", "ã",
        "¤", " "
    )

    if not any(c in text for c in markers):
        return text

    candidates = []

    try:
        candidates.append(
            text.encode("latin1").decode("utf-8")
        )
    except Exception:
        pass

    try:
        candidates.append(
            text.encode("cp1252").decode("utf-8")
        )
    except Exception:
        pass

    if not candidates:
        return text

    # 选择中文/非 ASCII 更合理的结果
    def score(value):
        chinese = sum(
            1 for c in value
            if "\u4e00" <= c <= "\u9fff"
        )
        replacement = value.count(" ")
        mojibake = sum(
            value.count(c)
            for c in ("Ã", "Â", "â", "è", "ç", "å", "æ")
        )
        return chinese * 10 - replacement * 20 - mojibake * 5

    best = max(candidates, key=score)

    if score(best) > score(text):
        return best

    return text


def clean_drive_filename(filename):
    if not filename:
        return None

    filename = html.unescape(str(filename))
    filename = fix_mojibake(filename)

    # 删除 Google Drive 页面标题附加文字
    filename = re.sub(
        r"\s*-\s*Google\s+(?:云端硬盘|云端硬盘|Drive)\s*$",
        "",
        filename,
        flags=re.IGNORECASE,
    )

    filename = filename.strip()

    return sanitize_filename(filename)


def filename_from_content_disposition(value):
    if not value:
        return None

    # RFC 5987
    match = re.search(
        r"filename\*\s*=\s*(?:UTF-8'')?([^;]+)",
        value,
        re.IGNORECASE,
    )

    if match:
        value = match.group(1).strip().strip('"')
        try:
            value = unquote(value)
        except Exception:
            pass

        value = fix_mojibake(value)
        value = sanitize_filename(value)

        if value:
            return value

    match = re.search(
        r'filename\s*=\s*"([^"]+)"',
        value,
        re.IGNORECASE,
    )

    if match:
        value = fix_mojibake(match.group(1))
        return sanitize_filename(value)

    match = re.search(
        r"filename\s*=\s*([^;]+)",
        value,
        re.IGNORECASE,
    )

    if match:
        value = fix_mojibake(match.group(1).strip())
        return sanitize_filename(value)

    return None


def extension_from_content_type(content_type):
    if not content_type:
        return None

    content_type = content_type.split(";")[0].strip().lower()

    return VIDEO_EXTENSIONS.get(content_type)


def add_extension_if_missing(filename, content_type=None):
    if not filename:
        return filename

    # 已经存在扩展名
    if os.path.splitext(filename)[1]:
        return filename

    extension = extension_from_content_type(content_type)

    if extension:
        return filename + extension

    return filename


def get_unique_filepath(output_dir, filename):
    """
    如果目标文件已经存在，则自动在文件名后添加数字：
    video.mp4
    video (1).mp4
    video (2).mp4
    ...
    """
    filename = sanitize_filename(filename) or "video"

    stem, ext = os.path.splitext(filename)
    candidate = os.path.join(output_dir, filename)

    number = 1
    while os.path.exists(candidate) or os.path.exists(candidate + ".part"):
        candidate_name = f"{stem} ({number}){ext}"
        candidate = os.path.join(output_dir, candidate_name)
        number += 1

    return candidate


# ============================================================
# Drive 页面文件信息
# ============================================================

def get_drive_page_info(session, file_id):
    """
    只负责读取 Drive 原始文件名。

    优先级：
    1. title
    2. og:title
    3. twitter:title
    4. JSON name
    5. JSON fileName

    Content-Disposition 不在这里参与。
    """

    url = f"https://drive.google.com/file/d/{file_id}/view"

    response = session.get(
        url,
        headers=HEADERS,
        timeout=TIMEOUT,
        allow_redirects=True,
    )

    if not response.ok:
        raise Exception(
            f"无法打开 Google Drive 页面：HTTP {response.status_code}"
        )

    # 不直接使用 response.text，避免编码猜测导致中文乱码
    raw = response.content

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")

    soup = BeautifulSoup(text, "html.parser")

    # ① title
    title = soup.find("title")
    if title:
        filename = clean_drive_filename(
            title.get_text(strip=True)
        )
        if filename:
            return filename

    # ② og:title
    meta = soup.find(
        "meta",
        attrs={"property": "og:title"},
    )
    if meta:
        filename = clean_drive_filename(
            meta.get("content")
        )
        if filename:
            return filename

    # ③ twitter:title
    meta = soup.find(
        "meta",
        attrs={"name": "twitter:title"},
    )
    if meta:
        filename = clean_drive_filename(
            meta.get("content")
        )
        if filename:
            return filename

    # ④ 页面 JSON
    patterns = [
        r'"name"\s*:\s*"([^"]+)"',
        r'\\"name\\"\s*:\s*\\"([^"]+)\\"',
        r'"fileName"\s*:\s*"([^"]+)"',
        r'\\"fileName\\"\s*:\s*\\"([^"]+)\\"',
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if not match:
            continue

        value = (
            match.group(1)
            .replace('\\"', '"')
            .replace("\\/", "/")
        )

        filename = clean_drive_filename(value)

        if filename:
            return filename

    return None


# ============================================================
# Google 下载确认页面
# ============================================================

def is_html_response(response):
    content_type = response.headers.get(
        "Content-Type", ""
    ).lower()

    return (
        "text/html" in content_type
        or "application/xhtml" in content_type
    )


def response_looks_like_file(response):
    if is_html_response(response):
        return False

    content_type = response.headers.get(
        "Content-Type", ""
    ).lower()

    if content_type.startswith("video/"):
        return True

    if "application/octet-stream" in content_type:
        return True

    if "application/mp4" in content_type:
        return True

    disposition = response.headers.get(
        "Content-Disposition", ""
    )

    if disposition and "attachment" in disposition.lower():
        return True

    return False


def parse_download_confirmation(
    session,
    response,
    file_id,
):
    try:
        text = response.text
    except Exception:
        return None

    soup = BeautifulSoup(
        text,
        "html.parser",
    )

    # ① form
    for form in soup.find_all("form"):
        action = form.get("action")

        if not action:
            continue

        action = html.unescape(action)
        action = urljoin(response.url, action)

        params = {}

        for tag in form.find_all("input"):
            name = tag.get("name")
            if name:
                params[name] = tag.get("value", "")

        params.setdefault("id", file_id)

        try:
            r = session.get(
                action,
                params=params,
                headers=HEADERS,
                stream=True,
                timeout=TIMEOUT,
                allow_redirects=True,
            )

            if response_looks_like_file(r):
                return r

            r.close()

        except Exception:
            pass

    # ② href
    for a in soup.find_all("a", href=True):
        href = html.unescape(a.get("href"))

        if not href:
            continue

        if (
            "download" not in href.lower()
            and "usercontent" not in href.lower()
        ):
            continue

        href = urljoin(response.url, href)

        try:
            r = session.get(
                href,
                headers=HEADERS,
                stream=True,
                timeout=TIMEOUT,
                allow_redirects=True,
            )

            if response_looks_like_file(r):
                return r

            r.close()

        except Exception:
            pass

    # ③ HTML 内直接 URL
    patterns = [
        r'https://drive\.usercontent\.google\.com/download[^"\']+',
        r'https:\\/\\/drive\.usercontent\.google\.com\\/download[^"\']+',
    ]

    for pattern in patterns:
        for match in re.findall(
            pattern,
            text,
            re.IGNORECASE,
        ):
            download_url = (
                html.unescape(match)
                .replace("\\/", "/")
            )

            try:
                r = session.get(
                    download_url,
                    headers=HEADERS,
                    stream=True,
                    timeout=TIMEOUT,
                    allow_redirects=True,
                )

                if response_looks_like_file(r):
                    return r

                r.close()

            except Exception:
                pass

    return None


def get_real_download_response(
    session,
    file_id,
):
    url = "https://drive.google.com/uc"

    params = {
        "export": "download",
        "id": file_id,
    }

    response = session.get(
        url,
        params=params,
        headers=HEADERS,
        stream=True,
        timeout=TIMEOUT,
        allow_redirects=True,
    )

    if response_looks_like_file(response):
        return response

    if not is_html_response(response):
        response.close()
        raise Exception(
            "Google 返回了无法识别的响应。"
        )

    real_response = parse_download_confirmation(
        session,
        response,
        file_id,
    )

    response.close()

    if real_response:
        if response_looks_like_file(real_response):
            return real_response

        real_response.close()

    raise Exception(
        "Google 返回了下载确认页面，"
        "但没有成功解析出最终文件下载地址。"
    )


# ============================================================
# 文件大小
# ============================================================

def get_total_size(response):
    content_range = response.headers.get(
        "Content-Range"
    )

    if content_range:
        match = re.search(
            r"/(\d+)$",
            content_range,
        )
        if match:
            try:
                return int(match.group(1))
            except Exception:
                pass

    content_length = response.headers.get(
        "Content-Length"
    )

    if content_length:
        try:
            return int(content_length)
        except Exception:
            pass

    return None


# ============================================================
# Range
# ============================================================

def request_with_range(
    session,
    download_url,
    start,
):
    headers = HEADERS.copy()
    headers["Range"] = f"bytes={start}-"

    response = session.get(
        download_url,
        headers=headers,
        stream=True,
        timeout=TIMEOUT,
        allow_redirects=True,
    )

    if response.status_code != 206:
        response.close()
        return None

    content_range = response.headers.get(
        "Content-Range",
        "",
    )

    match = re.match(
        r"bytes\s+(\d+)-(\d+)/(\d+|\*)",
        content_range,
    )

    if not match:
        response.close()
        return None

    actual_start = int(match.group(1))

    if actual_start != start:
        response.close()
        return None

    return response


# ============================================================
# 下载器
# ============================================================

class Downloader:

    def __init__(self, app):
        self.app = app
        self.stop_requested = False

    def stop(self):
        self.stop_requested = True

    def download_file(
        self,
        file_id,
        output_dir,
        index,
        total,
        custom_name=None,
    ):
        for attempt in range(1, MAX_RETRIES + 1):

            if self.stop_requested:
                return False

            try:
                return self.download_once(
                    file_id,
                    output_dir,
                    index,
                    total,
                    custom_name,
                )

            except Exception as e:

                self.app.log(
                    f"[{index}/{total}] "
                    f"第 {attempt} 次失败：{e}"
                )

                if attempt < MAX_RETRIES:
                    self.app.log(
                        f"[{index}/{total}] 准备重试..."
                    )

                    for _ in range(3):
                        if self.stop_requested:
                            return False
                        time.sleep(1)

        self.app.log(
            f"[{index}/{total}] 下载最终失败。"
        )

        return False

    def download_once(
        self,
        file_id,
        output_dir,
        index,
        total,
        custom_name=None,
    ):
        session = requests.Session()
        session.headers.update(HEADERS)

        response = None

        try:
            # ------------------------------------------------
            # ① 先从 Drive 页面获取原始文件名
            # ------------------------------------------------
            filename = get_drive_page_info(
                session,
                file_id,
            )

            self.app.log(
                f"[{index}/{total}] "
                f"Drive 原始文件名：{filename or '(未读取到)'}"
            )

            # ------------------------------------------------
            # ② 获取真实下载响应
            # ------------------------------------------------
            response = get_real_download_response(
                session,
                file_id,
            )

            content_type = response.headers.get(
                "Content-Type",
                "",
            )

            # ------------------------------------------------
            # ③ 文件名兜底
            #
            # 只有 Drive 页面完全读取不到文件名时，
            # 才允许使用 Content-Disposition。
            #
            # 绝不覆盖已经正确读取的 Drive 文件名。
            # ------------------------------------------------
            if not filename:
                filename = filename_from_content_disposition(
                    response.headers.get(
                        "Content-Disposition"
                    )
                )

            # ------------------------------------------------
            # ④ 文件名
            # 如果勾选“允许改名”，则优先使用这一行链接后面的名称
            # ------------------------------------------------
            if custom_name:
                filename = sanitize_filename(custom_name)

            if not filename:
                filename = file_id

            # 自动补视频扩展名
            filename = add_extension_if_missing(
                filename,
                content_type,
            )

            filename = sanitize_filename(filename)

            if not filename:
                filename = file_id

            # 如果重名，自动变成：
            # 文件名.mp4
            # 文件名 (1).mp4
            # 文件名 (2).mp4
            final_path = get_unique_filepath(
                output_dir,
                filename,
            )

            filename = os.path.basename(final_path)
            part_path = final_path + ".part"

            self.app.log(
                f"[{index}/{total}] "
                f"最终保存文件名：{filename}"
            )

            # ------------------------------------------------
            # ⑤ 获取文件大小
            # ------------------------------------------------
            total_size = get_total_size(response)

            # ------------------------------------------------
            # ⑥ 已存在正式文件
            # ------------------------------------------------
            if os.path.exists(final_path):

                existing_final_size = os.path.getsize(
                    final_path
                )

                if (
                    total_size is not None
                    and existing_final_size == total_size
                ):
                    response.close()
                    response = None

                    self.app.log(
                        f"[{index}/{total}] "
                        f"已存在，跳过：{filename}"
                    )

                    self.app.file_finished(
                        index,
                        total,
                    )

                    return True

            # ------------------------------------------------
            # ⑦ .part 断点
            # ------------------------------------------------
            existing_size = 0

            if os.path.exists(part_path):
                existing_size = os.path.getsize(
                    part_path
                )

            if existing_size > 0:

                download_url = response.url

                response.close()
                response = None

                self.app.log(
                    f"[{index}/{total}] "
                    f"发现断点：{format_size(existing_size)}"
                )

                range_response = request_with_range(
                    session,
                    download_url,
                    existing_size,
                )

                if range_response:

                    response = range_response

                    total_size = get_total_size(
                        response
                    )

                    self.app.log(
                        f"[{index}/{total}] "
                        f"服务器确认断点续传。"
                    )

                else:

                    self.app.log(
                        f"[{index}/{total}] "
                        f"服务器未正确返回 206，"
                        f"删除旧 .part，从头下载。"
                    )

                    try:
                        os.remove(part_path)
                    except Exception:
                        pass

                    existing_size = 0

                    response = get_real_download_response(
                        session,
                        file_id,
                    )

                    total_size = get_total_size(
                        response
                    )

            # ------------------------------------------------
            # ⑧ 更新界面
            # ------------------------------------------------
            self.app.update_file_info(
                index,
                total,
                filename,
                total_size,
                existing_size,
            )

            # ------------------------------------------------
            # ⑨ 下载
            # ------------------------------------------------
            mode = "ab" if existing_size > 0 else "wb"

            downloaded = existing_size
            last_time = time.time()
            last_bytes = downloaded

            with open(part_path, mode) as f:

                for chunk in response.iter_content(
                    chunk_size=CHUNK_SIZE
                ):
                    if self.stop_requested:

                        f.flush()

                        self.app.log(
                            f"[{index}/{total}] "
                            f"已停止：{filename}"
                        )

                        return False

                    if not chunk:
                        continue

                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()

                    if now - last_time >= 0.2:

                        elapsed = now - last_time

                        speed = (
                            downloaded - last_bytes
                        ) / elapsed

                        last_time = now
                        last_bytes = downloaded

                        self.app.update_progress(
                            index,
                            total,
                            filename,
                            downloaded,
                            total_size,
                            speed,
                        )

            response.close()
            response = None

            # ------------------------------------------------
            # ⑩ 最终大小校验
            # ------------------------------------------------
            actual_size = os.path.getsize(
                part_path
            )

            if total_size is not None:
                if actual_size != total_size:
                    raise Exception(
                        "下载完成但文件大小不一致："
                        f"{format_size(actual_size)} / "
                        f"{format_size(total_size)}"
                    )

            # ------------------------------------------------
            # ⑪ 防止 HTML 页面误保存
            # ------------------------------------------------
            with open(
                part_path,
                "rb"
            ) as f:
                first_bytes = f.read(32)

            if (
                first_bytes.startswith(b"<html")
                or first_bytes.startswith(b"<!DOCTYPE")
                or b"<html" in first_bytes.lower()
            ):
                raise Exception(
                    "下载结果是 HTML 页面，不是视频文件。"
                )

            # ------------------------------------------------
            # ⑫ 完成
            # ------------------------------------------------
            if os.path.exists(final_path):
                os.remove(final_path)

            os.replace(
                part_path,
                final_path,
            )

            self.app.update_progress(
                index,
                total,
                filename,
                actual_size,
                total_size,
                0,
            )

            self.app.log(
                f"[{index}/{total}] "
                f"✓ 下载完成：{filename}"
            )

            self.app.file_finished(
                index,
                total,
            )

            return True

        finally:

            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

            session.close()


# ============================================================
# GUI
# ============================================================

class App:

    def __init__(self, root):

        self.root = root

        self.root.title(
            "Google Drive 视频批量下载器"
        )

        self.root.geometry(
            "1050x780"
        )

        self.root.minsize(
            900,
            650
        )

        self.downloader = None
        self.downloading = False
        self.total_files = 0
        self.finished_files = 0

        self.setup_dark_theme()
        self.create_ui()

    # --------------------------------------------------------
    # 深色主题
    # --------------------------------------------------------

    def setup_dark_theme(self):

        style = ttk.Style()

        try:
            style.theme_use("clam")
        except Exception:
            pass

        bg = "#171717"
        panel = "#222222"
        fg = "#F0F0F0"
        entry_bg = "#2B2B2B"
        select_bg = "#444444"

        self.root.configure(bg=bg)

        style.configure(
            ".",
            background=bg,
            foreground=fg,
            font=("Microsoft YaHei", 10),
        )

        style.configure(
            "TFrame",
            background=bg,
        )

        style.configure(
            "TLabelframe",
            background=bg,
            foreground=fg,
        )

        style.configure(
            "TLabelframe.Label",
            background=bg,
            foreground=fg,
        )

        style.configure(
            "TLabel",
            background=bg,
            foreground=fg,
        )

        style.configure(
            "TButton",
            background=panel,
            foreground=fg,
            padding=(12, 6),
        )

        style.map(
            "TButton",
            background=[
                ("active", select_bg),
                ("pressed", "#555555"),
            ],
            foreground=[
                ("disabled", "#777777"),
            ],
        )

        style.configure(
            "TEntry",
            fieldbackground=entry_bg,
            foreground=fg,
            insertcolor=fg,
        )

        style.configure(
            "Treeview",
            background="#222222",
            foreground="#F0F0F0",
            fieldbackground="#222222",
        )

        style.map(
            "Treeview",
            background=[
                ("selected", "#FFFFFF"),
            ],
            foreground=[
                ("selected", "#000000"),
            ],
        )

        style.configure(
            "Horizontal.TProgressbar",
            background="#4CAF50",
            troughcolor="#303030",
            bordercolor="#303030",
            lightcolor="#4CAF50",
            darkcolor="#4CAF50",
        )

    # --------------------------------------------------------
    # UI
    # --------------------------------------------------------

    def create_ui(self):

        main = ttk.Frame(
            self.root,
            padding=15,
        )

        main.pack(
            fill="both",
            expand=True,
        )

        title = ttk.Label(
            main,
            text="Google Drive 视频批量下载器",
            font=(
                "Microsoft YaHei",
                18,
                "bold",
            ),
        )

        title.pack(
            anchor="w",
            pady=(0, 10),
        )

        # ====================================================
        # 视频链接表格
        # ====================================================

        link_frame = ttk.LabelFrame(
            main,
            text="视频链接列表",
            padding=10,
        )

        link_frame.pack(
            fill="both",
            expand=True,
        )

        table_frame = ttk.Frame(link_frame)
        table_frame.pack(
            fill="both",
            expand=True,
        )

        self.link_tree = ttk.Treeview(
            table_frame,
            columns=("url", "name"),
            show="headings",
            selectmode="extended",
        )

        self.link_tree.heading(
            "url",
            text="链接",
        )

        self.link_tree.heading(
            "name",
            text="新文件名",
        )

        self.link_tree.column(
            "url",
            width=650,
            anchor="w",
        )

        self.link_tree.column(
            "name",
            width=280,
            anchor="w",
        )

        tree_scroll = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.link_tree.yview,
        )

        self.link_tree.configure(
            yscrollcommand=tree_scroll.set,
        )

        self.link_tree.pack(
            side="left",
            fill="both",
            expand=True,
        )

        tree_scroll.pack(
            side="right",
            fill="y",
        )

        ttk.Label(
            link_frame,
            text="双击单元格可以编辑；勾选“允许改名”后，第二列的新文件名才会生效。",
        ).pack(
            anchor="w",
            pady=(6, 4),
        )

        table_btn_frame = ttk.Frame(link_frame)
        table_btn_frame.pack(
            fill="x",
            pady=(0, 4),
        )

        ttk.Button(
            table_btn_frame,
            text="添加链接",
            command=self.add_link_row,
        ).pack(
            side="left",
            padx=(0, 5),
        )

        ttk.Button(
            table_btn_frame,
            text="删除选中",
            command=self.delete_selected_rows,
        ).pack(
            side="left",
            padx=5,
        )

        ttk.Button(
            table_btn_frame,
            text="清空列表",
            command=self.clear_link_rows,
        ).pack(
            side="left",
            padx=5,
        )

        self.link_tree.bind(
            "<Double-1>",
            self.edit_link_cell,
        )

        # 支持直接把剪贴板中的整列链接粘贴到“链接”列。
        # 一行一个链接；如果没有选中行，则从列表末尾继续添加。
        self.link_tree.bind(
            "<Control-v>",
            self.paste_links,
        )
        self.link_tree.bind(
            "<Control-V>",
            self.paste_links,
        )
        self.link_tree.bind(
            "<Shift-Insert>",
            self.paste_links,
        )

        self.rename_var = tk.BooleanVar(
            value=False,
        )

        ttk.Checkbutton(
            link_frame,
            text="允许改名",
            variable=self.rename_var,
        ).pack(
            anchor="w",
            pady=(2, 0),
        )

        ttk.Label(
            link_frame,
            text="未勾选：使用 Google Drive 原文件名；勾选：使用第二列填写的新文件名。",
        ).pack(
            anchor="w",
            pady=(2, 0),
        )

        # ====================================================
        # 下载目录
        # ====================================================

        folder_frame = ttk.Frame(main)

        folder_frame.pack(
            fill="x",
            pady=10,
        )

        ttk.Label(
            folder_frame,
            text="下载目录：",
        ).pack(side="left")

        self.folder_var = tk.StringVar(
            value=os.path.join(
                os.path.expanduser("~"),
                "Downloads",
            )
        )

        ttk.Entry(
            folder_frame,
            textvariable=self.folder_var,
        ).pack(
            side="left",
            fill="x",
            expand=True,
            padx=5,
        )

        ttk.Button(
            folder_frame,
            text="选择目录",
            command=self.choose_folder,
        ).pack(side="right")

        # ====================================================
        # 按钮
        # ====================================================

        button_frame = ttk.Frame(main)

        button_frame.pack(
            fill="x",
            pady=(0, 10),
        )

        self.start_button = ttk.Button(
            button_frame,
            text="开始下载",
            command=self.start_download,
        )

        self.start_button.pack(
            side="left",
            padx=(0, 5),
        )

        self.stop_button = ttk.Button(
            button_frame,
            text="停止",
            command=self.stop_download,
            state="disabled",
        )

        self.stop_button.pack(
            side="left",
            padx=5,
        )

        ttk.Button(
            button_frame,
            text="清空链接",
            command=self.clear_links,
        ).pack(
            side="left",
            padx=5,
        )

        # ====================================================
        # 当前文件
        # ====================================================

        info_frame = ttk.LabelFrame(
            main,
            text="当前下载",
            padding=10,
        )

        info_frame.pack(fill="x")

        self.current_file_var = tk.StringVar(
            value="等待下载"
        )

        ttk.Label(
            info_frame,
            textvariable=self.current_file_var,
            font=("Microsoft YaHei", 10),
        ).pack(anchor="w")

        self.size_var = tk.StringVar(
            value="文件大小：--"
        )

        ttk.Label(
            info_frame,
            textvariable=self.size_var,
        ).pack(
            anchor="w",
            pady=(4, 0),
        )

        self.speed_var = tk.StringVar(
            value="速度：--"
        )

        ttk.Label(
            info_frame,
            textvariable=self.speed_var,
        ).pack(anchor="w")

        # ====================================================
        # 当前文件进度
        # ====================================================

        self.file_progress = ttk.Progressbar(
            main,
            orient="horizontal",
            mode="determinate",
        )

        self.file_progress.pack(
            fill="x",
            pady=(8, 0),
        )

        self.file_progress_var = tk.StringVar(
            value="0%"
        )

        ttk.Label(
            main,
            textvariable=self.file_progress_var,
        ).pack(anchor="e")

        # ====================================================
        # 总进度
        # ====================================================

        self.total_progress = ttk.Progressbar(
            main,
            orient="horizontal",
            mode="determinate",
        )

        self.total_progress.pack(
            fill="x",
            pady=(5, 0),
        )

        self.total_progress_var = tk.StringVar(
            value="总进度：0 / 0"
        )

        ttk.Label(
            main,
            textvariable=self.total_progress_var,
        ).pack(anchor="e")

        # ====================================================
        # 日志
        # ====================================================

        log_frame = ttk.LabelFrame(
            main,
            text="下载日志",
            padding=5,
        )

        log_frame.pack(
            fill="both",
            expand=True,
            pady=(10, 0),
        )

        self.log_text = tk.Text(
            log_frame,
            height=10,
            font=("Consolas", 9),
            state="disabled",
            bg="#202020",
            fg="#D8D8D8",
            insertbackground="#FFFFFF",
            selectbackground="#444444",
            relief="flat",
        )

        self.log_text.pack(
            side="left",
            fill="both",
            expand=True,
        )

        log_scroll = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_text.yview,
        )

        log_scroll.pack(
            side="right",
            fill="y",
        )

        self.log_text.configure(
            yscrollcommand=log_scroll.set
        )

    # --------------------------------------------------------
    # 选择目录
    # --------------------------------------------------------

    def choose_folder(self):

        folder = filedialog.askdirectory()

        if folder:
            self.folder_var.set(folder)

    # --------------------------------------------------------
    # 清空链接
    # --------------------------------------------------------

    def clear_links(self):

        if self.downloading:
            return

        self.clear_link_rows()

    def log(self, text):

        def update():

            self.log_text.configure(
                state="normal"
            )

            self.log_text.insert(
                "end",
                text + "\n",
            )

            self.log_text.see("end")

            self.log_text.configure(
                state="disabled"
            )

        self.root.after(
            0,
            update,
        )

    # --------------------------------------------------------
    # 文件信息
    # --------------------------------------------------------

    def update_file_info(
        self,
        index,
        total,
        filename,
        total_size,
        downloaded,
    ):

        def update():

            self.current_file_var.set(
                f"[{index}/{total}] {filename}"
            )

            self.size_var.set(
                "文件大小：" +
                format_size(total_size)
            )

            if total_size:

                percent = (
                    downloaded /
                    total_size *
                    100
                )

                self.file_progress["value"] = min(
                    percent,
                    100,
                )

                self.file_progress_var.set(
                    f"{percent:.2f}%"
                )

        self.root.after(
            0,
            update,
        )

    # --------------------------------------------------------
    # 实时进度
    # --------------------------------------------------------

    def update_progress(
        self,
        index,
        total,
        filename,
        downloaded,
        total_size,
        speed,
    ):

        def update():

            self.current_file_var.set(
                f"[{index}/{total}] {filename}"
            )

            if total_size:

                percent = (
                    downloaded /
                    total_size *
                    100
                )

                self.file_progress["value"] = min(
                    percent,
                    100,
                )

                self.file_progress_var.set(
                    f"{percent:.2f}%    "
                    f"{format_size(downloaded)} / "
                    f"{format_size(total_size)}"
                )

                self.size_var.set(
                    "文件大小：" +
                    format_size(total_size)
                )

            else:

                self.file_progress_var.set(
                    format_size(downloaded)
                )

            self.speed_var.set(
                "速度：" +
                format_speed(speed)
            )

        self.root.after(
            0,
            update,
        )

    # --------------------------------------------------------
    # 文件完成
    # --------------------------------------------------------

    def file_finished(
        self,
        index,
        total,
    ):

        self.finished_files += 1

        def update():

            if self.total_files:

                percent = (
                    self.finished_files /
                    self.total_files *
                    100
                )

                self.total_progress["value"] = percent

            self.total_progress_var.set(
                f"总进度："
                f"{self.finished_files} / "
                f"{self.total_files}"
            )

        self.root.after(
            0,
            update,
        )

    # --------------------------------------------------------
    # 开始
    # --------------------------------------------------------

    def add_link_row(self):
        self.link_tree.insert("", "end", values=("", ""))

    def delete_selected_rows(self):
        for item in self.link_tree.selection():
            self.link_tree.delete(item)

    def clear_link_rows(self):
        for item in self.link_tree.get_children():
            self.link_tree.delete(item)

    def edit_link_cell(self, event):
        item = self.link_tree.identify_row(event.y)
        column = self.link_tree.identify_column(event.x)
        if not item or column not in ("#1", "#2"):
            return

        bbox = self.link_tree.bbox(item, column)
        if not bbox:
            return

        x, y, width, height = bbox
        values = list(self.link_tree.item(item, "values"))
        index = 0 if column == "#1" else 1

        # 不再使用 ttk.Entry，避免某些 Windows 主题下编辑框变成白底。
        # 直接使用 tk.Entry，并明确指定深色背景/浅色文字。
        entry = tk.Entry(
            self.link_tree,
            bg="#2B2B2B",
            fg="#F0F0F0",
            insertbackground="#FFFFFF",
            selectbackground="#444444",
            selectforeground="#FFFFFF",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground="#555555",
            highlightcolor="#666666",
            font=("Microsoft YaHei", 10),
        )
        entry.place(x=x, y=y, width=width, height=height)
        entry.insert(0, values[index])
        entry.select_range(0, tk.END)
        entry.focus_set()

        def save(event=None):
            if not entry.winfo_exists():
                return
            values[index] = entry.get().strip()
            self.link_tree.item(item, values=values)
            entry.destroy()

        def cancel(event=None):
            if entry.winfo_exists():
                entry.destroy()

        entry.bind("<Return>", save)
        entry.bind("<Escape>", cancel)
        entry.bind("<FocusOut>", save)

    def paste_links(self, event=None):
        """
        直接把剪贴板中的多行链接粘贴到“链接”列。
        例如：
            https://drive.google.com/file/d/AAA/view
            https://drive.google.com/file/d/BBB/view
            https://drive.google.com/file/d/CCC/view

        粘贴后会自动逐行创建/覆盖列表中的链接行。
        如果当前选中了某一行，则从该行开始粘贴；
        如果没有选中行，则从列表末尾追加。
        """
        try:
            clipboard = self.root.clipboard_get()
        except tk.TclError:
            return "break"

        if not clipboard or not clipboard.strip():
            return "break"

        # 兼容从 Excel / 表格复制整列时带来的 \t。
        # 每一行只取第一列作为链接，空行自动忽略。
        links = []
        for line in clipboard.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue

            first_cell = line.split("\t", 1)[0].strip()
            if first_cell:
                links.append(first_cell)

        if not links:
            return "break"

        children = list(self.link_tree.get_children())
        selection = self.link_tree.selection()

        if selection:
            # 以当前选中的第一行作为粘贴起点。
            try:
                start_index = children.index(selection[0])
            except ValueError:
                start_index = len(children)
        else:
            start_index = len(children)

        # 确保有足够的行。
        while len(self.link_tree.get_children()) < start_index + len(links):
            self.link_tree.insert("", "end", values=("", ""))

        children = list(self.link_tree.get_children())

        for offset, link in enumerate(links):
            item = children[start_index + offset]
            values = list(self.link_tree.item(item, "values"))

            if len(values) < 2:
                values = [link, ""]
            else:
                values[0] = link

            self.link_tree.item(item, values=values)

        # 选中刚粘贴的区域，方便用户继续操作。
        children = list(self.link_tree.get_children())
        pasted_items = children[start_index:start_index + len(links)]
        self.link_tree.selection_set(pasted_items)
        if pasted_items:
            self.link_tree.focus(pasted_items[0])
            self.link_tree.see(pasted_items[0])

        self.log(f"已粘贴 {len(links)} 个链接。")
        return "break"

    def start_download(self):

        if self.downloading:
            return

        download_items = []

        for item in self.link_tree.get_children():

            values = self.link_tree.item(
                item,
                "values",
            )

            url = (
                values[0].strip()
                if len(values) > 0
                else ""
            )

            custom_name = (
                values[1].strip()
                if len(values) > 1
                else ""
            )

            if not url:
                continue

            file_id = extract_file_id(url)

            if not file_id:
                self.log(
                    f"跳过无效链接：{url}"
                )
                continue

            if not self.rename_var.get():
                custom_name = None

            elif not custom_name:
                custom_name = None

            download_items.append(
                (
                    file_id,
                    custom_name,
                )
            )

        if not download_items:

            messagebox.showwarning(
                "提示",
                "请先添加有效的 Google Drive 视频链接。",
            )

            return

        output_dir = self.folder_var.get().strip()

        if not output_dir:

            messagebox.showwarning(
                "提示",
                "请选择下载目录。",
            )

            return

        os.makedirs(
            output_dir,
            exist_ok=True,
        )

        self.downloading = True
        self.total_files = len(download_items)
        self.finished_files = 0

        self.start_button.configure(
            state="disabled",
        )

        self.stop_button.configure(
            state="normal",
        )

        self.downloader = Downloader(
            self
        )

        thread = threading.Thread(
            target=self.download_thread,
            args=(
                download_items,
                output_dir,
            ),
            daemon=True,
        )

        thread.start()

    def download_thread(
        self,
        download_items,
        output_dir,
    ):

        total = len(download_items)

        for index, (file_id, custom_name) in enumerate(
            download_items,
            1,
        ):

            if self.downloader.stop_requested:
                break

            self.log(
                f"[{index}/{total}] "
                f"File ID：{file_id}"
            )

            if custom_name:
                self.log(
                    f"[{index}/{total}] "
                    f"自定义文件名：{custom_name}"
                )

            self.downloader.download_file(
                file_id,
                output_dir,
                index,
                total,
                custom_name,
            )

        self.root.after(
            0,
            self.download_finished,
        )

    # --------------------------------------------------------
    # 停止
    # --------------------------------------------------------

    def stop_download(self):

        if self.downloader:

            self.downloader.stop()

            self.log(
                "正在停止，当前 .part 文件会保留。"
            )

    # --------------------------------------------------------
    # 完成
    # --------------------------------------------------------

    def download_finished(self):

        self.downloading = False

        self.start_button.configure(
            state="normal"
        )

        self.stop_button.configure(
            state="disabled"
        )

        self.speed_var.set(
            "速度：--"
        )

        if (
            self.downloader
            and self.downloader.stop_requested
        ):

            self.current_file_var.set(
                "下载已停止"
            )

            self.log(
                "下载任务已停止。"
            )

        else:

            self.current_file_var.set(
                "全部任务处理完成"
            )

            self.log(
                "=========================================="
            )

            self.log(
                "全部任务处理完成。"
            )

            messagebox.showinfo(
                "完成",
                "下载任务处理完成。\n\n"
                f"已完成/跳过："
                f"{self.finished_files} / "
                f"{self.total_files}",
            )


# ============================================================
# 主程序
# ============================================================

def main():

    root = tk.Tk()

    try:
        root.tk.call(
            "tk",
            "scaling",
            1.2,
        )
    except Exception:
        pass

    App(root)

    root.mainloop()


if __name__ == "__main__":
    main()
