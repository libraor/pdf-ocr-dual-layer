"""
Umi-OCR 批量转换非双层 PDF 为双层 PDF
用法：
  1. 确保 Umi-OCR 已启动且 HTTP 服务已开启（默认 http://localhost:1224）
  2. 将此脚本放在要处理的 PDF 目录下
  3. 运行：python convert_pdfs_to_dual_layer.py

流程：
  检测阶段（textOnly）→ 文本为空 → OCR 阶段（mixed）→ 双层 PDF 覆盖原文件
  检测阶段（textOnly）→ 有文本   → 跳过（已是双层 PDF）
"""

import os
import sys
import time
import json
import shutil
import requests

# 修复 Windows GBK 终端 emoji 输出问题
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

# ============ 配置 ============
UMI_OCR_BASE = None  # 自动探测

# 尝试的端口列表（Umi-OCR 端口可能因占用而漂移）
_TRY_PORTS = [1224, 1225, 1226, 1227, 1228, 1229, 1230, 1241]

def _detect_umi_ocr():
    """自动探测 Umi-OCR HTTP 服务地址"""
    global UMI_OCR_BASE
    if UMI_OCR_BASE:
        return True
    for port in _TRY_PORTS:
        try:
            url = f"http://localhost:{port}"
            r = requests.get(f"{url}/api/doc/get_options", timeout=3)
            if r.status_code == 200:
                UMI_OCR_BASE = url
                print(f"已连接 Umi-OCR: {url}")
                return True
        except Exception:
            continue
    return False

REQUEST_TIMEOUT = 10
POLL_INTERVAL = 2  # 轮询间隔（秒）
MAX_POLL_TIME = 600  # 单个 PDF 最大处理时间（秒）
# =============================


def find_pdfs(root_dir: str) -> list[Path]:
    """递归查找所有 PDF 文件"""
    root = Path(root_dir)
    pdfs = list(root.rglob("*.pdf")) + list(root.rglob("*.PDF"))
    return sorted(set(pdfs))


def upload_pdf(pdf_path: Path, options: dict) -> str | None:
    """上传 PDF 到 Umi-OCR，返回任务 ID"""
    url = f"{UMI_OCR_BASE}/api/doc/upload"
    try:
        with open(pdf_path, "rb") as f:
            files = {"file": (pdf_path.name, f, "application/pdf")}
            data = {"json": json.dumps(options)}
            resp = requests.post(url, files=files, data=data, timeout=REQUEST_TIMEOUT)
        result = resp.json()
        if result.get("code") == 100:
            return result["data"]
        else:
            print(f"  ⚠ 上传失败: {result.get('data', '未知错误')}")
            return None
    except Exception as e:
        print(f"  ⚠ 上传异常: {e}")
        return None


def poll_result(task_id: str, with_data: bool = True) -> dict | None:
    """轮询任务状态直到完成，返回结果"""
    url = f"{UMI_OCR_BASE}/api/doc/result"
    start_time = time.time()

    while time.time() - start_time < MAX_POLL_TIME:
        try:
            payload = {"id": task_id, "is_data": with_data, "format": "text"}
            resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            result = resp.json()

            code = result.get("code")
            is_done = result.get("is_done", False)

            if code == 100 and is_done:  # 成功且任务真正结束
                return result
            elif code == 100 and not is_done:  # 部分完成，继续等待
                time.sleep(POLL_INTERVAL)
                continue
            elif code == 101:  # 进行中
                time.sleep(POLL_INTERVAL)
                continue
            elif code == 102:  # 失败
                return {"code": 102, "data": result.get("data", "任务失败")}
            else:
                time.sleep(POLL_INTERVAL)
                continue
        except Exception as e:
            print(f"  ⚠ 轮询异常: {e}")
            time.sleep(POLL_INTERVAL)

    return {"code": -1, "data": "超时"}


