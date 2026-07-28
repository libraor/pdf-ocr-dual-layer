r"""
Umi-OCR 批量转换非双层 PDF 为双层 PDF

作者：林尧 · 浙江泽大律师事务所 高级合伙人 · linyao@foxmail.com

用法：
  1. 确保 Umi-OCR 已启动且 HTTP 服务已开启（默认 http://localhost:1224）
  2. 运行：python convert_pdfs_to_dual_layer.py [目标目录]

文件存储架构（详见 STORAGE_DESIGN.md）：
  主程序（坚果云同步）：当前脚本所在目录
  临时文件/日志/进度/备份：本地专用目录（不参与云同步）
    Windows: %LOCALAPPDATA%\pdf-ocr-dual-layer\
    Linux/Mac: ~/.local/share/pdf-ocr-dual-layer/
    可通过环境变量 PDF_OCR_WORK_DIR 覆盖

流程：
  PyMuPDF 本地检测 -> 有文本   -> 跳过（已是双层 PDF）
  PyMuPDF 本地检测 -> 无文本   -> 备份原文件 -> Umi-OCR HTTP 转换 -> 覆盖原文件
"""

import configparser
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

try:
    import fitz  # PyMuPDF
    _HAS_FITZ = True
except ImportError:
    _HAS_FITZ = False

# 修复 Windows GBK 终端 emoji 输出问题
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

__version__ = "1.9.2"


# ============ 配置文件加载 ============
def _load_config_file() -> configparser.ConfigParser:
    """
    加载配置文件，按优先级查找首个存在的文件：
      1. 环境变量 PDF_OCR_CONFIG_FILE 指定的路径
      2. 脚本所在目录的 config.ini
    未找到则返回空 parser（使用代码默认值）
    """
    parser = configparser.ConfigParser()
    candidates: list[Path] = []

    env_path = os.environ.get("PDF_OCR_CONFIG_FILE")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path(__file__).parent / "config.ini")

    for path in candidates:
        if path.is_file():
            try:
                parser.read(path, encoding="utf-8")
                print(f"已加载配置文件: {path}")
                break
            except Exception as e:
                print(f"⚠ 加载配置文件 {path} 失败: {e}，使用默认值")
                parser = configparser.ConfigParser()
    return parser


_CONFIG_FILE = _load_config_file()


def _cfg_str(section: str, key: str, env_name: str, default: str) -> str:
    """读取字符串配置：环境变量 > 配置文件 > 默认值"""
    env_val = os.environ.get(env_name)
    if env_val is not None and env_val != "":
        return env_val
    if _CONFIG_FILE.has_option(section, key):
        val = _CONFIG_FILE.get(section, key).strip()
        if val != "":
            return val
    return default


def _cfg_bool(section: str, key: str, env_name: str, default: bool) -> bool:
    """读取布尔配置"""
    s = _cfg_str(section, key, env_name, "true" if default else "false")
    return s.lower() in ("1", "true", "yes", "on")


def _cfg_int(section: str, key: str, env_name: str, default: int) -> int:
    """读取整数配置"""
    return int(_cfg_str(section, key, env_name, str(default)))


def _cfg_float(section: str, key: str, env_name: str, default: float) -> float:
    """读取浮点配置"""
    return float(_cfg_str(section, key, env_name, str(default)))


