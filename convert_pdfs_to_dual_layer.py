r"""
Umi-OCR 批量转换非双层 PDF 为双层 PDF

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

import hashlib
import json
import logging
import os
import shutil
import sys
import time
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

__version__ = "1.1.0"


# ============ 配置 ============
class Config:
    """集中配置项，支持环境变量覆盖"""
    # Umi-OCR 端口探测列表（端口可能因占用而漂移）
    TRY_PORTS = [1224, 1225, 1226, 1227, 1228, 1229, 1230, 1241]

    # HTTP 请求超时（秒）— 上传/下载/普通请求
    REQUEST_TIMEOUT = int(os.environ.get("PDF_OCR_TIMEOUT", "30"))

    # 轮询专用超时（秒）— OCR 处理大文件时响应慢，需要更长
    POLL_TIMEOUT = int(os.environ.get("PDF_OCR_POLL_TIMEOUT", "60"))

    # 轮询间隔（秒）
    POLL_INTERVAL = int(os.environ.get("PDF_OCR_POLL_INTERVAL", "2"))

    # 单个 PDF 最大处理时间（秒）
    MAX_POLL_TIME = int(os.environ.get("PDF_OCR_MAX_TIME", "600"))

    # 轮询进度提示间隔（秒）
    PROGRESS_NOTIFY_INTERVAL = 60

    # 下载重试次数
    DOWNLOAD_RETRIES = 5

    # 文本层检测：有文本页数 / 抽样页数 ≥ 此阈值才判定为双层
    TEXT_LAYER_RATIO = 0.5

    # 是否备份原文件（覆盖前备份到本地专用目录）
    BACKUP_ORIGINAL = os.environ.get("PDF_OCR_BACKUP", "1") == "1"

    # 转换后是否验证新 PDF 有效性（页数、文本层等）
    VERIFY_ON_SUCCESS = os.environ.get("PDF_OCR_VERIFY", "1") == "1"

    # 验证通过后是否清理备份（节省硬盘空间；关闭则保留备份用于回滚）
    CLEANUP_BACKUP_ON_SUCCESS = os.environ.get("PDF_OCR_CLEANUP_BACKUP", "1") == "1"

    # 本地工作目录环境变量名
    WORK_DIR_ENV = "PDF_OCR_WORK_DIR"


# ============ 本地工作目录管理 ============
_WORK_DIR: Optional[Path] = None


def get_work_dir() -> Path:
    """获取本地工作目录（缓存/日志/进度/备份），不参与云同步"""
    global _WORK_DIR
    if _WORK_DIR:
        return _WORK_DIR

    env_val = os.environ.get(Config.WORK_DIR_ENV)
    if env_val:
        work_dir = Path(env_val)
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
    """配置日志，同时输出到控制台和文件"""
    log_dir = get_work_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"convert_{ts}_{target_hash(target_dir)}.log"

    logger = logging.getLogger("pdf_ocr")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # 文件日志：详细级别
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

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
                print(f"已连接 Umi-OCR: {url}")
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


# ============ Umi-OCR 任务操作 ============
def upload_pdf(pdf_path: Path, options: dict) -> Optional[str]:
    """上传 PDF 到 Umi-OCR，返回任务 ID"""
    url = f"{UMI_OCR_BASE}/api/doc/upload"
    try:
        with open(pdf_path, "rb") as f:
            files = {"file": (pdf_path.name, f, "application/pdf")}
            data = {"json": json.dumps(options)}
            resp = requests.post(url, files=files, data=data, timeout=Config.REQUEST_TIMEOUT)
        result = resp.json()
        if result.get("code") == 100:
            return result["data"]
        else:
            msg = f"上传失败: {result.get('data', '未知错误')}"
            print(f"  ⚠ {msg}")
            log().warning(msg)
            return None
    except PermissionError:
        msg = f"文件被占用或无读取权限: {pdf_path}"
        print(f"  ⚠ {msg}")
        log().error(msg)
        return None
    except Exception as e:
        msg = f"上传异常: {e}"
        print(f"  ⚠ {msg}")
        log().error(msg, exc_info=True)
        return None


def poll_result(task_id: str, with_data: bool = True) -> Optional[dict]:
    """轮询任务状态直到完成，返回结果。每 60 秒打印进度提示。"""
    url = f"{UMI_OCR_BASE}/api/doc/result"
    start_time = time.time()
    last_notify = start_time

    while time.time() - start_time < Config.MAX_POLL_TIME:
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

            # 进度提示
            if time.time() - last_notify >= Config.PROGRESS_NOTIFY_INTERVAL:
                print(f"⏳ 已等待 {elapsed}s...", end=" ", flush=True)
                last_notify = time.time()

            time.sleep(Config.POLL_INTERVAL)
        except Exception as e:
            log().warning(f"轮询异常: {e}")
            time.sleep(Config.POLL_INTERVAL)

    return {"code": -1, "data": "超时"}


def download_result(task_id: str, file_types: list[str], output_path: Path) -> bool:
    """下载识别结果文件，失败时指数退避重试。临时文件用 try/finally 清理。"""
    url = f"{UMI_OCR_BASE}/api/doc/download"
    payload = {"id": task_id, "file_types": file_types, "ignore_blank": False}

    cache_dir = get_work_dir() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_dir / f"{task_id}.tmp.pdf"

    try:
        for attempt in range(Config.DOWNLOAD_RETRIES):
            try:
                resp = requests.post(url, json=payload, timeout=Config.REQUEST_TIMEOUT)
                result = resp.json()

                if result.get("code") == 100:
                    download_url = result["data"]
                    dl_resp = requests.get(download_url, timeout=Config.REQUEST_TIMEOUT * 10)
                    dl_resp.raise_for_status()
                    tmp_path.write_bytes(dl_resp.content)
                    # 原子替换
                    tmp_path.replace(output_path)
                    return True
                else:
                    wait = 3 * (attempt + 1)
                    time.sleep(wait)
            except Exception as e:
                if attempt < Config.DOWNLOAD_RETRIES - 1:
                    wait = 3 * (2 ** attempt)
                    log().warning(f"下载第 {attempt+1} 次失败，{wait}s 后重试: {e}")
                    time.sleep(wait)
                else:
                    msg = f"下载最终失败: {e}"
                    print(f"  ⚠ {msg}")
                    log().error(msg)
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
        msg = f"文件被占用: {pdf_path}"
        print(f"  ⚠ {msg}")
        log().error(msg)
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

    # 命名规范：<原文件名>.bak.pdf，重名时附加时间戳
    backup_name = f"{pdf_path.stem}.bak.pdf"
    backup_path = backup_dir / backup_name
    if backup_path.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"{pdf_path.stem}.{ts}.bak.pdf"

    try:
        shutil.copy2(pdf_path, backup_path)
        log().info(f"已备份: {pdf_path.name} -> {backup_path}")
        return backup_path
    except Exception as e:
        log().warning(f"备份失败: {e}")
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
    """从备份恢复原文件（验证失败时调用）"""
    try:
        shutil.copy2(backup_path, pdf_path)
        log().info(f"已从备份恢复: {backup_path.name}")
        return True
    except Exception as e:
        log().error(f"恢复失败: {e}")
        return False


def cleanup_backup(backup_path: Path) -> None:
    """删除备份文件（验证通过后调用，节省硬盘空间）"""
    try:
        backup_path.unlink()
        log().info(f"已清理备份: {backup_path.name}")
    except Exception as e:
        log().warning(f"清理备份失败: {e}")


# ============ OCR 主流程 ============
def ocr_to_dual_layer(pdf_path: Path, target_dir: str) -> bool:
    """
    OCR 转双层 PDF 完整流程：
      1. 记录原页数（用于后续验证）
      2. 备份原文件
      3. 上传 OCR
      4. 轮询结果
      5. 下载覆盖原文件
      6. 验证新文件
      7. 失败则从备份恢复；成功则清理备份
    """
    # 1. 记录原页数
    original_page_count = get_page_count(pdf_path)

    # 2. 备份
    backup_path = backup_original(pdf_path, target_dir)
    if Config.BACKUP_ORIGINAL and backup_path is None:
        # 启用备份但失败 -> 终止以保护原文件
        msg = "备份失败，终止转换以保护原文件"
        print(f"  ⚠ {msg}")
        log().error(msg)
        return False

    # 3. OCR 上传
    task_id = upload_pdf(pdf_path, {"doc.extractionMode": "mixed"})
    if not task_id:
        return False

    # 4. 轮询
    result = poll_result(task_id, with_data=False)
    if not result or result.get("code") != 100:
        msg = f"OCR 任务失败: {result.get('data', '未知') if result else '无响应'}"
        print(f"  ⚠ {msg}")
        log().warning(msg)
        clear_task(task_id)
        return False

    # 5. 下载
    success = download_result(task_id, ["pdfLayered"], pdf_path)
    clear_task(task_id)
    if not success:
        return False

    # 6. 验证
    if Config.VERIFY_ON_SUCCESS:
        if not verify_converted_pdf(pdf_path, original_page_count):
            print("  ⚠ 验证失败，从备份恢复原文件")
            log().error("新 PDF 验证失败，执行回滚")
            if backup_path and backup_path.exists():
                restore_from_backup(pdf_path, backup_path)
            return False
        print("  ✅ 验证通过")

    # 7. 验证通过，清理备份（节省空间）
    if Config.CLEANUP_BACKUP_ON_SUCCESS and backup_path and backup_path.exists():
        cleanup_backup(backup_path)

    return True


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
        "stats": {"converted": 0, "already_dual": 0, "failed": 0, "skipped": 0},
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
        print(f"  ⚠ 保存进度失败: {e}")
        log().error(f"保存进度失败: {e}")


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
    lines.append(f"| 🔄 成功转换 | {stats['converted']} |")
    lines.append(f"| ❌ 转换失败 | {stats['failed']} |")
    lines.append(f"| ⏭ 跳过 | {stats['skipped']} |")

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
        "converted": "🔄 已转换",
        "failed": "❌ 失败",
        "skipped": "⏭ 跳过",
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
    # 自动探测 Umi-OCR
    if not detect_umi_ocr():
        print("❌ 无法连接 Umi-OCR，请确保 Umi-OCR 已启动且 HTTP 服务已开启")
        sys.exit(1)

    # 支持命令行参数指定目录
    if len(sys.argv) > 1:
        target_dir = os.path.abspath(sys.argv[1])
    else:
        target_dir = os.path.dirname(os.path.abspath(__file__))

    if not os.path.isdir(target_dir):
        print(f"❌ 目标目录不存在: {target_dir}")
        sys.exit(1)

    # 初始化日志
    log_file = setup_logging(target_dir)
    log().info(f"=== 开始运行 v{__version__} ===")
    log().info(f"目标目录: {target_dir}")
    log().info(f"工作目录: {get_work_dir()}")

    print(f"目标目录: {target_dir}")
    print(f"工作目录: {get_work_dir()}")
    print(f"日志文件: {log_file}\n")

    pdfs = find_pdfs(target_dir)
    if not pdfs:
        print("未找到任何 PDF 文件。")
        log().info("未找到任何 PDF 文件")
        return

    # 进度文件（按目标目录 hash 隔离）
    progress_path = get_progress_path(target_dir)
    progress = load_progress(progress_path)
    processed_files = {r["file"] for r in progress["records"]}
    stats = progress["stats"]
    stats["total"] = len(pdfs)
    records = progress["records"]

    print(f"找到 {len(pdfs)} 个 PDF 文件")
    if processed_files:
        print(f"已处理 {len(processed_files)} 个，跳过续跑")
    print()

    interrupted = False
    interrupted_file: Optional[str] = None
    start_time = datetime.now()

    try:
        for i, pdf_path in enumerate(pdfs, 1):
            rel_path = str(pdf_path.relative_to(target_dir))

            if rel_path in processed_files:
                print(f"[{i}/{len(pdfs)}] {rel_path}  ⏭ 已处理，跳过")
                continue

            interrupted_file = rel_path
            print(f"[{i}/{len(pdfs)}] {rel_path}")
            log().info(f"处理: {rel_path}")

            # 阶段 1：检测文本层
            print("  🔍 检测文本层...", end=" ", flush=True)
            has_text = has_text_layer(pdf_path)

            if has_text is None:
                print("检测失败，跳过")
                stats["skipped"] += 1
                records.append({"file": rel_path, "status": "skipped", "detail": "检测失败"})
                processed_files.add(rel_path)
                save_progress(progress_path, {
                    "records": records, "stats": stats,
                    "target_dir": target_dir, "version": __version__,
                })
                continue

            if has_text:
                print("✅ 已是双层 PDF，跳过")
                stats["already_dual"] += 1
                records.append({"file": rel_path, "status": "already_dual", "detail": "已有文本层"})
                processed_files.add(rel_path)
                save_progress(progress_path, {
                    "records": records, "stats": stats,
                    "target_dir": target_dir, "version": __version__,
                })
                continue

            print("❌ 无文本层，开始 OCR...")

            # 阶段 2：OCR 转双层 PDF
            print("  🔄 OCR 转换中...", end=" ", flush=True)
            if ocr_to_dual_layer(pdf_path, target_dir):
                print("✅ 转换成功")
                stats["converted"] += 1
                records.append({"file": rel_path, "status": "converted", "detail": "OCR 成功"})
            else:
                print("❌ 转换失败")
                stats["failed"] += 1
                records.append({"file": rel_path, "status": "failed", "detail": "OCR 失败"})

            processed_files.add(rel_path)
            interrupted_file = None
            save_progress(progress_path, {
                "records": records, "stats": stats,
                "target_dir": target_dir, "version": __version__,
            })

    except KeyboardInterrupt:
        interrupted = True
        print("\n\n⏸ 用户中断！进度已保存。")
        log().warning("用户中断")
        if interrupted_file:
            print(f"中断时正在处理: {interrupted_file}（未完成，下次将从该文件继续）")
            records = [r for r in records if r["file"] != interrupted_file]

    # 生成报告（写入目标目录，便于用户查看；这是产出物，非临时文件）
    end_time = datetime.now()
    duration = end_time - start_time
    report_path = Path(target_dir) / "pdf_conversion_report.md"
    write_report(report_path, stats, records, start_time, end_time, duration, interrupted)

    print(f"\n报告已生成: {report_path}")
    print(f"日志文件: {log_file}")
    log().info(f"=== 运行结束，耗时 {duration} ===")


if __name__ == "__main__":
    main()
