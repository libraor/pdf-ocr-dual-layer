# PDF 双层转换工具

使用 [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR/releases) 将非双层（不可搜索）PDF 批量转换为双层（可搜索）PDF。

> 林尧 · 浙江泽大律师事务所 高级合伙人 · linyao@foxmail.com

- 本地 PyMuPDF 秒级检测文本层，跳过已是双层的 PDF
- 自动端口探测，适配 Umi-OCR 端口漂移
- 断点续跑，`Ctrl+C` 中断后自动从上次位置继续
- 转换后自动验证（页数/文本层），异常自动回滚
- 验证通过后自动清理备份，不堆积硬盘占用
- 失败按原因分类重试（超时 2x、文件占用内置等待、其余普通重试）
- Umi-OCR 任务自动清理（try/finally），避免服务端堆积
- 备份/恢复均支持文件占用自动重试（3/6/9s）
- 下载统一指数退避重试（3x2^attempt，共 5 次）
- 大文件上传专用超时（`UPLOAD_TIMEOUT`）
- 线程安全 logging 双 handler，并发模式不交错
- 临时/进度/备份文件本地化，不污染同步目录
- 支持 OCR 并发处理（`max_concurrent_ocr`），提升吞吐
- 输出 Markdown 转换报告

## 依赖

- Python 3.9+
- [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR/releases) 已启动且 HTTP 服务已开启
- Python 包：`requests`、`PyMuPDF`

```bash
pip install -r requirements.txt
```

## 快速使用

```bash
python convert_pdfs_to_dual_layer.py "目标目录路径"
```

不传参数则处理脚本所在目录。

### 工作流程

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
  └─ 无文本层 -> 标记 need_ocr（先持久化）-> 提交 OCR 任务
       ├─ 并发模式：线程池执行（max_concurrent_ocr 个任务并行）
       └─ 串行模式：直接执行
            ↓
       Umi-OCR 转换 -> 验证
       ├─ 验证通过 -> 清理备份 -> 标记 converted
       └─ 验证失败 -> 从备份恢复 -> 标记 failed（延迟到最后批量重试）
  ↓
全部文件扫描完成 -> 批量重试所有 failed 文件
  ↓
生成 pdf_conversion_report.md 报告
```

### 文件状态机

| 状态 | 含义 | 重启时行为 |
|------|------|-----------|
| `already_dual` | 已检测为双层 | 跳过 |
| `need_ocr` | 已检测需 OCR（中断未完成） | **直接 OCR，跳过文本层检测** |
| `converted` | OCR 转换成功 | 跳过 |
| `failed` | OCR 转换失败 | **延迟到最后批量重试，不阻塞新文件** |
| `skipped` | 检测失败 | 重新检测 |
| `excluded` | 匹配排除规则 | 永久跳过 |

> 关键优化：检测为需要 OCR 时立即保存 `need_ocr` 状态。即使 OCR 阶段中断，下次重启也直接进入 OCR，不会重复检测。

## 文件存储架构

主程序可存放在坚果云同步目录，多设备共享。临时文件、日志、进度、备份均存放本地专用目录，不参与云同步。

| 类型 | 位置 | 是否同步 |
|------|------|---------|
| 主程序 | 脚本目录 | ✅ |
| 临时缓存 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\cache\` | ❌ |
| 运行日志 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\logs\` | ❌ |
| 进度文件 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\progress\` | ❌ |
| 原 PDF 备份 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\backup\` | ❌ |
| 转换报告 | 目标目录 | 视目标目录而定 |

> Linux/Mac：`~/.local/share/pdf-ocr-dual-layer/` 
> 可通过环境变量 `PDF_OCR_WORK_DIR` 覆盖。

## 配置

支持三种配置方式，**优先级：环境变量 > `config.ini` > 代码默认值**。

### 方式一：配置文件（推荐）

编辑脚本同目录的 [config.ini](./config.ini)，所有项均带注释说明：

```ini
[server]
try_ports = 1224, 1225, 1226, 1227, 1228, 1229, 1230, 1241

[http]
request_timeout = 30
poll_timeout = 60
download_timeout = 300
# upload_timeout = 300        # 大 PDF 上传超时，未配置时取 max(request, download)
poll_interval = 2
max_poll_time = 600

[storage]
# 留空使用系统默认
work_dir =

[behavior]
backup_original = true
verify_on_success = true
cleanup_backup_on_success = true
text_layer_ratio = 0.5
max_concurrent_ocr = 3                            # 并发任务数（1=串行，推荐2-4）

[filter]
# 文件名排除规则（通配符 * ?，逗号分隔，不区分大小写）
exclude_patterns = *银行流水*,*流水*