# ============ 配置 ============
class Config:
    """集中配置项。优先级：环境变量 > config.ini > 默认值"""

    # Umi-OCR 端口探测列表（端口可能因占用而漂移）
    TRY_PORTS = [int(p.strip()) for p in _cfg_str(
        "server", "try_ports", "PDF_OCR_PORTS",
        "1224,1225,1226,1227,1228,1229,1230,1241"
    ).split(",") if p.strip()]

    # HTTP 请求超时（秒）- 上传/下载/普通请求
    REQUEST_TIMEOUT = _cfg_int("http", "request_timeout", "PDF_OCR_TIMEOUT", 30)

    # 轮询专用超时（秒）- OCR 处理大文件时响应慢，需要更长
    POLL_TIMEOUT = _cfg_int("http", "poll_timeout", "PDF_OCR_POLL_TIMEOUT", 60)

    # 下载专用超时（秒）- Umi-OCR 生成下载文件可能耗时，特别是多页 PDF
    # 用于：POST 获取下载链接 + GET 下载文件
    DOWNLOAD_TIMEOUT = _cfg_int("http", "download_timeout", "PDF_OCR_DOWNLOAD_TIMEOUT", 300)

    # 上传专用超时（秒）- 大 PDF（>100MB）上传可能超过默认 REQUEST_TIMEOUT
    # 未配置时取 DOWNLOAD_TIMEOUT（足以覆盖大文件场景）
    UPLOAD_TIMEOUT = _cfg_int("http", "upload_timeout", "PDF_OCR_UPLOAD_TIMEOUT",
                               max(30, int(_cfg_str("http", "download_timeout", "PDF_OCR_DOWNLOAD_TIMEOUT", "300"))))

    # 轮询间隔（秒）
    POLL_INTERVAL = _cfg_int("http", "poll_interval", "PDF_OCR_POLL_INTERVAL", 2)

    # 单个 PDF 最大处理时间（秒）
    MAX_POLL_TIME = _cfg_int("http", "max_poll_time", "PDF_OCR_MAX_TIME", 600)

    # 轮询进度提示间隔（秒）
    PROGRESS_NOTIFY_INTERVAL = _cfg_int("http", "progress_notify_interval", "", 60)

    # 下载重试次数
    DOWNLOAD_RETRIES = _cfg_int("http", "download_retries", "", 5)

    # 文本层检测：有文本页数 / 抽样页数 ≥ 此阈值才判定为双层
    TEXT_LAYER_RATIO = _cfg_float("behavior", "text_layer_ratio", "", 0.5)

    # 是否备份原文件（覆盖前备份到本地专用目录）
    BACKUP_ORIGINAL = _cfg_bool("behavior", "backup_original", "PDF_OCR_BACKUP", True)

    # 转换后是否验证新 PDF 有效性（页数、文本层等）
    VERIFY_ON_SUCCESS = _cfg_bool("behavior", "verify_on_success", "PDF_OCR_VERIFY", True)

    # 验证通过后是否清理备份（节省硬盘空间；关闭则保留备份用于回滚）
    CLEANUP_BACKUP_ON_SUCCESS = _cfg_bool(
        "behavior", "cleanup_backup_on_success", "PDF_OCR_CLEANUP_BACKUP", True
    )

    # ============ OCR 识别参数（传递给 Umi-OCR API） ============
    # 内容提取模式：mixed(混合,默认) / fullPage(整页强制OCR) / ocrOnly(仅OCR图片)
    # 注意：textOnly 仅用于内部文本层检测，不可作为转换模式
    OCR_EXTRACTION_MODE = _cfg_str("ocr", "extraction_mode", "PDF_OCR_EXTRACTION_MODE", "mixed")

    # 语言/模型库（详见 config.ini 注释）
    OCR_LANGUAGE = _cfg_str("ocr", "language", "PDF_OCR_LANGUAGE", "models/config_chinese.txt")

    # 纠正文本方向（识别倾斜或倒置的文本，可能降低识别速度）
    OCR_CLS = _cfg_bool("ocr", "cls", "PDF_OCR_CLS", False)

    # 限制图像边长（边长大于此值的图片会被压缩；越大越精确但越慢）
    OCR_LIMIT_SIDE_LEN = _cfg_int("ocr", "limit_side_len", "PDF_OCR_LIMIT_SIDE_LEN", 960)

    # 排版解析方案（详见 config.ini 注释）
    OCR_PARSER = _cfg_str("ocr", "parser", "PDF_OCR_PARSER", "multi_para")

    # ============ 文件过滤 ============
    # 文件名排除模式（不区分大小写，支持通配符 * ?，逗号分隔）
    # 匹配文件名的 PDF 会被标记为 excluded，永久跳过不 OCR
    # 示例：*银行流水*,*流水*
    EXCLUDE_PATTERNS = [
        p.strip() for p in _cfg_str("filter", "exclude_patterns", "PDF_OCR_EXCLUDE", "").split(",")
        if p.strip()
    ]

    # ============ 并发控制 ============
    # OCR 并发任务数：同时处理的文件数
    # Umi-OCR 内部支持任务队列，并发上传可让 OCR 引擎持续工作
    # 推荐：2-4（太高会占用大量内存和 CPU）
    # 1 = 串行（与旧版行为一致）
    MAX_CONCURRENT_OCR = _cfg_int("behavior", "max_concurrent_ocr", "PDF_OCR_MAX_CONCURRENT", 3)

    # 本地工作目录环境变量名
    WORK_DIR_ENV = "PDF_OCR_WORK_DIR"


def build_ocr_options(extraction_mode: Optional[str] = None) -> dict:
    """构建 Umi-OCR API 选项字典

    Args:
        extraction_mode: 提取模式覆盖；None 则使用 Config.OCR_EXTRACTION_MODE
    """
    return {
        "doc.extractionMode": extraction_mode or Config.OCR_EXTRACTION_MODE,
        "ocr.language": Config.OCR_LANGUAGE,
        "ocr.cls": Config.OCR_CLS,
        "ocr.limit_side_len": Config.OCR_LIMIT_SIDE_LEN,
        "tbpu.parser": Config.OCR_PARSER,
    }


# ============ 本地工作目录管理 ============
_WORK_DIR: Optional[Path] = None


def get_work_dir() -> Path:
    """获取本地工作目录（缓存/日志/进度/备份），不参与云同步"""
    global _WORK_DIR
    if _WORK_DIR:
        return _WORK_DIR

    # 优先级：环境变量 PDF_OCR_WORK_DIR > config.ini [storage] work_dir > 系统默认
    work_dir_str = _cfg_str("storage", "work_dir", Config.WORK_DIR_ENV, "")
    if work_dir_str:
        work_dir = Path(work_dir_str)
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", str(Path.home()))
        work_dir = Path(base) / "pdf-ocr-dual-layer"
    else:
        work_dir = Path.home() / ".local" / "share" / "pdf-ocr-dual-layer"

    # 创建子目录
    for sub in ["cache", "logs", "progress", "backup"]:
        (work_dir / sub).mkdir(parents=True, exist_ok=True)

    _WORK_DIR = work_dir
    return work_dir


def target_hash(target_dir: str) -> str:
    """根据目标目录绝对路径生成 8 位 hash，用于隔离不同目录的进度/备份"""
    return hashlib.md5(os.path.abspath(target_dir).encode("utf-8")).hexdigest()[:8]


# ============ 日志 ============
def setup_logging(target_dir: str) -> Path:
    """配置日志，同时输出到控制台和文件。
    logging 模块本身线程安全，并发模式下不会出现 print 交错问题。
    """
    log_dir = get_work_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"convert_{ts}_{target_hash(target_dir)}.log"

    logger = logging.getLogger("pdf_ocr")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # 文件日志：DEBUG 级别，详细格式
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    # 控制台日志：INFO 级别，简洁格式（仅消息体）
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(ch)

    # 线程安全：logging 模块内部有锁，并发调用不会交错
    logger.propagate = False

    return log_file


def log() -> logging.Logger:
    return logging.getLogger("pdf_ocr")


# ============ Umi-OCR 探测 ============
UMI_OCR_BASE: Optional[str] = None