def download_result(task_id: str, file_types: list[str], output_path: Path) -> bool:
    """下载识别结果文件，失败时最多重试 5 次"""
    url = f"{UMI_OCR_BASE}/api/doc/download"
    payload = {"id": task_id, "file_types": file_types, "ignore_blank": False}

    for attempt in range(5):
        try:
            resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            result = resp.json()

            if result.get("code") == 100:
                download_url = result["data"]
                # 下载文件
                dl_resp = requests.get(download_url, timeout=REQUEST_TIMEOUT * 10)
                dl_resp.raise_for_status()
                tmp_path = output_path.with_suffix(".tmp.pdf")
                tmp_path.write_bytes(dl_resp.content)
                tmp_path.replace(output_path)
                return True
            else:
                time.sleep(3)  # 等待 3 秒后重试
        except Exception as e:
            if attempt < 4:
                time.sleep(3)
            else:
                print(f"  ⚠ 下载异常: {e}")

    return False


def clear_task(task_id: str):
    """清理任务"""
    url = f"{UMI_OCR_BASE}/api/doc/clear/{task_id}"
    try:
        requests.get(url, timeout=REQUEST_TIMEOUT)
    except Exception:
        pass


def has_text_layer(pdf_path: Path) -> bool | None:
    """
    检测 PDF 是否有文本层（是否为双层/可搜索 PDF）
    使用 PyMuPDF 本地检测，无需 HTTP 请求，瞬间完成
    返回 True=有文本层, False=无, None=检测失败
    """
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(pdf_path))
        try:
            # 抽样检测：前3页 + 中间1页 + 最后1页，任一有文本即判定为双层
            page_count = doc.page_count
            if page_count == 0:
                return True  # 空文档，视为无需处理
            sample_indexes = [0]
            if page_count > 2:
                sample_indexes.append(1)
            if page_count > 3:
                sample_indexes.append(2)
            if page_count > 4:
                sample_indexes.append(page_count // 2)
            if page_count > 5:
                sample_indexes.append(page_count - 1)
            for i in sample_indexes:
                page = doc[i]
                text = page.get_text()
                if text and text.strip():
                    return True
            return False
        finally:
            doc.close()
    except ImportError:
        pass  # 回退到 HTTP 方式
    except Exception:
        return None

    # 回退：HTTP textOnly 方式
    task_id = upload_pdf(pdf_path, {"doc.extractionMode": "textOnly"})
    if not task_id:
        return None

    result = poll_result(task_id, with_data=True)
    if not result:
        clear_task(task_id)
        return None

    if result.get("code") != 100:
        clear_task(task_id)
        return None

    text = result.get("data", "")
    has_text = bool(text and text.strip())

    clear_task(task_id)
    return has_text


def ocr_to_dual_layer(pdf_path: Path) -> bool:
    """将 PDF OCR 转为双层 PDF，覆盖原文件"""
    task_id = upload_pdf(pdf_path, {"doc.extractionMode": "mixed"})
    if not task_id:
        return False

    result = poll_result(task_id, with_data=False)
    if not result or result.get("code") != 100:
        print(f"  ⚠ OCR 任务失败: {result.get('data', '未知') if result else '无响应'}")
        clear_task(task_id)
        return False

    success = download_result(task_id, ["pdfLayered"], pdf_path)
    clear_task(task_id)
    return success


from datetime import datetime

# 进度文件扩展名
PROGRESS_EXT = ".pdf_conversion_progress.json"


def load_progress(progress_path: str) -> dict:
    """加载已有的进度记录"""
    if os.path.exists(progress_path):
        try:
            with open(progress_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"records": [], "stats": {"converted": 0, "already_dual": 0, "failed": 0, "skipped": 0}}


def save_progress(progress_path: str, progress: dict):
    """保存当前进度"""
    try:
        with open(progress_path, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠ 保存进度失败: {e}")


def main():
    # 自动探测 Umi-OCR
    if not _detect_umi_ocr():
        print("❌ 无法连接 Umi-OCR，请确保 Umi-OCR 已启动且 HTTP 服务已开启")
        sys.exit(1)

    # 支持命令行参数指定目录
    if len(sys.argv) > 1:
        target_dir = os.path.abspath(sys.argv[1])
    else:
        target_dir = os.path.dirname(os.path.abspath(__file__))
    print(f"目标目录: {target_dir}\n")
    pdfs = find_pdfs(target_dir)
    script_dir = target_dir

    if not pdfs:
        print("未找到任何 PDF 文件。")
        return

    # 加载已有进度，实现续跑
    progress_path = os.path.join(script_dir, PROGRESS_EXT)
    progress = load_progress(progress_path)
    processed_files = {r["file"] for r in progress["records"]}
    stats = progress["stats"]
    stats["total"] = len(pdfs)  # total 始终取实际 PDF 总数
    records = progress["records"]

    print(f"找到 {len(pdfs)} 个 PDF 文件")
    if processed_files:
        print(f"已处理 {len(processed_files)} 个，跳过续跑")
    print()

    interrupted = False
    interrupted_file = None  # 记录中断时正在处理的文件
    start_time = datetime.now()

    try:
        for i, pdf_path in enumerate(pdfs, 1):
            rel_path = str(pdf_path.relative_to(script_dir))

            # 已处理过的跳过
            if rel_path in processed_files:
                print(f"[{i}/{len(pdfs)}] {rel_path}  ⏭ 已处理，跳过")
                continue

            interrupted_file = rel_path
            print(f"[{i}/{len(pdfs)}] {rel_path}")

            # 阶段 1：检测是否有文本层
            print("  🔍 检测文本层...", end=" ", flush=True)
            has_text = has_text_layer(pdf_path)

            if has_text is None:
                print("检测失败，跳过")
                stats["skipped"] += 1
                records.append({"file": rel_path, "status": "skipped", "detail": "检测失败"})
                processed_files.add(rel_path)
                save_progress(progress_path, {"records": records, "stats": stats})
                continue

            if has_text:
                print("✅ 已是双层 PDF，跳过")
                stats["already_dual"] += 1
                records.append({"file": rel_path, "status": "already_dual", "detail": "已有文本层"})
                processed_files.add(rel_path)
                save_progress(progress_path, {"records": records, "stats": stats})
                continue

            print("❌ 无文本层，开始 OCR...")

            # 阶段 2：OCR 转双层 PDF
            print("  🔄 OCR 转换中...", end=" ", flush=True)
            if ocr_to_dual_layer(pdf_path):
                print("✅ 转换成功")
                stats["converted"] += 1
                records.append({"file": rel_path, "status": "converted", "detail": "OCR 成功"})
            else:
                print("❌ 转换失败")
                stats["failed"] += 1
                records.append({"file": rel_path, "status": "failed", "detail": "OCR 失败"})

            processed_files.add(rel_path)
            interrupted_file = None
            # 每个文件处理完立即保存进度
            save_progress(progress_path, {"records": records, "stats": stats})

    except KeyboardInterrupt:
        interrupted = True
        print(f"\n\n⏸ 用户中断！进度已保存。")
        if interrupted_file:
            print(f"中断时正在处理: {interrupted_file}（未完成，下次将从该文件继续）")
            # 从 records 中移除可能未完成的记录
            records = [r for r in records if r["file"] != interrupted_file]

    # 生成 Markdown 报告
    end_time = datetime.now()
    duration = end_time - start_time
    report_path = os.path.join(script_dir, "pdf_conversion_report.md")
    write_report(report_path, stats, records, start_time, end_time, duration, interrupted)

    # 控制台摘要
    print(f"\n报告已生成: {report_path}")


def write_report(report_path, stats, records, start_time, end_time, duration, interrupted=False):
    """生成 Markdown 格式的转换报告"""
    lines = []
    lines.append("# PDF 双层转换报告\n")
    lines.append(f"**处理时间**: {start_time.strftime('%Y-%m-%d %H:%M:%S')} — {end_time.strftime('%H:%M:%S')}")
    lines.append(f"**总耗时**: {duration.seconds // 60} 分 {duration.seconds % 60} 秒")
    if interrupted:
        lines.append(f"\n⚠️ **任务被中断，进度已保存。下次运行将从断点继续。**")
    lines.append("---\n")
    lines.append("## 📊 汇总统计\n")
    lines.append("| 类别 | 数量 |")
    lines.append("|------|------|")
    lines.append(f"| 总计 | {stats['total']} |")
    lines.append(f"| ✅ 已是双层（跳过） | {stats['already_dual']} |")
    lines.append(f"| 🔄 成功转换 | {stats['converted']} |")
    lines.append(f"| ❌ 转换失败 | {stats['failed']} |")
    lines.append(f"| ⏭ 跳过 | {stats['skipped']} |")

    # 成功/失败率
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

    lines.append(f"\n---\n")
    lines.append(f"*报告由 `convert_pdfs_to_dual_layer.py` 自动生成*")

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    main()
