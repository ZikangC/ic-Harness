#!/usr/bin/env python3
"""Use Oh My Pi as the repair agent around the deterministic RTL verifier."""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]


def latest_feedback(task: str) -> Path | None:
    """Return the most recently produced feedback, independent of run-id spelling."""
    candidates = list((PROJECT / "runs").glob(f"{task}-*/feedback.txt"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def read_tail(path: Path, limit: int = 6000) -> str:
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    return text[-limit:]


def recent_iteration_handoff(task: str, count: int = 3, limit: int = 9000) -> str:
    """Extract concise prior-round context without injecting large unified diffs."""
    report = PROJECT / "reports" / task / "progress.md"
    if not report.exists():
        return ""
    text = report.read_text(encoding="utf-8", errors="replace")
    sections = re.split(r"(?=^## 第 \d+ 轮：)", text, flags=re.MULTILINE)
    concise: list[str] = []
    for section in sections:
        if not section.startswith("## 第 "):
            continue
        # write_report appends the potentially large patch after this heading.
        concise.append(section.split("\n## 代码差异", 1)[0].rstrip())
    return "\n\n".join(concise[-count:])[-limit:]


def iteration_history_index(task: str, limit: int = 6000) -> str:
    """Return an all-round, low-token index of tried changes and outcomes."""
    report = PROJECT / "reports" / task / "progress.md"
    if not report.exists():
        return ""
    text = report.read_text(encoding="utf-8", errors="replace")
    sections = re.split(r"(?=^## 第 \d+ 轮：)", text, flags=re.MULTILINE)
    entries: list[str] = []
    for section in sections:
        if not section.startswith("## 第 "):
            continue
        title = section.splitlines()[0]
        changed = re.search(r"## 代码修改\s*\n(.*?)(?=\n## 验证结果)", section, re.DOTALL)
        files = "; ".join(re.findall(r"- (?:修改|新增|删除) `([^`]+)`", changed.group(1) if changed else ""))
        score = re.search(r"## 验证结果\s*\n```json\s*(\{.*?\})\s*```", section, re.DOTALL)
        try:
            result = json.loads(score.group(1)) if score else {}
        except json.JSONDecodeError:
            result = {}
        outcome = []
        if "pass" in result:
            outcome.append(f"pass={result['pass']}")
        if "cos_min" in result:
            outcome.append(f"cos_min={result['cos_min']:.6g}")
        if "argmax_agree" in result:
            outcome.append(f"argmax={result['argmax_agree']:.6g}")
        files = files or "无 rtl/tb 文件变化"
        entries.append(f"- {title.removeprefix('## ')}：{files}；{'，'.join(outcome) or '无可解析评分'}")
    return "\n".join(entries)[-limit:]


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


def first_route_mismatch(path: Path | None) -> int | None:
    """Return the earliest route mismatch position emitted by the auto diagnostic."""
    if path is None or not path.exists():
        return None
    positions = [int(value) for value in re.findall(r"MISMATCH pos=(\d+)",
                                                     path.read_text(encoding="utf-8", errors="replace"))]
    return min(positions) if positions else None


def run_validation(task: str, seed: int, prompt: int, seq_len: int | None, log: Path) -> int:
    """Run the remote verifier; only its compact feedback is brought back locally."""
    command = ["bash", "scripts/remote_round.sh", "--task", task, "--seed", str(seed),
               "--prompt", str(prompt)]
    if seq_len is not None:
        command.extend(["--seq-len", str(seq_len)])
    with log.open("w", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(command) + "\n\n")
        process = subprocess.run(command, cwd=PROJECT, stdout=handle, stderr=subprocess.STDOUT,
                                 text=True, check=False)
    return process.returncode


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
                 attempt_dir: Path, feedback: Path | None, changes: list[str], diff: str,
                 validation_notes: list[str]) -> Path:
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
        *[f"- 验证调度：{note}" for note in validation_notes],
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
    # Keep the human-visible OMP conversation with the structured result.  This
    # is deliberately appended after the diff: recent_iteration_handoff()
    # stops at "代码差异", so a long transcript is not injected into the next
    # DeepSeek prompt.
    agent_log = attempt_dir / "agent.log"
    agent_output = agent_log.read_text(encoding="utf-8", errors="replace").rstrip() if agent_log.exists() else ""
    lines.extend([
        "",
        "## AI 可见工作记录（agent.log 原文）",
        "",
        "~~~~text",
        agent_output or "（本轮没有捕获到 OMP 可见输出。）",
        "~~~~",
    ])
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
    parser.add_argument("--seq-len", type=int, help="only execute the first N causal tokens per validation round")
    parser.add_argument("--adaptive-prefix", action="store_true",
                        help="run a short causal prefix, promote it to full prompt on pass, then shrink to the first route mismatch")
    parser.add_argument("--initial-seq-len", type=int,
                        help="first prefix length for --adaptive-prefix (pos2 debugging uses 3)")
    parser.add_argument("--model", help="optional OMP model selector; omit to use your configured DeepSeek")
    args = parser.parse_args()
    task = args.task.resolve()
    if not task.is_file() or PROJECT not in task.parents:
        raise SystemExit("--task must be a file inside this harness project")
    task_name = task.stem
    task_text = task.read_text(encoding="utf-8")
    if args.initial_seq_len is not None and args.initial_seq_len < 1:
        raise SystemExit("--initial-seq-len must be positive")
    if args.adaptive_prefix and args.seq_len is not None:
        raise SystemExit("use --initial-seq-len with --adaptive-prefix; --seq-len is for a fixed manual scope")
    validation_len = args.initial_seq_len if args.adaptive_prefix else args.seq_len
    # Attempt numbers restart at one for each invocation.  Include a session id
    # so a later --max-attempts run never overwrites prior agent logs/snapshots.
    session_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    mode = "adaptive" if args.adaptive_prefix else (f"prefix{args.seq_len}" if args.seq_len else "full")

    for attempt in range(1, args.max_attempts + 1):
        started = dt.datetime.now().astimezone()
        previous = latest_feedback(task_name)
        evidence = "没有上一轮结果。请先根据任务实现或修复 RTL，再执行验证。"
        if previous:
            data = feedback_summary(previous)
            evidence = f"上一轮评分摘要：\n{json.dumps(data, ensure_ascii=False, indent=2)}"
            evidence += "\n\n服务器限长反馈：\n" + read_tail(previous)
        handoff = recent_iteration_handoff(task_name)
        if handoff:
            evidence += ("\n\n最近最多 3 轮的交接记录（当前 rtl/ 已包含这些修改；先阅读，"
                         "避免重复尝试或无证据地撤回）：\n" + handoff)
        history_index = iteration_history_index(task_name)
        if history_index:
            evidence += ("\n\n全历史极简索引（所有轮次；只用于避免重复已证伪方向，"
                         "以当前 rtl/ 和最新反馈为准）：\n" + history_index)
        scope = (f"前缀 pos0～pos{validation_len - 1}（共 {validation_len} token）"
                 if validation_len is not None else f"完整 prompt {args.prompt}")
        prompt = f"""你正在执行第 {attempt} 轮 reference 驱动的 RTL 修复循环。

任务卡：\n{task_text}

严格边界：
- 只允许修改 {PROJECT}/rtl 和 {PROJECT}/tb。
- 禁止修改 harness/、harness.toml、tasks/、reference 文件或评分器。
- 编辑前先阅读既有 RTL 与 testbench；进行聚焦修复，禁止用占位实现或硬编码测试向量。
- 本轮验证范围：{scope}。
- 不要主动运行 `remote_round.sh`；本轮代码修改结束后，controller 会自动上传、仿真、评分和诊断。
- 只运行会自行结束的有限命令。禁止 `tail -f`、`remote_status.sh --follow`、交互式 shell、无限循环，或任何持续等待日志更新的命令；这些是人工监控功能，会使 agent 工具调用永久阻塞。
- 当 controller 给出完整 prompt 的首个 route 分叉时，下一轮会自动缩回到保留该位置历史状态的最短前缀。
- 完成条件是每个 token 的 cosine >= 0.98、argmax agreement >= 0.99，且状态字有效。
- 所有面向用户的计划、修改说明、失败分析与验证结论必须使用中文；保留工具原始错误文本、代码、文件名和信号名。
- 顶层失败后的紧凑反馈会自动附带 `auto` 诊断。需要更聚焦时，可调用：
  `bash scripts/remote_diagnose.sh --action routes`、`--action snapshots` 或 `--action one-token`。

验证证据：\n{evidence}
"""
        iteration_dir = PROJECT / "runs" / (
            f"agent-{task_name}--{session_id}--p{args.prompt}--{mode}--attempt{attempt}"
        )
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
        validation_notes = [f"执行 {scope}"]
        validation_rc = run_validation(task_name, args.seed, args.prompt, validation_len,
                                       iteration_dir / "validation.log")
        result = latest_feedback(task_name)
        if validation_rc:
            validation_notes.append(f"当前范围验证退出码={validation_rc}")

        # A short prefix passing is not completion.  Immediately run the full
        # selected prompt before giving the agent another edit turn.
        if args.adaptive_prefix and validation_len is not None and result and feedback_summary(result).get("pass"):
            validation_notes.append("前缀通过，自动升级为完整 prompt 回归")
            full_rc = run_validation(task_name, args.seed, args.prompt, None,
                                     iteration_dir / "full_regression.log")
            result = latest_feedback(task_name)
            validation_notes.append(f"完整 prompt 回归退出码={full_rc}")

        # On a full-prompt route failure, run only through that token next time.
        # On a prefix failure, retain its scope unless diagnostics find an even
        # earlier discrete divergence.
        mismatch = first_route_mismatch(result)
        if args.adaptive_prefix and result and not feedback_summary(result).get("pass") and mismatch is not None:
            next_len = mismatch + 1
            if validation_len != next_len:
                validation_notes.append(f"检测到首个 route 分叉 pos{mismatch}；下一轮切换为 {next_len}-token 前缀")
            validation_len = next_len
        report = write_report(task_name, attempt, started, dt.datetime.now().astimezone(), rc,
                              iteration_dir, result, changes, diff, validation_notes)
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
