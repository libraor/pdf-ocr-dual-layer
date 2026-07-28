---
name: pdf-ocr-dual-layer
description: >
  使用 Umi-OCR 将非双层（不可搜索）PDF 批量转换为双层（可搜索）PDF。
  作者：林尧 · 浙江泽大律师事务所高级合伙人 · linyao@foxmail.com。
  支持断点续跑、12 线程并发 OCR、本地文本层检测、失败延迟重试、超长文件名自动截短、
  自动端口探测、Markdown 报告输出。
  Umi-OCR 必须运行中且 HTTP 服务已开启。
  临时文件、进度、备份均存放于本地专用目录，不污染同步目录。
---

# PDF 双层转换（Agent Skill）

> 林尧 · 浙江泽大律师事务所 高级合伙人 · linyao@foxmail.com

## 何时触发此 Skill

当用户出现以下意图时，主动调用本 Skill：

- 「把 XX 目录的 PDF 转成可搜索/双层」
- 「这些 PDF 不能复制文字，帮我处理」
- 「PDF 是扫描件，转成可搜索」
- 「继续上次的 OCR 任务」/「重试失败的」
- 「跳过银行流水」「排除某些文件」
- 询问目录下 PDF 的 OCR 状态/进度

## 前置条件（必须逐项验证）

### 1. Umi-OCR 服务检查

**必须**先确认 Umi-OCR 已安装且在运行，否则脚本会长时间探测端口后失败。

#### 1.1 检查是否已安装

```bash
# 常见安装路径检查
where Umi-OCR.exe 2>nul
dir /b "C:\Program Files\Umi-OCR" 2>nul
dir /b "C:\Program Files (x86)\Umi-OCR" 2>nul
dir /b "%LOCALAPPDATA%\Umi-OCR" 2>nul
dir /b "D:\Umi-OCR" 2>nul
```

**若未安装**：告知用户并给出安装指引（不要擅自下载安装）：

> ⚠️ 未检测到 Umi-OCR，这是 OCR 识别的核心依赖，必须先安装：
>
> 1. 下载：https://github.com/hiroi-sora/Umi-OCR/releases（选 `Umi-OCR_v*_Windows.7z`）
> 2. 解压到任意目录（如 `D:\Umi-OCR`）
> 3. 运行 `Umi-OCR.exe` 启动主程序
> 4. 进入「设置」→「HTTP 服务」→ 勾选「启用 HTTP 服务器」
> 5. 确认端口号（默认 1224，可能因占用漂移到 1225-1230 或 1241）
> 6. 安装完成后告诉我，我继续执行转换任务

#### 1.2 检查是否在运行

```bash
# 检查进程
tasklist | findstr /I "Umi-OCR"
# 检查端口（默认 1224-1230, 1241）
netstat -ano | findstr "1224\|1241"
```

**若已安装但未运行**：告知用户：

> ⚠️ Umi-OCR 已安装但未运行。请启动 `Umi-OCR.exe` 并确认 HTTP 服务已开启，然后告诉我继续。

**若已运行但端口未知**：查看 `UmiOCR-data/.pre_settings` 确认实际端口，或直接运行脚本（自动探测 1224-1230, 1241）。

### 2. Python 依赖检查

```bash
python -c "import requests, fitz; print('OK')"
```

失败则执行：
```bash
pip install -r "<脚本目录>/requirements.txt"
```

### 3. 目标目录确认

- 路径必须存在且可写
- 含中文/空格/特殊字符时**必须用引号**包裹
- 建议先用 `Glob` 统计 PDF 数量，给用户预期

## 执行指令

### 标准执行（推荐后台运行）

```bash
python "<脚本目录>/convert_pdfs_to_dual_layer.py" "<目标目录>"
```

**关键点（Agent 必读）**：
- 脚本是**长时间任务**（几百文件需数小时），**必须用 `blocking: false` 异步运行**
- 用 `CheckCommandStatus` 轮询，**不要**重复调用 `RunCommand`
- 中断用 `StopCommand`，脚本会捕获并保存进度
- 不传参数则处理脚本所在目录

### 执行后立即汇报

