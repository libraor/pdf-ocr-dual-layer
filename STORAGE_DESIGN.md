# 文件存储解决方案设计文档

> 适用范围：`pdf-ocr-dual-layer` skill
> 目标：兼顾数据安全与同步效率，主程序同步多设备，临时/进度/备份本地化

## 一、设计目标

| 目标 | 要求 |
|------|------|
| 主程序同步 | 脚本/SKILL.md/依赖清单存放坚果云，多设备共享 |
| 临时文件隔离 | 缓存、日志、中间产物排除在同步范围外 |
| 进度可靠 | 断点续传文件妥善保存，不被同步冲突破坏 |
| 备份安全 | 原 PDF 备份不存放坚果云，避免同步占用与隐私泄露 |
| 跨设备兼容 | 同一脚本可在 Windows/Mac/Linux 运行 |
| 可回滚 | 误覆盖或新文件异常时能从备份恢复 |
| 空间高效 | 验证通过后立即清理备份，避免硬盘空间长期占用 |

---

## 二、方案：本地专用目录 + 验证即清理策略

### 1. 目录结构设计

```
坚果云同步目录（主程序，跨设备同步）:
  d:\坚果云\GitHub\Skills\pdf-ocr-dual-layer\
    ├── SKILL.md
    ├── convert_pdfs_to_dual_layer.py
    ├── requirements.txt
    └── STORAGE_DESIGN.md

本地专用目录（不同步，每台设备独立）:
  Windows: %LOCALAPPDATA%\pdf-ocr-dual-layer\
  Linux:   ~/.local/share/pdf-ocr-dual-layer/
  Mac:     ~/Library/Application Support/pdf-ocr-dual-layer/
  或通过环境变量 PDF_OCR_WORK_DIR 指定
    ├── cache\               # OCR 临时下载文件
    │   └── <task_id>.tmp.pdf
    ├── logs\                # 运行日志
    │   └── convert_<时间戳>_<hash8>.log
    ├── progress\            # 进度文件（按目标目录 hash 隔离）
    │   └── <hash8>_pdf_conversion_progress.json
    └── backup\              # 原 PDF 备份（验证通过后自动清理）
        └── <hash8>\
            └── <原文件名>.bak.pdf
```

### 2. 同步排除规则配置方法

**关键设计：临时文件根本不放在同步目录**，无需配置排除规则。但若用户自定义 `PDF_OCR_WORK_DIR` 到同步目录，需手动排除。

#### 坚果云客户端排除（可选）

坚果云客户端 -> 设置 -> 同步设置 -> 高级设置 -> 排除目录/文件类型，添加：

```
pdf-ocr-dual-layer\cache\
pdf-ocr-dual-layer\logs\
pdf-ocr-dual-layer\progress\
pdf-ocr-dual-layer\backup\
*.tmp.pdf
*.bak.pdf
```

#### 通过 .gitignore（若用 Git 同步）

```
# .gitignore（仅当 PDF_OCR_WORK_DIR 设为仓库内时需要）
pdf-ocr-dual-layer/cache/
pdf-ocr-dual-layer/logs/
pdf-ocr-dual-layer/progress/
pdf-ocr-dual-layer/backup/
*.tmp.pdf
*.bak.pdf
```

### 3. 备份文件命名规范

| 类型 | 命名模板 | 示例 |
|------|---------|------|
| 进度文件 | `<hash8>_pdf_conversion_progress.json` | `a1b2c3d4_pdf_conversion_progress.json` |
| 日志文件 | `convert_<YYYYMMDD_HHMMSS>_<hash8>.log` | `convert_20260725_143022_a1b2c3d4.log` |
| 备份文件 | `<原文件名>.bak.pdf` | `report.bak.pdf` |
| 临时文件 | `<task_id>.tmp.pdf` | `abc123.tmp.pdf` |

- `hash8` = `md5(目标目录绝对路径)[:8]`，确保不同目录互不干扰
- 时间戳格式 `YYYYMMDD_HHMMSS` 字典序即时间序，便于排序

### 4. 断点续传实现逻辑

```python
# 进度文件路径由目标目录 hash 决定（同目录多次运行复用）
progress_path = work_dir / "progress" / f"{hash8}_pdf_conversion_progress.json"

# 加载：读取已处理文件列表
progress = load_progress(progress_path)
processed_files = {r["file"] for r in progress["records"]}

# 主循环：跳过已处理
for pdf in find_pdfs(target_dir):
    rel_path = pdf.relative_to(target_dir)
    if rel_path in processed_files:
        continue  # 跳过
    # ... 处理 ...

    # 每个文件处理完立即原子保存（先写 .tmp 再 rename）
    save_progress(progress_path, {
        "records": records,
        "stats": stats,
        "target_dir": target_dir,
        "version": __version__,
    })

# Ctrl+C 中断时：移除未完成文件的记录，确保下次重跑
```

