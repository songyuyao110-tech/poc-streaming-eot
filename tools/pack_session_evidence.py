#!/usr/bin/env python3
"""一键打包双席会话取证包（对齐方案 §4.3-2，纯标准库，任何 python3 都能跑）。

把一次双席回放落盘的三样东西装进一个 .tgz：
  - <session_id>-D/             D 席 timeline.json（config + events + turns + summary）
  - <session_id>-P/             P 席 timeline.json
  - <session_id>/duo_summary.json   合并层（duo_done.summary，含 brain 任务账；若已落盘）
  - 可选的问题卡 .md（--issue-file 给出则顶层打包进去）

用法：
  python3 tools/pack_session_evidence.py <session_id> [--issue N] [--issue-file 卡.md] [--out DIR]

session_id 取 `duo-ui-YYYYMMDD-HHMMSS-xxxxxx`（去掉 -D/-P 后缀，`duo_accepted` 帧或 UI 里都有）。

退出码：0 = 打包成功；2 = 找不到任何可打包的东西。
"""
from __future__ import annotations

import argparse
import json
import sys
import tarfile
from pathlib import Path

POC_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_ROOT = POC_ROOT / "evidence" / "turn-session"
DEFAULT_OUT = POC_ROOT / "tmp"


def main() -> int:
    parser = argparse.ArgumentParser(description="打包双席会话取证包")
    parser.add_argument("session_id", help="如 duo-ui-20261104-165000-f22c82（去掉 -D/-P 后缀）")
    parser.add_argument("--issue", default="", help="问题编号（可选）；产包名 issue-<N>-<sid>.tgz")
    parser.add_argument("--issue-file", default="", help="问题卡 .md 路径（可选，顶层打包进去）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出目录（默认 %s）" % DEFAULT_OUT)
    parser.add_argument("--evidence-root", default=str(DEFAULT_EVIDENCE_ROOT),
                        help="evidence/turn-session 根目录（默认 %s）" % DEFAULT_EVIDENCE_ROOT)
    args = parser.parse_args()

    sid = args.session_id.strip()
    if not sid:
        parser.error("session_id 不能为空")
    evidence_root = Path(args.evidence_root).expanduser()
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    members = []  # (磁盘路径, 归档内相对路径)
    for suffix in ("-D", "-P"):
        seat_dir = evidence_root / (sid + suffix)
        if (seat_dir / "timeline.json").is_file():
            members.append((seat_dir, sid + suffix))
    duo_summary = evidence_root / sid / "duo_summary.json"
    if duo_summary.is_file():
        members.append((duo_summary, str(Path(sid) / "duo_summary.json")))
    if args.issue_file:
        card = Path(args.issue_file).expanduser()
        if not card.is_file():
            print("警告：问题卡不存在，跳过：%s" % card, file=sys.stderr)
        else:
            members.append((card, card.name))

    if not members:
        print("找不到 %s 的任何 -D/-P timeline 或 duo_summary.json（evidence_root=%s）"
              % (sid, evidence_root), file=sys.stderr)
        return 2

    name = ("issue-%s-%s" % (args.issue, sid)) if args.issue else ("session-%s" % sid)
    out_path = out_dir / (name + ".tgz")
    with tarfile.open(out_path, "w:gz") as tf:
        for disk, arc in members:
            tf.add(disk, arcname=arc)
    print(json.dumps({"ok": True, "archive": str(out_path),
                      "members": [arc for _, arc in members]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
