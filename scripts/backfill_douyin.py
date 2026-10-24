"""抖音评论全量回填驱动器：分批扫描 + 断点续跑 + 自动重试 + 企微播报。

背景：comment_pipeline.run_pipeline 是流式读 + 逐行写，单次进程跑太久会被
MySQL 掐断连接（net_write_timeout=60），且异常路径不会写 summary.json
（run() 的 finally 只关连接，summary 写在 try 里）。本驱动器把全量拆成小批次，
每批一个独立进程，单批失败只影响该批，失败自动缩批重试。

用法（在项目根目录）：.venv/bin/python scripts/backfill_douyin.py
可用环境变量：BACKFILL_BATCH（默认 10000）、WECOM_WEBHOOK
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from comment_pipeline.common.db import connect  # noqa: E402

SRC = "douyin_raw_comments"
HAS_TEXT = "comment_text IS NOT NULL AND CHAR_LENGTH(TRIM(comment_text)) > 0"
BATCH = int(os.getenv("BACKFILL_BATCH", "10000"))
START_WM = int(os.getenv("BACKFILL_START_WM", "0"))
MIN_BATCH = 1000
MAX_RETRY = 3
WEBHOOK = os.getenv("WECOM_WEBHOOK", "").strip()
PY = str(ROOT / ".venv" / "bin" / "python")
LOGDIR = ROOT / "var" / "backfill_logs"
LOGDIR.mkdir(parents=True, exist_ok=True)
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
DRIVER_LOG = LOGDIR / ("backfill_douyin_%s.log" % STAMP)


def log(msg: str) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with DRIVER_LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def notify(content: str) -> None:
    if not WEBHOOK:
        log("未配置 WECOM_WEBHOOK，跳过企微通知")
        return
    payload = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}).encode()
    req = urllib.request.Request(
        WEBHOOK, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log("企微通知 -> HTTP %s" % resp.status)
    except Exception as exc:  # noqa: BLE001
        log("企微通知失败: %s" % exc)


def window(cur, wm: int, limit: int) -> dict:
    """与 iter_source_rows 完全一致的口径，返回本批实际扫描到的 id 上界。"""
    cur.execute(
        "SELECT MAX(id) mx, COUNT(*) n FROM (SELECT id FROM %s "
        "WHERE id > %%s AND %s ORDER BY id ASC LIMIT %%s) t" % (SRC, HAS_TEXT),
        (wm, limit),
    )
    return cur.fetchone()


def outstanding(cur, wm: int) -> int:
    cur.execute(
        "SELECT COUNT(*) n FROM %s WHERE id > %%s AND %s" % (SRC, HAS_TEXT), (wm,)
    )
    return int(cur.fetchone()["n"])


def progress(cur):
    cur.execute(
        "SELECT COUNT(*) n, MAX(source_row_id) mx FROM comment_clean_base WHERE platform=%s",
        ("douyin",),
    )
    row = cur.fetchone()
    return int(row["n"]), (int(row["mx"]) if row["mx"] is not None else 0)


def run_batch(wm: int, limit: int, tag: str) -> int:
    cmd = [
        PY, "-u", "-m", "comment_pipeline.run_pipeline",
        "--min-id", str(wm), "--limit", str(limit),
        "--output-dir", "var/catfood_choice_comment_artifacts",
    ]
    batch_log = LOGDIR / ("batch_%s_%s.log" % (STAMP, tag))
    with batch_log.open("w", encoding="utf-8") as fh:
        fh.write("# " + " ".join(cmd) + "\n")
        fh.flush()
        proc = subprocess.run(cmd, cwd=str(ROOT), stdout=fh, stderr=subprocess.STDOUT)
    return proc.returncode


def main() -> int:
    t_start = time.time()
    conn = connect()
    # 关键：驱动连接必须 autocommit。否则 InnoDB REPEATABLE READ 会让本连接一直读
    # 第一次 SELECT 建立的旧快照，而写入发生在别的子进程里 —— 进度数字会永远不动。
    conn.autocommit(True)
    cur = conn.cursor()
    total = outstanding(cur, 0)
    log("待回填 douyin 评论 = %d 行；批大小 = %d；起始水位 = %d；驱动日志 = %s"
        % (total, BATCH, START_WM, DRIVER_LOG))
    notify(
        "**抖音评论回填已启动**\n> 待处理: <font color=\"info\">%d</font> 行\n"
        "> 批大小: %d 行/批\n> 起始水位: id > %d" % (total, BATCH, START_WM)
    )

    wm = START_WM
    done = 0
    skipped = []
    while True:
        win = window(cur, wm, BATCH)
        if not win["n"]:
            log("没有更多待扫描的源行，主循环结束。")
            break
        target = int(win["mx"])
        rows_in_win = int(win["n"])
        done += 1
        tag = "b%03d_%d_%d" % (done, wm, target)

        ok = False
        attempt_limit = BATCH
        for attempt in range(1, MAX_RETRY + 1):
            log("批 %d: id %d~%d (%d 行, limit=%d) 第 %d 次"
                % (done, wm + 1, target, rows_in_win, attempt_limit, attempt))
            t0 = time.time()
            rc = run_batch(wm, attempt_limit, "%s_a%d" % (tag, attempt))
            log("  退出码=%s 用时=%.1fs" % (rc, time.time() - t0))
            if rc == 0:
                ok = True
                break
            attempt_limit = max(MIN_BATCH, attempt_limit // 2)
            log("  批次失败，缩批到 %d 后重试（写入幂等，可安全重跑）" % attempt_limit)

        if not ok:
            skipped.append("%d~%d" % (wm + 1, target))
            log("  !! 重试仍失败，跳过该窗口继续：%d~%d（末尾统一核对）" % (wm + 1, target))

        wm = target
        cleaned, mx = progress(cur)
        left = outstanding(cur, wm)
        elapsed = time.time() - t_start
        rate = (total - left) / elapsed if elapsed > 0 else 0.0
        eta = (left / rate / 60.0) if rate > 0 else 0.0
        log("累计已清洗=%d 已扫到 id=%d 剩余=%d 均速=%.0f 行/秒 预计剩余=%.1f 分钟"
            % (cleaned, mx, left, rate, eta))

    cleaned, mx = progress(cur)
    cur.execute(
        "SELECT COUNT(*) n FROM %s r WHERE %s "
        "AND NOT EXISTS(SELECT 1 FROM comment_clean_base b "
        "WHERE b.platform='douyin' AND b.source_row_id=r.id)"
        % (SRC, HAS_TEXT.replace("comment_text", "r.comment_text"))
    )
    still_missing = int(cur.fetchone()["n"])
    elapsed = time.time() - t_start
    conn.close()

    summary = (
        "**抖音评论回填完成**\n"
        "> 用时: <font color=\"info\">%.1f</font> 分钟\n"
        "> 批次数: %d\n"
        "> 已清洗 douyin 行数: <font color=\"info\">%d</font>\n"
        "> 仍未覆盖(噪音+失败): <font color=\"warning\">%d</font>\n"
        "> 失败窗口: %s" % (elapsed / 60.0, done, cleaned, still_missing, skipped or "无")
    )
    log(summary.replace("\n", " | "))
    notify(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