def detect_umi_ocr() -> bool:
    """自动探测 Umi-OCR HTTP 服务地址"""
    global UMI_OCR_BASE
    if UMI_OCR_BASE:
        return True
    for port in Config.TRY_PORTS:
        try:
            url = f"http://localhost:{port}"
            r = requests.get(f"{url}/api/doc/get_options", timeout=3)
            if r.status_code == 200:
                UMI_OCR_BASE = url
                log().info(f"已连接 Umi-OCR: {url}")
                return True
        except Exception:
            continue
    return False


# ============ PDF 查找 ============
def find_pdfs(root_dir: str) -> list[Path]:
    """递归查找所有 PDF 文件（Windows 文件系统大小写不敏感，仅匹配 *.pdf）"""
    root = Path(root_dir)
    pdfs = list(root.rglob("*.pdf"))
    return sorted(set(pdfs))


def is_excluded(pdf_path: Path) -> tuple[bool, str]:
    """
    检查文件是否匹配排除规则
    返回 (是否排除, 匹配的模式)
    """
    if not Config.EXCLUDE_PATTERNS:
        return False, ""
    name_lower = pdf_path.name.lower()
    for pattern in Config.EXCLUDE_PATTERNS:
        if fnmatch.fnmatch(name_lower, pattern.lower()):
            return True, pattern
    return False, ""


# ============ Umi-OCR 任务操作 ============
# 超长文件名会导致 Umi-OCR 下载 URL 超过 HTTP 限制（中文 URL 编码后单字变 9 字符）
_UPLOAD_NAME_MAX_URL_LEN = 200  # URL 编码后的最大长度


def _short_upload_name(original_name: str) -> str:
    """如果文件名 URL 编码后过长或含特殊字符，用 hash 生成短名"""
    url_encoded = urllib.parse.quote(original_name, safe="")
    # 含 # ? & 等 URL 保留字符也会导致 Umi-OCR 下载链被截断
    has_special = any(c in original_name for c in "#?&%=+")
    if len(url_encoded) <= _UPLOAD_NAME_MAX_URL_LEN and not has_special:
        return original_name
    name_hash = hashlib.md5(original_name.encode("utf-8")).hexdigest()[:8]
    short = f"ocr_{name_hash}.pdf"
    reason = f"URL长度 {len(url_encoded)} > {_UPLOAD_NAME_MAX_URL_LEN}" if len(url_encoded) > _UPLOAD_NAME_MAX_URL_LEN else "含特殊字符"
    log().info(f"文件名截短: {original_name[:60]}... -> {short} ({reason})")
    return short


def upload_pdf(pdf_path: Path, options: dict) -> Optional[str]:
    """上传 PDF 到 Umi-OCR，返回任务 ID。超长文件名自动截短。"""
    url = f"{UMI_OCR_BASE}/api/doc/upload"
    upload_name = _short_upload_name(pdf_path.name)
    try:
        with open(pdf_path, "rb") as f:
            files = {"file": (upload_name, f, "application/pdf")}
            data = {"json": json.dumps(options)}
            # 大 PDF 上传可能超过 REQUEST_TIMEOUT，使用 UPLOAD_TIMEOUT
            resp = requests.post(url, files=files, data=data, timeout=Config.UPLOAD_TIMEOUT)
        result = resp.json()
        if result.get("code") == 100:
            return result["data"]
        else:
            log().warning(f"上传失败: {result.get('data', '未知错误')}")
            return None
    except PermissionError:
        log().error(f"文件被占用或无读取权限: {pdf_path}")
        return None
    except Exception as e:
        log().error(f"上传异常: {e}", exc_info=True)
        return None


def poll_result(task_id: str, with_data: bool = True,
                custom_timeout: Optional[int] = None) -> Optional[dict]:
    """轮询任务状态直到完成，返回结果。custom_timeout 覆盖默认 MAX_POLL_TIME。"""
    url = f"{UMI_OCR_BASE}/api/doc/result"
    start_time = time.time()
    last_notify = start_time
    max_wait = custom_timeout if custom_timeout else Config.MAX_POLL_TIME

    while time.time() - start_time < max_wait:
        try:
            payload = {"id": task_id, "is_data": with_data, "format": "text"}
            resp = requests.post(url, json=payload, timeout=Config.POLL_TIMEOUT)
            result = resp.json()

            code = result.get("code")
            is_done = result.get("is_done", False)
            elapsed = int(time.time() - start_time)

            if code == 100 and is_done:
                return result
            elif code == 100 and not is_done:
                # 部分完成，继续等待
                pass
            elif code == 102:  # 失败
                return {"code": 102, "data": result.get("data", "任务失败")}

            # 进度提示（logging 线程安全，并发下不会交错）
            if time.time() - last_notify >= Config.PROGRESS_NOTIFY_INTERVAL:
                log().info(f"⏳ 已等待 {elapsed}s...")
                last_notify = time.time()

            time.sleep(Config.POLL_INTERVAL)
        except Exception as e:
            log().warning(f"轮询异常: {e}")
            time.sleep(Config.POLL_INTERVAL)

    return {"code": -1, "data": "超时"}