启动后**立即**向用户汇报：
1. 日志文件路径（`%LOCALAPPDATA%\pdf-ocr-dual-layer\logs\convert_*.log`）
2. 进度文件路径（`%LOCALAPPDATA%\pdf-ocr-dual-layer\progress\<hash>_pdf_conversion_progress.json`）
3. 预计耗时（按 ~80 秒/文件 × 文件数 / 并发度估算）

## 运行中监控

### 查看实时进度（无需停止任务）

```bash
# 查看最新日志尾部
python -c "import os,glob; d=os.path.expandvars(r'%LOCALAPPDATA%\pdf-ocr-dual-layer\logs'); f=max(glob.glob(d+r'\convert_*.log'),key=os.path.getmtime); print(''.join(open(f,encoding='utf-8').readlines()[-30:]))"
```

### 解析进度文件

```bash
python -c "
import json,glob,os
d=os.path.expandvars(r'%LOCALAPPDATA%\pdf-ocr-dual-layer\progress')
f=max(glob.glob(d+r'\*_pdf_conversion_progress.json'),key=os.path.getmtime)
data=json.load(open(f,encoding='utf-8'))
recs=data['records']
from collections import Counter
c=Counter(r['status'] for r in recs)
total=len(recs)
done=c.get('converted',0)+c.get('already_dual',0)
print(f'总计 {total} | 已完成 {done} ({done*100//total}%) | 进行中 {c.get(\"need_ocr\",0)} | 失败 {c.get(\"failed\",0)} | 排除 {c.get(\"excluded\",0)}')
# 失败原因分类
fails=[r for r in recs if r['status']=='failed']
if fails:
    from collections import Counter as C2
    fc=C2(r.get('detail','未知') for r in fails)
    print('失败分类:', dict(fc))
"
```

### 何时汇报进度

- 用户主动询问时
- 每完成 10% 进度时（可定时轮询）
- 任务结束时（无论成功/失败/中断）

## 决策树

### 任务结束后的处理

```
读取 pdf_conversion_report.md（在目标目录下）
  ↓
查看 failed 数量
  ├─ 0 → 汇报完成，任务结束
  ├─ <10 → 建议用户再跑一次（脚本会自动重试 failed 文件）
  └─ ≥10 → 分析失败原因：
        ├─ 多数「超时」→ 建议调大 PDF_OCR_MAX_TIME（如 1800）后重跑
        ├─ 多数「上传失败」→ 文件本身损坏，列出清单让用户手动处理
        ├─ 多数「验证失败」→ Umi-OCR 可能任务堆积，重启 Umi-OCR 后重跑
        └─ 多数「备份失败」→ 文件被占用，让用户关闭 PDF 阅读器后重跑
```

### 用户要求「继续」时

- **直接重新执行原命令**即可。脚本的状态机会自动：
  - 跳过 `already_dual`/`converted`
  - 直接 OCR `need_ocr`（不重复检测）
  - 延迟重试所有 `failed`
- **不要**删除进度文件（会丢失断点）

### 用户要求「重新开始」时

```bash
# 删除进度文件（需用户确认）
del "%LOCALAPPDATA%\pdf-ocr-dual-layer\progress\<hash>_*.json"
```
- `<hash>` 是目标目录 MD5 前 8 位，可用 Python 计算：
  ```bash
  python -c "import hashlib,os; print(hashlib.md5(os.path.abspath(r'<目标目录>').encode()).hexdigest()[:8])"
  ```

### 排除规则调整

用户说「跳过 XX 文件」时，编辑 `config.ini`：

```ini
[filter]
exclude_patterns = *银行流水*,*流水*,*新增模式*
```

- 排除规则**仅对新扫描的文件生效**
- 已标记为 `excluded` 的不会自动取消
- 已进入 `need_ocr`/`failed` 的不会被排除（需先删进度文件重跑）

## 配置快速参考

### 三级优先级：环境变量 > `config.ini` > 默认值

### 关键配置项（Agent 调整时用环境变量临时覆盖）