[ocr]
# Umi-OCR 识别参数
extraction_mode = mixed                          # mixed/fullPage/ocrOnly
language = models/config_chinese.txt             # 简体中文/English/繁體/日本語/한국어/Русский
cls = false                                      # 纠正文本方向
limit_side_len = 960                             # 960/2880/4320/999999
parser = multi_para                              # 排版解析方案
```

配置文件查找位置（按顺序，首个存在的生效）：
1. 环境变量 `PDF_OCR_CONFIG_FILE` 指定的路径
2. 脚本所在目录的 `config.ini`

> 想为不同设备维护独立配置：把 `config.ini` 加入 `.gitignore`/坚果云排除，用 `PDF_OCR_CONFIG_FILE` 指向各设备本地路径。

### 方式二：环境变量（覆盖配置文件）

| 变量 | 默认值 | 说明 | 对应配置项 |
|------|--------|------|-----------|
| `PDF_OCR_CONFIG_FILE` | - | 指定配置文件路径 | - |
| `PDF_OCR_WORK_DIR` | 系统默认 | 本地工作目录 | `[storage] work_dir` |
| `PDF_OCR_BACKUP` | `1` | 是否备份原文件（`0` 关闭，**风险自负**） | `[behavior] backup_original` |
| `PDF_OCR_VERIFY` | `1` | 转换后是否验证新 PDF | `[behavior] verify_on_success` |
| `PDF_OCR_CLEANUP_BACKUP` | `1` | 验证通过后是否清理备份 | `[behavior] cleanup_backup_on_success` |
| `PDF_OCR_TIMEOUT` | `30` | HTTP 请求超时（秒） | `[http] request_timeout` |
| `PDF_OCR_POLL_TIMEOUT` | `60` | 轮询专用超时（秒） | `[http] poll_timeout` |
| `PDF_OCR_DOWNLOAD_TIMEOUT` | `300` | 下载专用超时（秒，大文件下载） | `[http] download_timeout` |
| `PDF_OCR_UPLOAD_TIMEOUT` | `300` | 上传专用超时（秒，大 PDF 上传） | `[http] upload_timeout` |
| `PDF_OCR_POLL_INTERVAL` | `2` | 轮询间隔（秒） | `[http] poll_interval` |
| `PDF_OCR_MAX_TIME` | `600` | 单个 PDF 最大处理时间（秒） | `[http] max_poll_time` |
| `PDF_OCR_PORTS` | `1224,...` | 端口列表（逗号分隔） | `[server] try_ports` |
| `PDF_OCR_EXTRACTION_MODE` | `mixed` | 提取模式（`mixed`/`fullPage`/`ocrOnly`） | `[ocr] extraction_mode` |
| `PDF_OCR_LANGUAGE` | `models/config_chinese.txt` | 语言/模型库 | `[ocr] language` |
| `PDF_OCR_CLS` | `false` | 纠正文本方向 | `[ocr] cls` |
| `PDF_OCR_LIMIT_SIDE_LEN` | `960` | 限制图像边长 | `[ocr] limit_side_len` |
| `PDF_OCR_PARSER` | `multi_para` | 排版解析方案 | `[ocr] parser` |
| `PDF_OCR_EXCLUDE` | (空) | 文件名排除模式（逗号分隔） | `[filter] exclude_patterns` |
| `PDF_OCR_MAX_CONCURRENT` | `3` | OCR 并发任务数（`1`=串行） | `[behavior] max_concurrent_ocr` |

### OCR 识别参数详解

| 参数 | 可选值 | 说明 |
|------|--------|------|
| `extraction_mode` | `mixed` / `fullPage` / `ocrOnly` | mixed=混合(图文混排推荐)；fullPage=整页强制OCR(扫描件)；ocrOnly=仅OCR图片 |
| `language` | `models/config_chinese.txt` 等 | 语言模型库，详见 config.ini 注释 |
| `cls` | `true` / `false` | 启用方向分类，识别倾斜或倒置的文本，可能降低速度 |
| `limit_side_len` | `960` / `2880` / `4320` / `999999` | 边长大于此值的图片会被压缩；越大越精确越慢 |
| `parser` | `multi_para` 等 | 排版解析方案：multi_para=多栏自然段；single_code=保留缩进(代码) |

> `textOnly` 提取模式由脚本内部用于文本层检测，不可在 `[ocr]` 段配置。
> 完整参数说明参见 [Umi-OCR HTTP API 文档](https://github.com/hiroi-sora/Umi-OCR/blob/main/docs/http/api_doc.md)。

### 方式三：默认值

不配置任何项即使用代码内置默认值，开箱即用。

### 配置组合

| BACKUP | VERIFY | CLEANUP | 行为 |
|--------|--------|---------|------|
| 1 | 1 | 1 | **默认**：备份->OCR->验证->通过清理/失败回滚 |
| 1 | 1 | 0 | 备份保留：验证通过也保留备份，可手动回滚 |
| 1 | 0 | - | 不验证：OCR 完即视为成功，备份保留 |
| 0 | - | - | 不备份：直接覆盖原文件（不推荐） |

### 设置示例（PowerShell）

```powershell
# 临时覆盖（当前会话）
$env:PDF_OCR_TIMEOUT = "99"

