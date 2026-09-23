#!/usr/bin/env python3
"""One Pro guidance round, isolated from controller scheduling and execution."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from project_adapter_api import decision_error
from sta_evidence_extract import PPA_RUN_ID_RE


MAX_FLASH_ATTEMPTS = 6
PRO_DECISION_PROMPT_MAX_CHARS = 200000
RUN_EVIDENCE_ACTION = "inspect_run_evidence"
EXPAND_BLACKBOX_ACTION = "expand_blackbox"


class ProGuidanceError(RuntimeError):
    """Base failure for one guidance round."""

    def __init__(self, message: str, *, raw_output: str = "") -> None:
        super().__init__(message)
        self.raw_output = raw_output


class ProGuidanceInputError(ProGuidanceError):
    """The immutable DecisionPacket cannot be rendered safely."""


class ProModelInvocationError(ProGuidanceError):
    """The Pro model process failed before producing an accepted decision."""

    def __init__(self, exit_code: int, *, raw_output: str) -> None:
        super().__init__(f"Pro model exited {exit_code}", raw_output=raw_output)
        self.exit_code = exit_code


class ProOutputError(ProGuidanceError):
    """The Pro response does not contain one syntactically valid decision."""


class ProDecisionRejected(ProGuidanceError):
    """The parsed decision violates its immutable DecisionPacket."""

    def __init__(self, message: str, *, raw_output: str,
                 decision: Mapping[str, object]) -> None:
        super().__init__(message, raw_output=raw_output)
        self.decision = dict(decision)


@dataclass(frozen=True)
class ProGuidanceResult:
    """Accepted output of one Pro guidance round."""

    decision: Dict[str, object]
    raw_output: str


def task_constraints() -> str:
    """Stable Task-02 guardrails needed for strategic guidance."""
    return """目标：对已有功能基本正确的 RTL 用 Yosys/ABC/Nangate45 获得可信 PPA/STA 证据，并只在功能不回归前提下迭代。
硬边界：不读取/同步/修改 nano-kpu；服务器 runs 是不可变证据库；不得把分区 ABC proxy 写成完整芯片 Fmax；无 RTL 改动时不得虚构功能/PPA结果。
策略：每轮一个可证伪因果假设；local 至多一个 RTL 文件且可 targeted PPA，architecture 是原子跨文件变更且强制完整回归/full PPA；脚本/测量 harness 改动且无 RTL 改动走单网表 smoke（controller 跑 planner 显式声明且通过校验的合法分区：一个分区时 smoke，多个时 targeted），不重复完整分区 PPA；纯功能/计数层假设（无 RTL 改动且无 harness 改动）只走 16-token 功能计数点并回报 C(n)/m，禁止调度任何分区 PPA；RTL 改动至少 16-token，高风险控制/数据通路还要 prompt-0/48-token。
测量：预算只允许 180/600/1800/3600/7200 秒；相同测量配置且没有新证据时只能选择严格更高且不超过上限的预算；有黑盒/未链接单元的 STA 必须标记 PARTIAL_HIERARCHICAL。closure_measurement_incomplete 表示 timeout/truncation 而非导出缺陷；达到最大预算边界后转为 closure_resource_blocked，禁止继续相同测量并要求改变测量策略或停止调试。Closure Skill 不授权 RTL 优化；closure_repair_required 才要求 repair_harness/revise_task_card。
阶段切换：当 sta_link_result=ok、sta_timing_measured=yes 且功能回归通过时，即使整体仍为 PARTIAL_HIERARCHICAL，也允许并优先考虑逐个展开 top_level_sta 的计算黑盒。展开必须一次只选一个当前黑盒，由 controller 修改 ppa.toml 并强制一次 targeted top_level_sta 测量；不得把“仍有其他黑盒”作为继续修通用 harness 的理由。"""


def parse_decision(text: str) -> Dict[str, object]:
    """Parse the newest syntactically supported decision from Pro output."""
    matches = re.findall(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if not matches:
        raise ValueError("Pro reply lacks a fenced JSON decision")
    for match in reversed(matches):
        try:
            value = json.loads(match)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        action = value.get("action")
        if not isinstance(action, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", action):
            continue
        attempts = value.get("flash_attempts", 0)
        if (not isinstance(attempts, int) or isinstance(attempts, bool)
                or not 0 <= attempts <= MAX_FLASH_ATTEMPTS):
            raise ValueError(f"flash_attempts must be in 0..{MAX_FLASH_ATTEMPTS}")
        if "flash_directive" in value:
            raise ValueError("flash_directive was removed; use the immutable campaign fields")
        request = value.get("evidence_request")
        if action == RUN_EVIDENCE_ACTION:
            if not isinstance(request, dict):
                raise ValueError("inspect_run_evidence requires an evidence_request object")
            if set(request) != {"run_id", "artifact", "selector", "max_lines"}:
                raise ValueError(
                    "evidence_request must contain only run_id, artifact, selector, max_lines")
            if request.get("artifact") != "sta.log":
                raise ValueError("inspect_run_evidence currently permits only artifact=sta.log")
            if not isinstance(request.get("run_id"), str) or not isinstance(
                    request.get("selector"), str):
                raise ValueError("evidence_request run_id and selector must be strings")
            if not isinstance(request.get("max_lines"), int):
                raise ValueError("evidence_request max_lines must be an integer")
            if (request["run_id"] != "latest_measured_sta"
                    and not PPA_RUN_ID_RE.fullmatch(request["run_id"])):
                raise ValueError(
                    "evidence_request run_id must be latest_measured_sta or a Task-02 PPA run")
        knowledge = value.get("reusable_knowledge")
        if knowledge not in {"append", "none"}:
            raise ValueError("Pro must declare reusable_knowledge as append or none")
        if action == "record_reusable_knowledge" and knowledge != "append":
            raise ValueError("record_reusable_knowledge requires reusable_knowledge=append")
        if action == EXPAND_BLACKBOX_ACTION:
            module = value.get("module")
            if not isinstance(module, str) or not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", module):
                raise ValueError("expand_blackbox requires one valid module name")
        return value
    raise ValueError("Pro reply contains no supported JSON decision")


def build_prompt(decision_packet: Mapping[str, object]) -> str:
    """Render the bounded strategic interface from one immutable packet."""
    identity = decision_packet.get("identity")
    evidence = decision_packet.get("evidence")
    allowed_actions = decision_packet.get("allowed_actions")
    if not isinstance(identity, Mapping) or not isinstance(evidence, Mapping):
        raise RuntimeError("Pro DecisionPacket is missing identity or evidence")
    if not isinstance(allowed_actions, Mapping) or not allowed_actions:
        raise RuntimeError("Pro DecisionPacket contains no allowed action")
    rendered_packet = json.dumps(decision_packet, ensure_ascii=False, indent=2)
    prompt = f"""你是 Task 02 的上层 Pro 战略模型，只在执行批次边界做选择；不得运行命令或修改文件。

