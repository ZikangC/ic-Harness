#!/usr/bin/env python3
"""Use Oh My Pi as the repair agent around the deterministic RTL verifier."""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


def latest_feedback(task: str) -> Path | None:
    candidates = sorted((PROJECT / "runs").glob(f"{task}-*/feedback.txt"))
    return candidates[-1] if candidates else None


def read_tail(path: Path, limit: int = 6000) -> str:
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return text[-limit:]


def feedback_summary(path: Path) -> dict:
    """Extract the small server-side score JSON from remote_round feedback."""
    text = path.read_text(encoding="utf-8", errors="replace")
    begin = text.find("__HARNESS_SUMMARY_BEGIN__")
    end = text.find("__HARNESS_SUMMARY_END__")
    if begin < 0 or end < begin:
        return {"pass": False, "reason": "远程反馈中没有评分摘要"}
    payload = text[begin + len("__HARNESS_SUMMARY_BEGIN__"):end].strip()
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        return {"pass": False, "reason": f"远程评分摘要不是有效 JSON：{error}"}


def snapshot_candidate(destination: Path) -> None:
    """Preserve the RTL/TB tree around one agent turn for an auditable diff."""
    for name in ("rtl", "tb"):
        source = PROJECT / name
        if source.is_dir():
            shutil.copytree(source, destination / name, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))


def candidate_diff(before: Path, after: Path) -> tuple[list[str], str]:
    """Return changed paths plus a unified text diff for the two snapshots."""
    before_files = {path.relative_to(before) for path in before.rglob("*") if path.is_file()}
    after_files = {path.relative_to(after) for path in after.rglob("*") if path.is_file()}
    changes: list[str] = []
    pieces: list[str] = []
    for relative in sorted(before_files | after_files):
        left, right = before / relative, after / relative
        if not left.exists():
            kind = "新增"
        elif not right.exists():
            kind = "删除"
        elif left.read_bytes() == right.read_bytes():
            continue
        else:
            kind = "修改"
        changes.append(f"{kind} `{relative}`")
        if left.exists() and right.exists() and left.stat().st_size < 2_000_000 and right.stat().st_size < 2_000_000:
            old = left.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            new = right.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            pieces.extend(difflib.unified_diff(old, new, fromfile=f"before/{relative}",
                                               tofile=f"after/{relative}"))
    return changes, "".join(pieces)


def write_report(task: str, attempt: int, started: dt.datetime, ended: dt.datetime, omp_rc: int,
                 attempt_dir: Path, feedback: Path | None, changes: list[str], diff: str) -> Path:
    report_dir = PROJECT / "reports" / task
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / "progress.md"
    summary = feedback_summary(feedback) if feedback else {"pass": False, "reason": "本轮没有远程反馈"}
    lines = []
    if not report.exists():
        lines.extend([f"# RTL Harness 迭代记录：{task}", ""])
    lines.extend([
        f"## 第 {attempt} 轮：{started.astimezone():%Y-%m-%d %H:%M:%S %Z}",
        "",
        "## 执行信息",
        "",
        f"- 开始时间：{started.astimezone().isoformat(timespec='seconds')}",
        f"- 结束时间：{ended.astimezone().isoformat(timespec='seconds')}",
        f"- OMP 退出码：`{omp_rc}`",
        f"- AI 日志：`{attempt_dir / 'agent.log'}`",
        "",
        "## 代码修改",
        "",
    ])
    lines.extend([f"- {item}" for item in changes] or ["- 本轮未检测到 `rtl/` 或 `tb/` 的文件变化。"])
    lines.extend([
        "",
        "## 验证结果",
        "",
        "```json",
        json.dumps(summary, ensure_ascii=False, indent=2),
        "```",
    ])
    if feedback:
        lines.extend(["", f"- 紧凑服务器反馈：`{feedback}`"])
    if diff:
        lines.extend(["", "## 代码差异", "", "```diff", diff.rstrip(), "```"])
    with report.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n\n")
    (attempt_dir / "changes.diff").write_text(diff, encoding="utf-8")
    return report


def call_omp(prompt: str, log: Path, model: str | None) -> int:
    command = ["omp", "-p", "--cwd", str(PROJECT)]
    if model:
        command.extend(["--model", model])
    command.append(prompt)
    with log.open("w", encoding="utf-8") as handle:
        process = subprocess.run(command, cwd=PROJECT, stdout=handle, stderr=subprocess.STDOUT,
                                 text=True, check=False)
    return process.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260706)
    parser.add_argument("--prompt", type=int, choices=(0, 1), default=1)
    parser.add_argument("--model", help="optional OMP model selector; omit to use your configured DeepSeek")
    args = parser.parse_args()
    task = args.task.resolve()
    if not task.is_file() or PROJECT not in task.parents:
        raise SystemExit("--task must be a file inside this harness project")
    task_name = task.stem
    task_text = task.read_text(encoding="utf-8")

    for attempt in range(1, args.max_attempts + 1):
        started = dt.datetime.now().astimezone()
        previous = latest_feedback(task_name)
        evidence = "没有上一轮结果。请先根据任务实现或修复 RTL，再执行验证。"
        if previous:
            data = feedback_summary(previous)
            evidence = f"上一轮评分摘要：\n{json.dumps(data, ensure_ascii=False, indent=2)}"
            evidence += "\n\n服务器限长反馈：\n" + read_tail(previous)
        prompt = f"""你正在执行第 {attempt} 轮 reference 驱动的 RTL 修复循环。

任务卡：\n{task_text}

严格边界：
- 只允许修改 {PROJECT}/rtl 和 {PROJECT}/tb。
- 禁止修改 harness/、harness.toml、tasks/、reference 文件或评分器。
- 编辑前先阅读既有 RTL 与 testbench；进行聚焦修复，禁止用占位实现或硬编码测试向量。
- 最后必须执行：bash scripts/remote_round.sh --task {task_name} --seed {args.seed} --prompt {args.prompt}
- 此命令只上传必要验证文件，在服务器 k3demo-eda 容器内执行 Python/reference/仿真，再下载本轮日志与摘要。
- 完成条件是每个 token 的 cosine >= 0.98、argmax agreement >= 0.99，且状态字有效。
- 所有面向用户的计划、修改说明、失败分析与验证结论必须使用中文；保留工具原始错误文本、代码、文件名和信号名。

验证证据：\n{evidence}
"""
        iteration_dir = PROJECT / "runs" / f"agent-{task_name}-{attempt}"
        iteration_dir.mkdir(parents=True, exist_ok=True)
        before = iteration_dir / "before"
        after = iteration_dir / "after"
        shutil.rmtree(before, ignore_errors=True)
        shutil.rmtree(after, ignore_errors=True)
        snapshot_candidate(before)
        rc = call_omp(prompt, iteration_dir / "agent.log", args.model)
        snapshot_candidate(after)
        changes, diff = candidate_diff(before, after)
        if rc:
            print(f"attempt {attempt}: omp exited {rc}; log={iteration_dir / 'agent.log'}", file=sys.stderr)
        result = latest_feedback(task_name)
        report = write_report(task_name, attempt, started, dt.datetime.now().astimezone(), rc,
                              iteration_dir, result, changes, diff)
        print(f"第 {attempt} 轮 Markdown 记录：{report}")
        if result:
            summary = feedback_summary(result)
            if summary.get("pass"):
                print(f"PASS on attempt {attempt}: {result}")
                return 0
        print(f"attempt {attempt} did not pass; continuing")
    print(f"FAILED after {args.max_attempts} attempts; inspect {PROJECT / 'runs'}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