# 永久设置（重启终端生效）
setx PDF_OCR_CLEANUP_BACKUP "0"
setx PDF_OCR_MAX_TIME "1800"
setx PDF_OCR_WORK_DIR "D:\pdf-ocr"
setx PDF_OCR_CONFIG_FILE "D:\my-pdf-ocr-config.ini"
```

## 文件命名规范

| 类型 | 命名模板 | 示例 |
|------|---------|------|
| 进度文件 | `<hash8>_pdf_conversion_progress.json` | `a1b2c3d4_pdf_conversion_progress.json` |
| 日志文件 | `convert_<YYYYMMDD_HHMMSS>_<hash8>.log` | `convert_20260725_143022_a1b2c3d4.log` |
| 备份文件 | `<原文件名>.bak.pdf` | `report.bak.pdf` |
| 临时文件 | `<task_id>.tmp.pdf` | `abc123.tmp.pdf` |

`hash8` = `md5(目标目录绝对路径)[:8]`，确保不同目录的进度/备份互不干扰。
备份前会清理同名历史备份，避免重试时堆积。

## 故障排查

| 问题 | 解决 |
|------|------|
| Umi-OCR 连接失败 | 确保 Umi-OCR 已打开，设置中开启 HTTP 服务 |
| 端口不通 | 查看 `UmiOCR-data/.pre_settings` 确认实际端口 |
| OCR 转换失败 | 少数 PDF 格式损坏，可手动检查或用其他工具处理 |
| 验证失败已回滚 | 新 PDF 异常（页数/文本层），原文件已自动恢复；查看日志排查 |
| 文件被占用 | 关闭占用 PDF 的程序后重试（备份/恢复均自动等待 3/6/9s 重试） |
| 大 PDF 超时 | 调大 `PDF_OCR_MAX_TIME` 或 `PDF_OCR_POLL_TIMEOUT` |
| 上传超时 | 调大 `PDF_OCR_UPLOAD_TIMEOUT` |
| 进度丢失 | 检查 `%LOCALAPPDATA%\pdf-ocr-dual-layer\progress\` 是否可写 |
| 想重新开始 | 删除对应 `<hash>_pdf_conversion_progress.json` 后重跑 |
| 想保留所有备份 | 设置 `PDF_OCR_CLEANUP_BACKUP=0` |
| 误覆盖想恢复 | 备份目录仅保留验证失败的文件；如需回滚成功的转换，关闭 `PDF_OCR_CLEANUP_BACKUP` 后重跑 |

## 项目结构

```
pdf-ocr-dual-layer/
├── README.md                       # 本文件
├── SKILL.md                        # AI Skill 描述文件（面向 Agent 自动化）
├── convert_pdfs_to_dual_layer.py   # 主程序
├── config.ini                      # 配置文件模板
└── requirements.txt                # Python 依赖
```

## 许可

随主项目分发。

## 变更日志

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.1.0 | 2026-07-25 | 本地专用目录架构、备份机制、进度隔离 |
| 1.2.0 | 2026-07-25 | 转换后验证、失败自动回滚、验证通过自动清理备份 |
| 1.3.0 | 2026-07-25 | 新增 `config.ini` 配置文件支持，三级优先级：环境变量 > 配置文件 > 默认值 |
| 1.4.0 | 2026-07-25 | 新增 `[ocr]` 段，Umi-OCR 识别参数（提取模式/语言/方向纠正/边长/排版）可配置 |
| 1.5.0 | 2026-07-25 | 状态机驱动流程，新增 `need_ocr` 中间态，中断不重复检测，`failed` 自动重试 |
| 1.5.1 | 2026-07-25 | 修复 Windows 跨卷下载 Bug；备份前清理历史备份，避免重试堆积 |
| 1.5.2 | 2026-07-25 | 新增 `DOWNLOAD_TIMEOUT` 配置项，修复多页 PDF 下载超时失败 |
| 1.6.0 | 2026-07-25 | 新增文件名排除规则（`[filter] exclude_patterns`），跳过银行流水等大型扫描件 |
| 1.7.0 | 2026-07-26 | 新增 OCR 并发处理（`[behavior] max_concurrent_ocr`），线程池提升吞吐；进度文件加锁线程安全 |
| 1.8.0 | 2026-07-28 | 失败文件延迟到最终批量重试，不阻塞新文件；超长文件名自动截短修复 Umi-OCR 下载失败 |
| 1.9.0 | 2026-07-28 | 按失败原因分类重试：超时自动 2x 时间，文件占用内置等待重试；记录精准失败原因 |
| 1.9.1 | 2026-07-28 | OCR 失败确保清理 Umi-OCR 任务（try/finally）；恢复备份支持文件占用重试；新增 `UPLOAD_TIMEOUT`；统一下载重试日志 |
| 1.9.2 | 2026-07-28 | 全面改用 logging 输出（线程安全，并发模式不交错）；控制台 INFO + 文件 DEBUG 双 handler |
