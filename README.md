# PDF 双层转换工具

使用 [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR/releases) 将非双层（不可搜索）PDF 批量转换为双层（可搜索）PDF。

- 本地 PyMuPDF 秒级检测文本层，跳过已是双层的 PDF
- 自动端口探测，适配 Umi-OCR 端口漂移
- 断点续跑，`Ctrl+C` 中断后自动从上次位置继续
- 转换后自动验证（页数/文本层），异常自动回滚
- 验证通过后自动清理备份，不堆积硬盘占用
- 临时/进度/备份文件本地化，不污染同步目录
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
PyMuPDF 本地检测文本层（<0.1秒/文件，比例阈值 50%）
  ├─ 有文本层 -> 跳过
  └─ 无文本层 -> 备份原文件 -> Umi-OCR HTTP 转换 -> 覆盖原文件
       ↓
       验证新文件（页数/文本层）
       ├─ 验证通过 -> 清理备份（节省空间）
       └─ 验证失败 -> 从备份恢复原文件
  ↓
生成 pdf_conversion_report.md 报告
```

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

详细设计方案见 [STORAGE_DESIGN.md](./STORAGE_DESIGN.md)。

## 配置

所有配置通过环境变量覆盖，均有默认值。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PDF_OCR_WORK_DIR` | 系统默认 | 本地工作目录（缓存/日志/进度/备份） |
| `PDF_OCR_BACKUP` | `1` | 是否备份原文件（`0` 关闭，**风险自负**） |
| `PDF_OCR_VERIFY` | `1` | 转换后是否验证新 PDF（页数/文本层） |
| `PDF_OCR_CLEANUP_BACKUP` | `1` | 验证通过后是否清理备份（`0` 保留用于回滚） |
| `PDF_OCR_TIMEOUT` | `30` | HTTP 请求超时（秒，上传/下载/普通请求） |
| `PDF_OCR_POLL_TIMEOUT` | `60` | 轮询专用超时（秒，OCR 处理大文件响应慢） |
| `PDF_OCR_POLL_INTERVAL` | `2` | 轮询间隔（秒） |
| `PDF_OCR_MAX_TIME` | `600` | 单个 PDF 最大处理时间（秒） |

### 配置组合

| BACKUP | VERIFY | CLEANUP | 行为 |
|--------|--------|---------|------|
| 1 | 1 | 1 | **默认**：备份->OCR->验证->通过清理/失败回滚 |
| 1 | 1 | 0 | 备份保留：验证通过也保留备份，可手动回滚 |
| 1 | 0 | - | 不验证：OCR 完即视为成功，备份保留 |
| 0 | - | - | 不备份：直接覆盖原文件（不推荐） |

### 设置示例（PowerShell）

```powershell
setx PDF_OCR_CLEANUP_BACKUP "0"     # 保留所有备份
setx PDF_OCR_MAX_TIME "1800"        # 大 PDF 超时延至 30 分钟
setx PDF_OCR_WORK_DIR "D:\pdf-ocr"  # 自定义工作目录
```

设置后需重启终端生效。

## 文件命名规范

| 类型 | 命名模板 | 示例 |
|------|---------|------|
| 进度文件 | `<hash8>_pdf_conversion_progress.json` | `a1b2c3d4_pdf_conversion_progress.json` |
| 日志文件 | `convert_<YYYYMMDD_HHMMSS>_<hash8>.log` | `convert_20260725_143022_a1b2c3d4.log` |
| 备份文件 | `<原文件名>.bak.pdf` | `report.bak.pdf` |
| 备份文件（重名） | `<原文件名>.<时间戳>.bak.pdf` | `report.20260725_143022.bak.pdf` |
| 临时文件 | `<task_id>.tmp.pdf` | `abc123.tmp.pdf` |

`hash8` = `md5(目标目录绝对路径)[:8]`，确保不同目录的进度/备份互不干扰。

## 故障排查

| 问题 | 解决 |
|------|------|
| Umi-OCR 连接失败 | 确保 Umi-OCR 已打开，设置中开启 HTTP 服务 |
| 端口不通 | 查看 `UmiOCR-data/.pre_settings` 确认实际端口 |
| OCR 转换失败 | 少数 PDF 格式损坏，可手动检查或用其他工具处理 |
| 验证失败已回滚 | 新 PDF 异常（页数/文本层），原文件已自动恢复；查看日志排查 |
| 文件被占用 | 关闭占用 PDF 的程序后重试 |
| 大 PDF 超时 | 调大 `PDF_OCR_MAX_TIME` 或 `PDF_OCR_POLL_TIMEOUT` |
| 进度丢失 | 检查 `%LOCALAPPDATA%\pdf-ocr-dual-layer\progress\` 是否可写 |
| 想重新开始 | 删除对应 `<hash>_pdf_conversion_progress.json` 后重跑 |
| 想保留所有备份 | 设置 `PDF_OCR_CLEANUP_BACKUP=0` |
| 误覆盖想恢复 | 备份目录仅保留验证失败的文件；如需回滚成功的转换，关闭 `PDF_OCR_CLEANUP_BACKUP` 后重跑 |

## 项目结构

```
pdf-ocr-dual-layer/
├── README.md                       # 本文件
├── SKILL.md                        # AI Skill 描述文件
├── convert_pdfs_to_dual_layer.py   # 主程序
├── requirements.txt                # Python 依赖
└── STORAGE_DESIGN.md               # 文件存储设计方案
```

## 许可

随主项目分发。

## 变更日志

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.1.0 | 2026-07-25 | 本地专用目录架构、备份机制、进度隔离 |
| 1.2.0 | 2026-07-25 | 转换后验证、失败自动回滚、验证通过自动清理备份 |