**保障措施**：
- 每处理完一个文件立即保存（不是批处理末尾）
- 临时文件 + 原子替换（`tmp_path.replace(progress_path)`）
- 中断时移除正在处理的文件记录，避免误判已处理

### 5. 验证与清理机制（核心保障）

为避免 OCR 输出异常文件覆盖原文件后无法挽回，同时避免备份长期占用硬盘空间，采用「**验证通过即清理、失败即回滚**」策略。

#### 流程

```
OCR 下载完成
  ↓
verify_converted_pdf(pdf_path, original_page_count)
  ├─ 验证失败 → restore_from_backup() → 标记转换失败
  └─ 验证通过 → cleanup_backup()      → 标记转换成功
```

#### 验证项（`verify_converted_pdf`）

| 检查项 | 判定 | 失败处理 |
|--------|------|---------|
| 文件存在 | 不存在即失败 | 回滚 |
| 文件大小 | < 100 字节即失败 | 回滚 |
| PyMuPDF 打开 | 抛异常即失败 | 回滚 |
| 页数 | 与原文件不一致即失败 | 回滚 |
| 文本层（前 3 页抽样） | 全部无文本即失败 | 回滚 |

> 未安装 PyMuPDF 时仅做基本大小检查，记录 info 日志。

#### 回滚（`restore_from_backup`）

```python
shutil.copy2(backup_path, pdf_path)  # 用备份覆盖回原位置
```

#### 清理（`cleanup_backup`）

```python
backup_path.unlink()  # 验证通过后立即删除备份
```

#### 配置开关

| 环境变量 | 默认 | 说明 |
|---------|------|------|
| `PDF_OCR_VERIFY` | `1` | 转换后是否验证新 PDF（关闭则不验证也不清理） |
| `PDF_OCR_CLEANUP_BACKUP` | `1` | 验证通过后是否清理备份（关闭则保留备份用于回滚） |
| `PDF_OCR_BACKUP` | `1` | 是否在覆盖前备份原文件 |

**配置组合行为**：

| BACKUP | VERIFY | CLEANUP | 行为 |
|--------|--------|---------|------|
| 1 | 1 | 1 | 默认：备份→OCR→验证→通过清理/失败回滚 |
| 1 | 1 | 0 | 备份保留：验证通过也保留备份，可手动回滚 |
| 1 | 0 | - | 不验证：OCR 完即视为成功，备份保留（不清理） |
| 0 | - | - | 不备份：直接覆盖原文件，**风险自负** |

### 6. 数据安全保障措施

| 风险 | 保障 |
|------|------|
| OCR 中断损坏原文件 | 先写 `cache/<task_id>.tmp.pdf` 再原子 `replace` |
| 进度文件写入中断 | 临时文件 + `replace` 原子替换 |
| 误覆盖原 PDF | OCR 前自动 `shutil.copy2` 到 backup 目录 |
| 新 PDF 异常（页数错/无文本层） | 验证失败自动从备份恢复原文件 |
| 备份目录被误删 | 备份在 `%LOCALAPPDATA%`，与同步目录物理隔离 |
| 备份长期占用空间 | 验证通过后立即清理，仅失败时保留 |
| 进度被同步冲突破坏 | 进度文件不放在同步目录 |
| 长时间 OCR 无反馈 | 每 60 秒打印等待时长，日志详细记录 |
| 文件被占用 | 单独捕获 `PermissionError`，明确提示 |
| 任务残留 | `clear_task` 失败时记录 warning 日志 |
| 备份失败但要求备份 | 终止转换以保护原文件，不冒险覆盖 |

### 7. 空间占用评估

| 场景 | 单文件占用 | 说明 |
|------|-----------|------|
| 转换成功 | 0（备份已清理） | 仅原 PDF + 新 PDF |
| 转换失败 | 1 份原文件备份 | 自动保留用于人工排查 |
| 转换中断 | 1 份原文件备份 + 临时文件 | 临时文件下次启动可能残留 |
| 缓存临时文件 | 1 份 OCR 输出 | 下载完成立即替换或清理 |

#### 临时缓存清理

`cache/<task_id>.tmp.pdf` 在 `download_result` 中通过 `try/finally` 保证清理，即使下载失败也不会残留。

#### 旧备份定期清理（可选）

转换失败的备份会保留。如需定期清理超过 N 天的失败备份：

```powershell
# 删除 30 天前的失败备份
$backupDir = "$env:LOCALAPPDATA\pdf-ocr-dual-layer\backup"
Get-ChildItem $backupDir -Recurse -Filter "*.bak.pdf" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
    Remove-Item -Force
```

可通过 Windows 任务计划程序每周执行一次。

---

## 三、实施步骤与操作指南

### 步骤 1：依赖安装

```bash
cd "d:\坚果云\GitHub\Skills\pdf-ocr-dual-layer"
pip install -r requirements.txt
```