| 场景 | 环境变量 | 值 |
|------|---------|-----|
| 大 PDF 超时 | `PDF_OCR_MAX_TIME` | `1800`（30分钟） |
| 上传超时 | `PDF_OCR_UPLOAD_TIMEOUT` | `600` |
| 下载超时 | `PDF_OCR_DOWNLOAD_TIMEOUT` | `600` |
| 调整并发 | `PDF_OCR_MAX_CONCURRENT` | `1`=串行 / `3-12`=并发 |
| 保留备份 | `PDF_OCR_CLEANUP_BACKUP` | `0` |
| 自定义工作目录 | `PDF_OCR_WORK_DIR` | `D:\pdf-ocr` |

### PowerShell 临时覆盖示例

```powershell
$env:PDF_OCR_MAX_TIME = "1800"
$env:PDF_OCR_MAX_CONCURRENT = "5"
python convert_pdfs_to_dual_layer.py "D:\目标目录"
```

## 文件存储架构

| 类型 | 位置 | 是否同步 |
|------|------|---------|
| 主程序（脚本/SKILL.md） | 脚本目录（坚果云） | ✅ |
| 临时缓存 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\cache\` | ❌ |
| 运行日志 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\logs\` | ❌ |
| 进度文件 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\progress\` | ❌ |
| 原 PDF 备份 | `%LOCALAPPDATA%\pdf-ocr-dual-layer\backup\` | ❌ |
| 转换报告 | 目标目录 | 视目录而定 |

> Linux/Mac：`~/.local/share/pdf-ocr-dual-layer/`
> 可通过 `PDF_OCR_WORK_DIR` 覆盖。

## 文件状态机

| 状态 | 含义 | 重启时行为 |
|------|------|-----------|
| `already_dual` | 已检测为双层 | 跳过 |
| `need_ocr` | 已检测需 OCR（中断未完成） | **直接 OCR，跳过文本层检测** |
| `converted` | OCR 转换成功 | 跳过 |
| `failed` | OCR 转换失败 | **延迟批量重试** |
| `skipped` | 检测失败 | 重新检测 |
| `excluded` | 匹配排除规则 | 永久跳过 |

> 关键：检测为需 OCR 时立即保存 `need_ocr`。即使 OCR 中断，下次重启也直接进入 OCR，不重复检测。

## 工作流程

```
扫描目录所有 PDF
  ↓
状态机决策（按已有记录）
  ├─ already_dual / converted / excluded -> 跳过
  ├─ need_ocr                            -> 直接 OCR
  ├─ failed                              -> 跳过（延迟到最后批量重试）
  ├─ skipped                             -> 重新检测
  └─ 无记录                              -> 完整流程
       ↓
PyMuPDF 本地检测文本层（<0.1秒/文件，比例阈值 50%）
  ├─ 有文本层 -> 标记 already_dual，跳过
  └─ 无文本层 -> 标记 need_ocr（先持久化）-> Umi-OCR 转换 -> 验证
       ├─ 验证通过 -> 清理备份 -> 标记 converted
       └─ 验证失败 -> 从备份恢复 -> 标记 failed（延迟批量重试）
  ↓
全部扫描完成 -> 批量重试所有 failed（按原因分类）
  ├─ 超时    -> 2x max_poll_time 重试
  ├─ 文件占用-> 内置 3/6/9s 等待重试
  └─ 其他    -> 普通重试
  ↓
