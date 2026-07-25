"""效率分析"""
import json
from pathlib import Path
from datetime import datetime

p = Path.home() / "AppData" / "Local" / "pdf-ocr-dual-layer" / "progress"
f = sorted(p.glob("*.json"), key=lambda x: x.stat().st_mtime)[-1]
d = json.loads(f.read_text(encoding="utf-8"))

# 查看第一条记录的结构
if d["records"]:
    print("=== 记录结构示例 ===")
    print(json.dumps(d["records"][0], ensure_ascii=False, indent=2))
    print()

# 统计
counts = {}
for r in d["records"]:
    counts[r["status"]] = counts.get(r["status"], 0) + 1

total = d["stats"].get("total", 0)
processed = len(d["records"])

print(f"=== 进度 ===")
print(f"总文件数: {total}")
print(f"已处理: {processed} ({processed*100//total if total else 0}%)")

print(f"\n=== 状态分布 ===")
for k, v in sorted(counts.items()):
    print(f"  {k}: {v}")

# 进度文件修改时间
mtime = datetime.fromtimestamp(f.stat().st_mtime)
ctime = datetime.fromtimestamp(f.stat().st_ctime)
print(f"\n进度文件创建: {ctime.strftime('%H:%M:%S')}")
print(f"进度文件更新: {mtime.strftime('%H:%M:%S')}")
