---
name: pdf-ocr-dual-layer
description: >
  使用 Umi-OCR 将非双层（不可搜索）PDF 批量转换为双层（可搜索）PDF。
  支持断点续跑、本地文本层检测、自动端口探测、Markdown 报告输出。
  Umi-OCR 必须运行中且 HTTP 服务已开启。
---

# PDF 双层转换

## 前置条件

1. **Umi-OCR** 已启动，HTTP 服务已开启（默认端口 1224，脚本自动探测）
2. Python 环境已安装 `requests` 和 `fitz`（PyMuPDF）

## 使用方式

### 一键执行

```
对 <目标目录> 执行 PDF 双层转换
```

脚本位置：与本 SKILL.md 同目录的 `convert_pdfs_to_dual_layer.py`

### 手动执行

```bash
python "C:\Users\linya\.claude\skills\pdf-ocr-dual-layer\convert_pdfs_to_dual_layer.py" "目标目录路径"
```

不传参数则处理脚本所在目录。

## 工作流程

```
扫描目录所有 PDF
  ↓
PyMuPDF 本地检测文本层（<0.1秒/文件）
  ├─ 有文本层 → 跳过（已是双层/可搜索 PDF）
  └─ 无文本层 → Umi-OCR HTTP 转换 → 覆盖原文件
  ↓
生成 pdf_conversion_report.md 报告
```

## 关键特性

| 特性 | 说明 |
|------|------|
| **断点续跑** | `Ctrl+C` 中断后重新执行同一命令，自动跳过已处理文件 |
| **本地检测** | PyMuPDF 抽样检测文本层，秒级判断，不消耗 HTTP 资源 |
| **端口自动探测** | 尝试 [1224-1230, 1241] 等端口，适配端口漂移 |
| **原子写入** | 先写临时文件再替换，中断不会损坏原文件 |
| **Markdown 报告** | 输出汇总统计 + 每文件详细状态 |

## 进度文件

- `pdf_conversion_progress.json` — 断点续跑记录，删除后重新开始
- `pdf_conversion_report.md` — 最终/阶段性报告

## 故障排查

| 问题 | 解决 |
|------|------|
| Umi-OCR 连接失败 | 确保 Umi-OCR 已打开，设置中开启 HTTP 服务 |
| 端口不通 | 查看 `UmiOCR-data/.pre_settings` 确认实际端口 |
| OCR 转换失败 | 少数 PDF 格式损坏，可手动检查或用其他工具处理 |