任务硬边界：
{task_constraints()}

下面是本轮唯一 DecisionPacket。`evidence` 是 Evidence Model 整理并经 validator 校验的当前阶段证据；
`allowed_actions` 来自确定性 Skill 和项目 adapter，逐项列出了允许参数。不得选择列表外动作，
不得改变 transaction_id、generation 或 packet_fingerprint。

```json
{rendered_packet}
```

先用中文给出简洁、证据化的战略判断，最后只输出一个 fenced JSON：
```json
{{
  "transaction_id": {json.dumps(identity.get("transaction_id"), ensure_ascii=False)},
  "generation": {json.dumps(identity.get("generation"), ensure_ascii=False)},
  "packet_fingerprint": {json.dumps(identity.get("packet_fingerprint"), ensure_ascii=False)},
  "action": "逐字选择 allowed_actions 的一个键",
  "module": "仅在所选动作 parameter_schema 要求时填写",
  "flash_attempts": 0,
  "rationale": "真实证据依据",
  "reusable_knowledge": "append|none"
}}
```

所有参数必须符合所选动作的 `parameter_schema`。`inspect_run_evidence` 还必须提供原有
`evidence_request` 对象：run_id、artifact=sta.log、selector 和 max_lines。没有该请求时省略字段。
只有 `campaign_policy.selection=pro` 的 Flash 动作，才在同一个 JSON 对象中额外填写
`campaign_profile`、`campaign_objective`、`campaign_scope_paths` 和
`measurement_budget_ceiling_seconds` 四个字段；fixed 或非 Flash 动作必须全部省略。
campaign_profile 决定修复域、修改权限和验证级别；Flash 只能在该契约内
逐步提出战术工作包，不能在后续步骤改变 profile 或扩大 campaign_scope_paths。
指导轮只做身份、动作成员和参数 schema 校验，不会把你的动作改写成另一个动作。"""
    if len(prompt) > PRO_DECISION_PROMPT_MAX_CHARS:
        raise RuntimeError("Pro DecisionPacket exceeded its bounded context budget")
    return prompt


class ProGuidanceRunner:
    """Own prompt, model request, parsing, and immutable decision validation."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root

    def run(self, decision_packet: Mapping[str, object], *, model: str,
            log_path: Path, execute: Callable[..., int],
            before_invoke: Optional[Callable[[], None]] = None) -> ProGuidanceResult:
        try:
            prompt = build_prompt(decision_packet)
        except RuntimeError as error:
            raise ProGuidanceInputError(str(error)) from error
        if before_invoke is not None:
            before_invoke()
        rc = execute(
            ["omp", "-p", "--cwd", str(self.project_root), "--model", model, prompt],
            log_path,
            record_command=False,
        )
        try:
            raw_output = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            raise ProGuidanceError(f"Pro output log is unreadable: {error}") from error
        if rc != 0:
            raise ProModelInvocationError(rc, raw_output=raw_output)
        try:
            decision = parse_decision(raw_output)
        except (ValueError, json.JSONDecodeError) as error:
            raise ProOutputError(str(error), raw_output=raw_output) from error
        packet_error = decision_error(decision, decision_packet)
        if packet_error is not None:
            raise ProDecisionRejected(
                packet_error, raw_output=raw_output, decision=decision)
        return ProGuidanceResult(decision=decision, raw_output=raw_output)