生成 pdf_conversion_report.md（目标目录）
```

## 关键特性

| 特性 | 说明 |
|------|------|
| **状态机驱动** | 6 种文件状态，中断后精准续跑，不重复检测 |
| **断点续跑** | `Ctrl+C` 中断后重新执行，按状态决定是否重试 |
| **并发 OCR** | 12 线程并发上传/下载，threading + logging 线程安全 |
| **失败延迟重试** | 失败文件不阻塞新文件，全部处理完后按原因分类重试 |
| **分类重试策略** | 超时→2x 等待；文件占用→3/6/9s 等待；其余→普通重试 |
| **Umi-OCR 任务清理** | try/finally 确保失败/超时也清理任务，避免服务端堆积 |
| **超长文件名截短** | 超长/含特殊字符文件名自动替换为 `ocr_{hash}.pdf` |
| **download 重试** | 统一指数退避（3×2^attempt，共 5 次） |
| **大文件上传** | `UPLOAD_TIMEOUT` 独立配置 |
| **线程安全日志** | logging 双 handler（控制台 INFO + 文件 DEBUG） |
| **本地检测** | PyMuPDF 抽样检测文本层，秒级判断 |
| **端口自动探测** | 尝试 [1224-1230, 1241] 等端口 |
| **原子写入** | 先写临时文件再替换，中断不损坏原文件 |
| **转换验证** | 验证新 PDF 页数与文本层，异常自动回滚 |
| **原文件备份** | 覆盖前自动备份，验证通过后自动清理 |

## Agent 行为规范

### 必须做
- ✅ 启动前检查 Umi-OCR 进程
- ✅ 长任务用 `blocking: false` 异步运行
- ✅ 用 `CheckCommandStatus` 轮询，不重复 `RunCommand`
- ✅ 启动后立即告知日志/进度文件路径
- ✅ 用户问进度时，读进度文件而非反复轮询终端
- ✅ 任务结束后读取 `pdf_conversion_report.md` 汇报

### 禁止做
- ❌ 擅自启动 `Umi-OCR.exe`（GUI 程序，需用户操作）
- ❌ 擅自删除进度文件（除非用户明确要求「重新开始」）
- ❌ 在同目录多次并行运行脚本（会冲突）
- ❌ 用 `blocking: true` 运行（会阻塞数小时）
- ❌ 修改 `config.ini` 后不告知用户

## 故障排查

| 问题 | Agent 应做 |
|------|-----------|
| Umi-OCR 连接失败 | 检查进程是否运行，告知用户启动 Umi-OCR |
| 探测端口耗时长 | 正常，前 7 个端口超时各 3s，最后连上即可 |
| OCR 转换失败 | 脚本自动分类重试；结束后读报告分析 |
| 验证失败已回滚 | 原文件已自动恢复；备份/恢复均自动重试 |
| 文件被占用 | 脚本内置 3/6/9s 重试；仍失败则告知用户关闭 PDF 阅读器 |
| 大 PDF 超时 | 建议用户设 `PDF_OCR_MAX_TIME=1800` 后重跑 |
| 上传超时 | 建议用户设 `PDF_OCR_UPLOAD_TIMEOUT=600` 后重跑 |
| 进度丢失 | 检查 `%LOCALAPPDATA%\pdf-ocr-dual-layer\progress\` 可写性 |
| 任务堆积 | v1.9.2+ 自动 try/finally 清理；若仍堆积建议重启 Umi-OCR |

## 输出产物

任务结束后在**目标目录**生成：

- `pdf_conversion_report.md` - Markdown 转换报告（汇总统计 + 每文件状态）

本地工作目录保留：

- `logs/convert_<时间戳>_<hash>.log` - 详细运行日志
- `progress/<hash>_pdf_conversion_progress.json` - 进度文件（断点续传）
- `backup/<hash>/<原名>.bak.pdf` - 仅验证失败的备份（成功的已清理）

## 配置文件完整示例

脚本同目录 `config.ini`：

```ini
[server]
try_ports = 1224, 1225, 1226, 1227, 1228, 1229, 1230, 1241

[http]
request_timeout = 30
poll_timeout = 60
download_timeout = 300
# upload_timeout = 300      # 默认取 max(request, download)
poll_interval = 2
max_poll_time = 600

[storage]
work_dir =

[behavior]
backup_original = true
verify_on_success = true
cleanup_backup_on_success = true
text_layer_ratio = 0.5
max_concurrent_ocr = 12

[filter]
exclude_patterns = *银行流水*,*流水*

[ocr]
extraction_mode = mixed                          # mixed/fullPage/ocrOnly
language = models/config_chinese.txt
cls = false
limit_side_len = 960                             # 960/2880/4320/999999
parser = multi_para
```

配置文件查找顺序：
1. 环境变量 `PDF_OCR_CONFIG_FILE` 指定路径
2. 脚本所在目录的 `config.ini`

完整参数说明参见 [Umi-OCR HTTP API 文档](https://github.com/hiroi-sora/Umi-OCR/blob/main/docs/http/api_doc.md)。