def download_result(task_id: str, file_types: list[str], output_path: Path) -> bool:
    """下载识别结果文件，失败时指数退避重试。临时文件用 try/finally 清理。

    临时文件放在目标文件同目录，确保 Windows 上 replace 是同卷原子操作
    （Windows 不支持跨卷 rename，会抛 WinError 17）。
    """
    url = f"{UMI_OCR_BASE}/api/doc/download"
    payload = {"id": task_id, "file_types": file_types, "ignore_blank": False}

    # 临时文件与目标文件同目录，确保同卷原子替换
    # 文件名加 . 前缀（隐藏），避免在目标目录可见
    tmp_path = output_path.parent / f".{output_path.stem}.{task_id}.tmp.pdf"

    try:
        for attempt in range(Config.DOWNLOAD_RETRIES):
            try:
                # POST 获取下载链接（Umi-OCR 生成文件可能耗时，用 DOWNLOAD_TIMEOUT）
                resp = requests.post(url, json=payload, timeout=Config.DOWNLOAD_TIMEOUT)
                result = resp.json()

                if result.get("code") == 100:
                    download_url = result["data"]
                    # GET 下载文件（同样用 DOWNLOAD_TIMEOUT）
                    dl_resp = requests.get(download_url, timeout=Config.DOWNLOAD_TIMEOUT)
                    dl_resp.raise_for_status()
                    tmp_path.write_bytes(dl_resp.content)
                    # 同卷原子替换（Windows 下跨卷会失败）
                    tmp_path.replace(output_path)
                    return True
                else:
                    # Umi-OCR 返回错误码，统一用指数退避
                    err_msg = f"code={result.get('code')}, data={result.get('data', '')}"
                    if attempt < Config.DOWNLOAD_RETRIES - 1:
                        wait = 3 * (2 ** attempt)
                        log().warning(f"下载第 {attempt+1} 次失败（{err_msg}），{wait}s 后重试")
                        time.sleep(wait)
                    else:
                        log().error(f"下载最终失败（{err_msg}）")
            except Exception as e:
                if attempt < Config.DOWNLOAD_RETRIES - 1:
                    wait = 3 * (2 ** attempt)
                    log().warning(f"下载第 {attempt+1} 次异常，{wait}s 后重试: {e}")
                    time.sleep(wait)
                else:
                    log().error(f"下载最终失败: {e}")
    finally:
        # 清理残留临时文件
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass

    return False


def clear_task(task_id: str) -> None:
    """清理任务，失败时记录日志（不静默）"""
    url = f"{UMI_OCR_BASE}/api/doc/clear/{task_id}"
    try:
        requests.get(url, timeout=Config.REQUEST_TIMEOUT)
    except Exception as e:
        log().warning(f"清理任务 {task_id} 失败: {e}")


