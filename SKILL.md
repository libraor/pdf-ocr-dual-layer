---
name: pdf-ocr-dual-layer
description: >
  使用 Umi-OCR 将非双层（不可搜索）PDF 批量转换为双层（可搜索）PDF。
  支持断点续跑、本地文本层检测、自动端口探测、Markdown 报告输出。
  Umi-OCR 必须运行中且 HTTP 服务已开启。
  临时文件、进度、备份均存放于本地专用目录，不污染同步目录。
---

# PDF 双层转换

## 前置条件

1. **Umi-OCR** 已启动，HTTP 服务已开启（默认端口 1224，脚本自动探测）
   - 下载：<https://github.com/hiroi-sora/Umi-OCR/releases>
2. Python 3.9+ 已安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

## 文件存储架构

| 类型 | 位置 | 是否同步 | 说明 |
|------|------|---------|------|
| 主程序（脚本/SKILL.md） | 脚本目录（坚果云） | ✅ 同步 | 多设备共享代码 |
| 临时缓存 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\cache\` | ❌ 本地 | OCR 中间产物 |
| 运行日志 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\logs\` | ❌ 本地 | 详细运行日志 |
| 进度文件 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\progress\` | ❌ 本地 | 断点续传 |
| 原 PDF 备份 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\backup\` | ❌ 本地 | 覆盖前自动备份，验证通过后自动清理 |
| 转换报告 | 目标目录 | 视目标目录而定 | 产出物，便于查看 |

> Linux/Mac 等价路径：`~/.local/share/pdf-ocr-dual-layer/`
> 可通过环境变量 `PDF_OCR_WORK_DIR` 覆盖本地工作目录。

详细设计方案见 [STORAGE_DESIGN.md](./STORAGE_DESIGN.md)。

## 使用方式

### 一键执行

```
对 <目标目录> 执行 PDF 双层转换
```

### 手动执行

```bash
python "<脚本所在目录>/convert_pdfs_to_dual_layer.py" "目标目录路径"
```

不传参数则处理脚本所在目录。

## 工作流程

```
扫描目录所有 PDF
  ↓
状态机决策（按已有记录）
  ├─ already_dual / converted -> 跳过
  ├─ need_ocr                  -> 直接 OCR（跳过文本层检测）
  ├─ failed                    -> 重试 OCR
  ├─ skipped                   -> 重新检测
  └─ 无记录                    -> 完整流程
       ↓
PyMuPDF 本地检测文本层（<0.1秒/文件，比例阈值 50%）
  ├─ 有文本层 -> 标记 already_dual，跳过
  └─ 无文本层 -> 标记 need_ocr（先持久化）-> Umi-OCR 转换 -> 验证
       ├─ 验证通过 -> 清理备份 -> 标记 converted
       └─ 验证失败 -> 从备份恢复 -> 标记 failed
  ↓
生成 pdf_conversion_report.md 报告（写入目标目录）
```

### 文件状态机

| 状态 | 含义 | 重启时行为 |
|------|------|-----------|
| `already_dual` | 已检测为双层 | 跳过 |
| `need_ocr` | 已检测需 OCR（中断未完成） | **直接 OCR，跳过文本层检测** |
| `converted` | OCR 转换成功 | 跳过 |
| `failed` | OCR 转换失败 | 重试 OCR |
| `skipped` | 检测失败 | 重新检测 |

> 关键优化：检测为需要 OCR 时立即保存 `need_ocr` 状态。即使 OCR 阶段中断，下次重启也直接进入 OCR，不会重复检测。

## 关键特性

| 特性 | 说明 |
|------|------|
| **状态机驱动** | 5 种文件状态，中断后精准续跑，不重复检测 |
| **断点续跑** | `Ctrl+C` 中断后重新执行，按状态决定是否重试 |
| **本地检测** | PyMuPDF 抽样检测文本层，秒级判断，不消耗 HTTP 资源 |
| **端口自动探测** | 尝试 [1224-1230, 1241] 等端口，适配端口漂移 |
| **原子写入** | 先写临时文件再替换，中断不会损坏原文件 |
| **转换验证** | 验证新 PDF 页数与文本层，异常自动回滚 |
| **原文件备份** | 覆盖前自动备份到本地专用目录，验证通过后自动清理 |
| **进度隔离** | 不同目标目录的进度按 hash 隔离，互不干扰 |
| **Markdown 报告** | 输出汇总统计 + 每文件详细状态 |
| **本地化存储** | 临时/进度/备份均存放本地，不污染同步目录 |
| **日志记录** | 详细日志写入本地，便于排错 |
| **进度提示** | 长时间 OCR 每 60 秒输出已等待时间 |

## 配置

支持三级配置：**环境变量 > `config.ini` > 默认值**。

### 配置文件（推荐）

编辑脚本同目录的 `config.ini`，分段配置服务器/HTTP/存储/行为：

```ini
[server]
try_ports = 1224, 1225, 1226, 1227, 1228, 1229, 1230, 1241

[http]
request_timeout = 30
poll_timeout = 60
poll_interval = 2
max_poll_time = 600

[storage]
work_dir =

[behavior]
backup_original = true
verify_on_success = true
cleanup_backup_on_success = true
text_layer_ratio = 0.5