### 步骤 2：验证本地工作目录可写

```powershell
$workDir = "$env:LOCALAPPDATA\pdf-ocr-dual-layer"
New-Item -ItemType Directory -Force -Path "$workDir\cache","$workDir\logs","$workDir\progress","$workDir\backup"
"test" | Out-File "$workDir\progress\test.txt"
Test-Path "$workDir\progress\test.txt"  # 应返回 True
Remove-Item "$workDir\progress\test.txt"
```

### 步骤 3：执行首次转换

```bash
python "d:\坚果云\GitHub\Skills\pdf-ocr-dual-layer\convert_pdfs_to_dual_layer.py" "D:\我的PDF目录"
```

正常输出示例：
```
[1/10] report.pdf
  🔍 检测文本层... ❌ 无文本层，开始 OCR...
  🔄 OCR 转换中... ✅ 转换成功
  ✅ 验证通过
```

### 步骤 4：验证产物

```powershell
# 检查报告
Test-Path "D:\我的PDF目录\pdf_conversion_report.md"

# 检查进度文件
Get-ChildItem "$env:LOCALAPPDATA\pdf-ocr-dual-layer\progress\"

# 检查备份目录（应仅包含失败文件的备份，成功的已清理）
Get-ChildItem "$env:LOCALAPPDATA\pdf-ocr-dual-layer\backup\" -Recurse

# 检查日志
Get-ChildItem "$env:LOCALAPPDATA\pdf-ocr-dual-layer\logs\"
```

### 步骤 5：（可选）配置策略调整

```powershell
# 关闭自动清理备份（保留所有备份用于回滚）
setx PDF_OCR_CLEANUP_BACKUP "0"

# 关闭验证（不推荐，仅用于信任 OCR 输出的场景）
setx PDF_OCR_VERIFY "0"

# 完全关闭备份（不推荐，原文件将被直接覆盖）
setx PDF_OCR_BACKUP "0"

# 自定义工作目录到 D 盘
setx PDF_OCR_WORK_DIR "D:\pdf-ocr-work"
```

设置后需重启终端生效。

### 步骤 6：（可选）配置定时清理失败备份

参考上文章节「旧备份定期清理」中的 PowerShell 命令，配合 Windows 任务计划程序每周执行。

---

## 四、监控与维护

### 日常检查项

| 检查项 | 命令 | 频率 |
|--------|------|------|
| 备份目录大小 | `Get-ChildItem "$env:LOCALAPPDATA\pdf-ocr-dual-layer\backup" -Recurse \| Measure-Object Length -Sum` | 每月 |
| 日志目录大小 | 同上，换 `logs` | 每月 |
| 旧备份清理 | 见「旧备份定期清理」 | 每周 |
| 进度文件堆积 | `Get-ChildItem "$env:LOCALAPPDATA\pdf-ocr-dual-layer\progress\"` | 每季度 |

### 灾难恢复

| 场景 | 恢复方法 |
|------|---------|
| 误覆盖 PDF（备份保留时） | 从 `backup\<hash>\<原文件名>.bak.pdf` 复制回原位置 |
| 转换失败 | 自动已回滚，无需操作；如需手动重试，删除进度文件中该条记录 |
| 进度文件损坏 | 删除该进度文件，重跑（已成功转换的 PDF 会被识别为双层而跳过） |
| 工作目录被删 | 重新运行脚本，自动重建目录结构 |
| 验证逻辑误判 | 临时设置 `PDF_OCR_VERIFY=0` 重跑（保留备份手动确认） |

---

## 五、迁移与回滚

### 从旧版迁移

旧版进度文件位于目标目录下 `.pdf_conversion_progress.json`，迁移到新位置：

```powershell
$oldProgress = "D:\PDF目录\.pdf_conversion_progress.json"
if (Test-Path $oldProgress) {
    # 计算 hash（需要 PowerShell 5+）
    $absPath = "D:\PDF目录"
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $hash = [System.BitConverter]::ToString($md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($absPath))).Replace("-","").ToLower().Substring(0,8)
    $newPath = "$env:LOCALAPPDATA\pdf-ocr-dual-layer\progress\${hash}_pdf_conversion_progress.json"
    Move-Item $oldProgress $newPath
    Write-Host "已迁移进度文件到 $newPath"
}
```

### 回滚到旧版

1. 把进度文件移回目标目录并改名为 `.pdf_conversion_progress.json`
2. 用 git 回退脚本到旧版
3. 删除 `%LOCALAPPDATA%\pdf-ocr-dual-layer\`（可选）

---

## 六、变更日志

| 版本 | 日期 | 变更 |
|------|------|------|
| 1.1.0 | 2026-07-25 | 实现本地专用目录架构、备份机制、进度隔离 |
| 1.2.0 | 2026-07-25 | 新增转换后验证、失败自动回滚、验证通过自动清理备份 |