# ============ 文本层检测 ============
def has_text_layer(pdf_path: Path) -> Optional[bool]:
    """
    检测 PDF 是否有文本层
    使用 PyMuPDF 本地检测，无需 HTTP 请求
    返回 True=有文本层, False=无, None=检测失败

    改进：采用比例阈值，避免只有封面有文字的扫描 PDF 被误判
    """
    if not _HAS_FITZ:
        return _has_text_layer_http(pdf_path)

    try:
        doc = fitz.open(str(pdf_path))
        try:
            page_count = doc.page_count
            if page_count == 0:
                return True

            # 抽样：前3页 + 中间1页 + 最后1页
            sample_indexes = [0]
            if page_count > 2:
                sample_indexes.append(1)
            if page_count > 3:
                sample_indexes.append(2)
            if page_count > 4:
                sample_indexes.append(page_count // 2)
            if page_count > 5:
                sample_indexes.append(page_count - 1)
            sample_indexes = sorted(set(sample_indexes))

            text_pages = 0
            for i in sample_indexes:
                page = doc[i]
                text = page.get_text()
                if text and text.strip():
                    text_pages += 1

            ratio = text_pages / len(sample_indexes)
            return ratio >= Config.TEXT_LAYER_RATIO
        finally:
            doc.close()
    except PermissionError:
        log().error(f"文件被占用: {pdf_path}")
        return None
    except Exception as e:
        log().warning(f"PyMuPDF 检测失败: {e}，回退到 HTTP 方式")
        return _has_text_layer_http(pdf_path)


def _has_text_layer_http(pdf_path: Path) -> Optional[bool]:
    """回退：使用 HTTP textOnly 方式检测"""
    task_id = upload_pdf(pdf_path, {"doc.extractionMode": "textOnly"})
    if not task_id:
        return None

    result = poll_result(task_id, with_data=True)
    if not result or result.get("code") != 100:
        clear_task(task_id)
        return None

    text = result.get("data", "")
    has_text = bool(text and text.strip())

    clear_task(task_id)
    return has_text


# ============ 备份与验证 ============
def backup_original(pdf_path: Path, target_dir: str) -> Optional[Path]:
    """备份原 PDF 到本地专用目录（不参与云同步），返回备份路径；未启用或失败时返回 None"""
    if not Config.BACKUP_ORIGINAL:
        return None

    backup_dir = get_work_dir() / "backup" / target_hash(target_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    # 清理该文件的历史备份，避免重试时堆积（<stem>.bak.pdf 和 <stem>.<ts>.bak.pdf）
    stem = pdf_path.stem
    for old in backup_dir.glob(f"{stem}*.bak.pdf"):
        try:
            old.unlink()
            log().info(f"清理旧备份: {old.name}")
        except Exception:
            pass

    # 创建新备份（无需时间戳，因为旧备份已清理）
    backup_path = backup_dir / f"{stem}.bak.pdf"

    # 文件占用时自动等待重试（最多 3 次，每次间隔递增）
    max_retries = 3
    for attempt in range(max_retries):
        try:
            shutil.copy2(pdf_path, backup_path)
            log().info(f"已备份: {pdf_path.name} -> {backup_path}")
            return backup_path
        except PermissionError as e:
            if attempt < max_retries - 1:
                wait_sec = (attempt + 1) * 3
                log().warning(f"备份文件被占用，{wait_sec}s 后重试 ({attempt+1}/{max_retries}): {pdf_path.name}")
                time.sleep(wait_sec)
            else:
                log().warning(f"备份最终失败（文件持续被占用）: {e}")
                return None
        except Exception as e:
            log().warning(f"备份失败: {e}")
            return None
    return None


def get_page_count(pdf_path: Path) -> int:
    """获取 PDF 页数，失败返回 -1"""
    if not _HAS_FITZ:
        return -1
    try:
        doc = fitz.open(str(pdf_path))
        try:
            return doc.page_count
        finally:
            doc.close()
    except Exception as e:
        log().warning(f"获取页数失败: {e}")
        return -1


def verify_converted_pdf(pdf_path: Path, original_page_count: int) -> bool:
    """
    验证转换后的 PDF 是否有效：
      - 文件存在且非空（>100 字节）
      - 能被 PyMuPDF 正常打开
      - 页数与原文件一致（原页数已知时）
      - 至少一页有文本层（双层 PDF 核心特征）
    """
    if not pdf_path.exists():
        log().warning("验证失败: 文件不存在")
        return False

    size = pdf_path.stat().st_size
    if size < 100:
        log().warning(f"验证失败: 文件过小 ({size} bytes)")
        return False

    if not _HAS_FITZ:
        log().info("无 PyMuPDF，跳过深度验证（仅基本大小检查通过）")
        return True

    try:
        doc = fitz.open(str(pdf_path))
        try:
            page_count = doc.page_count
            if page_count == 0:
                log().warning("验证失败: 页数为 0")
                return False
            if original_page_count > 0 and page_count != original_page_count:
                log().warning(f"验证失败: 页数不匹配 (原 {original_page_count}, 新 {page_count})")
                return False
            # 抽样检查文本层：前 3 页中至少 1 页有文本
            sample_count = min(3, page_count)
            text_pages = 0
            for i in range(sample_count):
                if doc[i].get_text().strip():
                    text_pages += 1
            if text_pages == 0:
                log().warning("验证失败: 抽样页均无文本层")
                return False
            log().info(f"验证通过: {page_count} 页, {text_pages}/{sample_count} 抽样页有文本")
            return True
        finally:
            doc.close()
    except Exception as e:
        log().warning(f"验证失败: {e}")
        return False


def restore_from_backup(pdf_path: Path, backup_path: Path) -> bool:
    """从备份恢复原文件（验证失败时调用）。
    文件占用时自动等待重试（3 次，间隔 3/6/9s），避免原文件停留在损坏状态。
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            shutil.copy2(backup_path, pdf_path)
            log().info(f"已从备份恢复: {backup_path.name}")
            return True
        except PermissionError as e:
            if attempt < max_retries - 1:
                wait_sec = (attempt + 1) * 3
                log().warning(f"恢复文件被占用，{wait_sec}s 后重试 ({attempt+1}/{max_retries}): {pdf_path.name}")
                time.sleep(wait_sec)
            else:
                log().error(f"❌ 恢复最终失败（文件持续被占用）: {e}，原文件可能损坏: {pdf_path}，请手动从备份恢复: {backup_path}")
                return False
        except Exception as e:
            log().error(f"❌ 恢复失败: {e}，请手动从备份恢复: {backup_path}")
            return False
    return False


def cleanup_backup(backup_path: Path) -> None:
    """删除备份文件（验证通过后调用，节省硬盘空间）"""
    try:
        backup_path.unlink()
        log().info(f"已清理备份: {backup_path.name}")
    except Exception as e:
        log().warning(f"清理备份失败: {e}")


# ============ OCR 主流程 ============
def ocr_to_dual_layer(pdf_path: Path, target_dir: str,
                      custom_poll_timeout: Optional[int] = None) -> tuple[bool, str]:
    """
    OCR 转双层 PDF 完整流程。
    返回 (成功, 失败原因)。
    custom_poll_timeout: 覆盖默认的 MAX_POLL_TIME（用于超时重试）
    """
    # 1. 记录原页数
    original_page_count = get_page_count(pdf_path)

    # 2. 备份
    backup_path = backup_original(pdf_path, target_dir)
    if Config.BACKUP_ORIGINAL and backup_path is None:
        return False, "备份失败（文件占用）"

    # 3. OCR 上传
    task_id = upload_pdf(pdf_path, build_ocr_options())
    if not task_id:
        return False, "上传失败"

    try:
        # 4. 轮询
        result = poll_result(task_id, with_data=False,
                             custom_timeout=custom_poll_timeout)
        if result is None:
            return False, "轮询无响应"
        if result.get("code") == -1:
            return False, "超时"
        if result.get("code") != 100:
            return False, f"Umi-OCR 错误: {result.get('data', '未知')}"

        # 5. 下载
        success = download_result(task_id, ["pdfLayered"], pdf_path)
        if not success:
            return False, "下载失败"
    finally:
        # 无论成功失败都清理 Umi-OCR 端任务，避免堆积
        clear_task(task_id)

    # 6. 验证
    if Config.VERIFY_ON_SUCCESS:
        if not verify_converted_pdf(pdf_path, original_page_count):
            log().error("⚠ 验证失败，从备份恢复原文件")
            if backup_path and backup_path.exists():
                restore_from_backup(pdf_path, backup_path)
            return False, "验证失败（无文本层）"
        log().info("  ✅ 验证通过")

    # 7. 验证通过，清理备份（节省空间）
    if Config.CLEANUP_BACKUP_ON_SUCCESS and backup_path and backup_path.exists():
        cleanup_backup(backup_path)

    return True, ""


# ============ 进度管理 ============
PROGRESS_FILENAME = "pdf_conversion_progress.json"


def get_progress_path(target_dir: str) -> Path:
    """获取进度文件路径：本地专用目录下，按目标目录 hash 隔离"""
    progress_dir = get_work_dir() / "progress"
    progress_dir.mkdir(parents=True, exist_ok=True)
    return progress_dir / f"{target_hash(target_dir)}_{PROGRESS_FILENAME}"


def load_progress(progress_path: Path) -> dict:
    """加载已有的进度记录"""
    if progress_path.exists():
        try:
            with open(progress_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log().warning(f"加载进度失败，重新开始: {e}")
    return {
        "records": [],
        "stats": {"converted": 0, "already_dual": 0, "failed": 0, "skipped": 0, "excluded": 0},
        "target_dir": "",
        "version": __version__,
    }


def save_progress(progress_path: Path, progress: dict) -> None:
    """保存当前进度（先写临时文件再原子替换）"""
    tmp_path = progress_path.with_suffix(".json.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
        tmp_path.replace(progress_path)
    except Exception as e:
        log().error(f"保存进度失败: {e}")


# ============ 状态机辅助函数 ============
# 文件状态：
#   already_dual - 已检测为双层，跳过 OCR
#   need_ocr     - 已检测需要 OCR，未转换或中断
#   converted    - OCR 转换成功
#   failed       - OCR 转换失败
#   skipped      - 检测失败跳过
#   excluded     - 匹配排除规则，永久跳过

# 进度文件写入锁（并发模式下保护进度文件和 records 列表）
_progress_lock = threading.Lock()


def _make_progress(records: list, stats: dict, target_dir: str) -> dict:
    """构建进度字典"""
    return {
        "records": records,
        "stats": stats,
        "target_dir": target_dir,
        "version": __version__,
    }


def _upsert_record(records: list, record_map: dict, rel_path: str,
                   status: str, detail: str) -> dict:
    """更新或插入记录，返回该记录对象"""
    if rel_path in record_map:
        r = record_map[rel_path]
        r["status"] = status
        r["detail"] = detail
        return r
    r = {"file": rel_path, "status": status, "detail": detail}
    records.append(r)
    record_map[rel_path] = r
    return r


def _recompute_stats(stats: dict, records: list) -> None:
    """从 records 重新计算 stats，避免重试时重复计数"""
    stats["converted"] = sum(1 for r in records if r["status"] == "converted")
    stats["already_dual"] = sum(1 for r in records if r["status"] == "already_dual")
    stats["failed"] = sum(1 for r in records if r["status"] == "failed")
    stats["skipped"] = sum(1 for r in records if r["status"] == "skipped")
    stats["excluded"] = sum(1 for r in records if r["status"] == "excluded")


def _save_progress_locked(progress_path: Path, records: list, stats: dict,
                          target_dir: str) -> None:
    """线程安全地保存进度文件"""
    with _progress_lock:
        save_progress(progress_path, _make_progress(records, stats, target_dir))


def _do_ocr_stage(pdf_path: Path, rel_path: str, target_dir: str,
                  record: dict, records: list, stats: dict,
                  progress_path: Path,
                  custom_timeout: Optional[int] = None) -> bool:
    """执行 OCR 阶段并更新记录（线程安全，返回是否成功）"""
    log().info(f"  🔄 OCR 转换中: {rel_path} ...")
    success, reason = ocr_to_dual_layer(pdf_path, target_dir,
                                         custom_poll_timeout=custom_timeout)
    with _progress_lock:
        if success:
            log().info(f"  ✅ 转换成功: {rel_path}")
            record["status"] = "converted"
            record["detail"] = "OCR 成功"
        else:
            log().warning(f"  ❌ 转换失败: {rel_path} ({reason})")
            record["status"] = "failed"
            record["detail"] = reason
        _recompute_stats(stats, records)
        save_progress(progress_path, _make_progress(records, stats, target_dir))
    return success


def _print_status_summary(records: list) -> None:
    """打印历史进度统计"""
    counts: dict = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    log().info("  历史进度:")
    log().info(f"    ✅ 已是双层: {counts.get('already_dual', 0)}")
    log().info(f"    🔄 待 OCR:   {counts.get('need_ocr', 0)}")
    log().info(f"    ✅ 已转换:   {counts.get('converted', 0)}")
    log().info(f"    ❌ 失败:     {counts.get('failed', 0)}")
    log().info(f"    ⏭ 跳过:     {counts.get('skipped', 0)}")
    log().info(f"    🚫 排除:     {counts.get('excluded', 0)}")


# ============ 报告生成 ============
def write_report(report_path: Path, stats: dict, records: list,
                 start_time: datetime, end_time: datetime,
                 duration, interrupted: bool = False) -> None:
    """生成 Markdown 格式的转换报告"""
    lines = []
    lines.append("# PDF 双层转换报告\n")
    lines.append(f"**处理时间**: {start_time.strftime('%Y-%m-%d %H:%M:%S')} - {end_time.strftime('%H:%M:%S')}")
    lines.append(f"**总耗时**: {duration.seconds // 60} 分 {duration.seconds % 60} 秒")
    if interrupted:
        lines.append("\n⚠️ **任务被中断，进度已保存。下次运行将从断点继续。**")
    lines.append("---\n")
    lines.append("## 📊 汇总统计\n")
    lines.append("| 类别 | 数量 |")
    lines.append("|------|------|")
    lines.append(f"| 总计 | {stats.get('total', 0)} |")
    lines.append(f"| ✅ 已是双层（跳过） | {stats['already_dual']} |")
    need_ocr_count = sum(1 for r in records if r["status"] == "need_ocr")
    lines.append(f"| 🔄 待 OCR（中断未完成） | {need_ocr_count} |")
    lines.append(f"| ✅ 成功转换 | {stats['converted']} |")
    lines.append(f"| ❌ 转换失败 | {stats['failed']} |")
    lines.append(f"| ⏭ 跳过 | {stats['skipped']} |")
    lines.append(f"| 🚫 排除规则 | {stats.get('excluded', 0)} |")

    processed = stats["converted"] + stats["failed"]
    if processed > 0:
        success_rate = stats["converted"] / processed * 100
        lines.append(f"\n**转换成功率**: {success_rate:.1f}% ({stats['converted']}/{processed})")

    lines.append("\n---\n")
    lines.append("## 📋 详细结果\n")
    lines.append("| 文件 | 状态 | 说明 |")
    lines.append("|------|------|------|")

    status_emoji = {
        "already_dual": "✅ 已是双层",
        "need_ocr": "🔄 待 OCR",
        "converted": "✅ 已转换",
        "failed": "❌ 失败",
        "skipped": "⏭ 跳过",
        "excluded": "🚫 排除",
    }

    for r in records:
        status_text = status_emoji.get(r["status"], r["status"])
        lines.append(f"| {r['file']} | {status_text} | {r['detail']} |")

    lines.append("\n---\n")
    lines.append(f"*报告由 convert_pdfs_to_dual_layer.py v{__version__} 自动生成*")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ============ 主入口 ============
def main() -> None:
    # 支持命令行参数指定目录
    if len(sys.argv) > 1:
        target_dir = os.path.abspath(sys.argv[1])
    else:
        target_dir = os.path.dirname(os.path.abspath(__file__))

    if not os.path.isdir(target_dir):
        print(f"❌ 目标目录不存在: {target_dir}")
        sys.exit(1)

    # 初始化日志（先初始化，后续所有输出都走 logging，线程安全）
    log_file = setup_logging(target_dir)
    log().info(f"=== 开始运行 v{__version__} ===")
    log().info(f"目标目录: {target_dir}")
    log().info(f"工作目录: {get_work_dir()}")
    log().info(f"日志文件: {log_file}")
    log().info(f"并发度: {Config.MAX_CONCURRENT_OCR}")

    # 自动探测 Umi-OCR（日志已初始化，可被记录）
    if not detect_umi_ocr():
        log().error("❌ 无法连接 Umi-OCR，请确保 Umi-OCR 已启动且 HTTP 服务已开启")
        sys.exit(1)

    if Config.MAX_CONCURRENT_OCR > 1:
        log().info(f"⚡ 并发模式: 最多 {Config.MAX_CONCURRENT_OCR} 个任务同时 OCR")
    else:
        log().info("串行模式（max_concurrent_ocr=1）")

    pdfs = find_pdfs(target_dir)
    if not pdfs:
        log().info("未找到任何 PDF 文件。")
        return

    # 进度文件（按目标目录 hash 隔离）
    progress_path = get_progress_path(target_dir)
    progress = load_progress(progress_path)
    records = progress["records"]
    stats = progress["stats"]
    stats["total"] = len(pdfs)
    record_map = {r["file"]: r for r in records}

    log().info(f"找到 {len(pdfs)} 个 PDF 文件")
    if records:
        _print_status_summary(records)
    log().info("")

    interrupted = False
    interrupted_file: Optional[str] = None  # 主线程正在处理的文件（检测阶段）
    start_time = datetime.now()

    # 并发模式初始化
    use_concurrent = Config.MAX_CONCURRENT_OCR > 1
    pending_futures: set = set()
    executor: Optional[ThreadPoolExecutor] = None
    if use_concurrent:
        executor = ThreadPoolExecutor(max_workers=Config.MAX_CONCURRENT_OCR)

    def _submit_ocr(pdf_path: Path, rel_path: str, record: dict) -> None:
        """提交 OCR 任务：并发模式提交到线程池，串行模式直接执行"""
        if executor:
            future = executor.submit(_do_ocr_stage, pdf_path, rel_path,
                                     target_dir, record, records, stats,
                                     progress_path)
            pending_futures.add(future)
        else:
            _do_ocr_stage(pdf_path, rel_path, target_dir, record,
                          records, stats, progress_path)

    def _wait_for_slot() -> None:
        """控制并发度：待办任务过多时等待部分完成"""
        if not executor:
            return
        max_pending = Config.MAX_CONCURRENT_OCR * 2
        while len(pending_futures) >= max_pending:
            done, _ = wait(pending_futures, return_when=FIRST_COMPLETED)
            pending_futures.difference_update(done)
            for f in done:
                try:
                    f.result()
                except Exception as e:
                    log().error(f"OCR 任务异常: {e}")

    def _update_status(rel_path: str, status: str, detail: str) -> None:
        """线程安全地更新文件状态并保存进度"""
        with _progress_lock:
            _upsert_record(records, record_map, rel_path, status, detail)
            _recompute_stats(stats, records)
            save_progress(progress_path, _make_progress(records, stats, target_dir))

    try:
        for i, pdf_path in enumerate(pdfs, 1):
            _wait_for_slot()  # 控制并发度
            rel_path = str(pdf_path.relative_to(target_dir))
            record = record_map.get(rel_path)

            # ===== 状态机：根据已有记录决定流程 =====
            if record is not None:
                status = record["status"]
                if status == "already_dual":
                    log().info(f"[{i}/{len(pdfs)}] {rel_path}  ⏭ 已是双层，跳过")
                    continue
                if status == "converted":
                    log().info(f"[{i}/{len(pdfs)}] {rel_path}  ⏭ 已转换，跳过")
                    continue
                if status == "excluded":
                    # 排除规则匹配的文件，永久跳过
                    continue

            # ===== 排除规则检查（优先于 need_ocr/failed/skipped/无记录） =====
            # 已是双层/已转换的文件保留原状态，不排除
            excluded, pattern = is_excluded(pdf_path)
            if excluded:
                log().info(f"[{i}/{len(pdfs)}] {rel_path}  ⏭ 匹配排除规则 '{pattern}'，跳过")
                _update_status(rel_path, "excluded", f"匹配排除规则: {pattern}")
                continue

            if record is not None:
                status = record["status"]
                if status == "need_ocr":
                    # 已检测需 OCR，直接进入 OCR 阶段（跳过检测）
                    log().info(f"[{i}/{len(pdfs)}] {rel_path}  🔄 待 OCR（已检测，跳过文本层检测）")
                    _submit_ocr(pdf_path, rel_path, record)
                    continue
                if status == "failed":
                    # 上次失败，延迟到最终批量重试（避免阻塞新文件处理）
                    log().info(f"[{i}/{len(pdfs)}] {rel_path}  ⏸ 之前失败（稍后批量重试）")
                    continue
                # status == "skipped"：上次检测失败，重新走完整流程
                log().info(f"[{i}/{len(pdfs)}] {rel_path}  🔍 重新检测（上次跳过）")
            else:
                log().info(f"[{i}/{len(pdfs)}] {rel_path}")

            interrupted_file = rel_path

            # 阶段 1：检测文本层（主线程串行，毫秒级）
            log().info(f"  🔍 检测文本层: {rel_path} ...")
            has_text = has_text_layer(pdf_path)

            if has_text is None:
                log().warning(f"  检测失败，跳过: {rel_path}")
                _update_status(rel_path, "skipped", "检测失败")
                interrupted_file = None
                continue

            if has_text:
                log().info(f"  ✅ 已是双层 PDF，跳过: {rel_path}")
                _update_status(rel_path, "already_dual", "已有文本层")
                interrupted_file = None
                continue

            log().info(f"  ❌ 无文本层，开始 OCR: {rel_path}")

            # 关键优化：立即保存 need_ocr 状态，OCR 中断后下次跳过检测
            _update_status(rel_path, "need_ocr", "无文本层，待 OCR")
            interrupted_file = None  # 已保存 need_ocr，下次直接 OCR

            # 阶段 2：OCR 转双层 PDF（提交到线程池或直接执行）
            _submit_ocr(pdf_path, rel_path, record_map[rel_path])

        # 等待所有 OCR 任务完成
        if pending_futures:
            log().info(f"⏳ 等待 {len(pending_futures)} 个 OCR 任务完成...")
            for f in as_completed(pending_futures):
                try:
                    f.result()
                except Exception as e:
                    log().error(f"OCR 任务异常: {e}")

        # ===== 最终批量重试失败文件（按原因分类处理） =====
        failed_records = [(p, str(p.relative_to(target_dir)), record_map[str(p.relative_to(target_dir))])
                          for p in pdfs
                          if str(p.relative_to(target_dir)) in record_map
                          and record_map[str(p.relative_to(target_dir))]["status"] == "failed"]
        if failed_records:
            # 超时 → 用 2x max_poll_time 重试
            timeout_files = [(p, r, rec) for p, r, rec in failed_records
                             if rec["detail"].startswith("超时")]
            normal_failed = [(p, r, rec) for p, r, rec in failed_records
                             if not rec["detail"].startswith("超时")]

            if timeout_files:
                timeout_extra = Config.MAX_POLL_TIME
                total_timeout = Config.MAX_POLL_TIME + timeout_extra
                log().info(f"🔄 超时重试 {len(timeout_files)} 个文件（超时从 {Config.MAX_POLL_TIME}s 增至 {total_timeout}s）...")
                for pdf_path, rel_path, record in timeout_files:
                    log().info(f"  ⏰ 超时重试: {rel_path}")
                    if executor:
                        future = executor.submit(_do_ocr_stage, pdf_path, rel_path,
                                                 target_dir, record, records, stats,
                                                 progress_path, total_timeout)
                        pending_futures.add(future)
                    else:
                        _do_ocr_stage(pdf_path, rel_path, target_dir, record,
                                      records, stats, progress_path, total_timeout)

            # 其他失败 → 普通重试（备份占用已由 backup_original 内部等待重试）
            if normal_failed:
                log().info(f"🔄 普通重试 {len(normal_failed)} 个失败文件...")
                for pdf_path, rel_path, record in normal_failed:
                    log().info(f"  🔄 重试: {rel_path}")
                    _submit_ocr(pdf_path, rel_path, record)

            if pending_futures:
                log().info(f"⏳ 等待 {len(pending_futures)} 个重试任务完成...")
                for f in as_completed(pending_futures):
                    try:
                        f.result()
                    except Exception as e:
                        log().error(f"重试任务异常: {e}")

    except KeyboardInterrupt:
        interrupted = True
        log().warning("⏸ 用户中断！")

        # 处理主线程正在检测的文件
        if interrupted_file:
            with _progress_lock:
                r = record_map.get(interrupted_file)
                if r and r["status"] == "need_ocr":
                    log().info(f"  中断时正在 OCR: {interrupted_file}（已保存待 OCR 状态，下次直接重试）")
                else:
                    log().info(f"  中断时正在检测: {interrupted_file}（下次将重新检测）")
                    records[:] = [rr for rr in records if rr["file"] != interrupted_file]
                    record_map.pop(interrupted_file, None)
                    _recompute_stats(stats, records)

        # 并发模式：取消未开始的任务，等待运行中的完成
        if executor and pending_futures:
            running = [f for f in pending_futures if f.running()]
            pending_count = [f for f in pending_futures if not f.running() and not f.done()]
            for f in pending_count:
                f.cancel()
            if running:
                log().info(f"  等待 {len(running)} 个运行中的 OCR 任务完成（最多 120 秒）...")
                wait(running, timeout=120)
                for f in running:
                    try:
                        f.result(timeout=1)
                    except Exception:
                        pass

        _save_progress_locked(progress_path, records, stats, target_dir)
        log().info("  进度已保存。")

    finally:
        if executor:
            executor.shutdown(wait=False)

    # 生成报告（写入目标目录，便于用户查看；这是产出物，非临时文件）
    end_time = datetime.now()
    duration = end_time - start_time
    report_path = Path(target_dir) / "pdf_conversion_report.md"
    write_report(report_path, stats, records, start_time, end_time, duration, interrupted)

    log().info(f"报告已生成: {report_path}")
    log().info(f"日志文件: {log_file}")
    log().info(f"=== 运行结束，耗时 {duration} ===")


if __name__ == "__main__":
    main()