[ocr]
# Umi-OCR 识别参数
extraction_mode = mixed                          # mixed/fullPage/ocrOnly
language = models/config_chinese.txt             # 语言模型库
cls = false                                      # 纠正文本方向
limit_side_len = 960                             # 960/2880/4320/999999
parser = multi_para                              # 排版解析方案
```

配置文件查找位置（按顺序，首个存在的生效）：
1. 环境变量 `PDF_OCR_CONFIG_FILE` 指定的路径
2. 脚本所在目录的 `config.ini`

### 环境变量（覆盖配置文件）

| 变量 | 默认值 | 说明 | 对应配置项 |
|------|--------|------|-----------|
| `PDF_OCR_CONFIG_FILE` | - | 指定配置文件路径 | - |
| `PDF_OCR_WORK_DIR` | 系统默认 | 本地工作目录（缓存/日志/进度/备份） | `[storage] work_dir` |
| `PDF_OCR_BACKUP` | `1` | 是否备份原文件（`0` 关闭，**风险自负**） | `[behavior] backup_original` |
| `PDF_OCR_VERIFY` | `1` | 转换后是否验证新 PDF（页数/文本层） | `[behavior] verify_on_success` |
| `PDF_OCR_CLEANUP_BACKUP` | `1` | 验证通过后是否清理备份（`0` 保留用于回滚） | `[behavior] cleanup_backup_on_success` |
| `PDF_OCR_TIMEOUT` | `30` | HTTP 请求超时（秒，上传/下载/普通请求） | `[http] request_timeout` |
| `PDF_OCR_POLL_TIMEOUT` | `60` | 轮询专用超时（秒，OCR 处理大文件响应慢） | `[http] poll_timeout` |
| `PDF_OCR_POLL_INTERVAL` | `2` | 轮询间隔（秒） | `[http] poll_interval` |
| `PDF_OCR_MAX_TIME` | `600` | 单个 PDF 最大处理时间（秒） | `[http] max_poll_time` |
| `PDF_OCR_PORTS` | `1224,...` | 端口列表（逗号分隔） | `[server] try_ports` |
| `PDF_OCR_EXTRACTION_MODE` | `mixed` | 提取模式（`mixed`/`fullPage`/`ocrOnly`） | `[ocr] extraction_mode` |
| `PDF_OCR_LANGUAGE` | `models/config_chinese.txt` | 语言/模型库 | `[ocr] language` |
| `PDF_OCR_CLS` | `false` | 纠正文本方向 | `[ocr] cls` |
| `PDF_OCR_LIMIT_SIDE_LEN` | `960` | 限制图像边长（960/2880/4320/999999） | `[ocr] limit_side_len` |
| `PDF_OCR_PARSER` | `multi_para` | 排版解析方案 | `[ocr] parser` |

> `textOnly` 提取模式由脚本内部用于文本层检测，不可在 `[ocr]` 段配置。
> 完整参数说明参见 [Umi-OCR HTTP API 文档](https://github.com/hiroi-sora/Umi-OCR/blob/main/docs/http/api_doc.md)。

**配置组合**：

| BACKUP | VERIFY | CLEANUP | 行为 |
|--------|--------|---------|------|
| 1 | 1 | 1 | 默认：备份->OCR->验证->通过清理/失败回滚 |
| 1 | 1 | 0 | 备份保留：验证通过也保留备份，可手动回滚 |
| 1 | 0 | - | 不验证：OCR 完即视为成功，备份保留 |
| 0 | - | - | 不备份：直接覆盖原文件（不推荐） |

## 进度与备份文件命名规范

- **进度文件**：`<target_hash8>_pdf_conversion_progress.json`
  - 同一目标目录多次运行会复用同一进度文件
  - target_hash 为目标目录绝对路径的 MD5 前 8 位
- **备份文件**：`<原文件名>.bak.pdf`，重名时附加时间戳 `<原文件名>.YYYYMMDD_HHMMSS.bak.pdf`
- **日志文件**：`convert_<YYYYMMDD_HHMMSS>_<target_hash8>.log`

## 故障排查

| 问题 | 解决 |
|------|------|
| Umi-OCR 连接失败 | 确保 Umi-OCR 已打开，设置中开启 HTTP 服务 |
| 端口不通 | 查看 `UmiOCR-data/.pre_settings` 确认实际端口 |
| OCR 转换失败 | 少数 PDF 格式损坏，可手动检查或用其他工具处理 |
| 验证失败已回滚 | 新 PDF 异常（页数/文本层），原文件已自动恢复；可查看日志排查 |
| 文件被占用 | 关闭占用 PDF 的程序后重试 |
| 大 PDF 超时 | 设置 `PDF_OCR_MAX_TIME=1800`（30 分钟） |
| 进度丢失 | 检查 `%LOCALAPPDATA%\pdf-ocr-dual-layer\progress\` 是否可写 |
| 想重新开始 | 删除对应 `<hash>_pdf_conversion_progress.json` 后重跑 |
| 想保留所有备份 | 设置 `PDF_OCR_CLEANUP_BACKUP=0` |
| 误覆盖想恢复 | 备份目录中仅保留验证失败的文件，成功的已清理；如需回滚成功的转换，需关闭 `PDF_OCR_CLEANUP_BACKUP` 后重跑 |
