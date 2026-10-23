# Repair an independently reviewed RTL candidate

Use the provided audit report, frozen candidate identity and current implementation work directory. Read the relevant source requirements, reference and architecture as well as the failed job's actual diagnostics. Preserve earlier candidates and evidence. Runtime repair changes project artifacts in the writable implementation work directory; it does not modify frozen inputs, general skills or audit-owned artifacts.

## Diagnose before changing behavior

Reproduce the reported failure against the current implementation or explain the exact version/environment difference. A report need not name a root cause. Missing computation, unconnected required modules and dummy output paths are implementation failures to repair, not conditions to waive. A numerical or protocol failure is an engineering problem to investigate; do not wait for an external operator to identify each bug.

Check a review's architecture and measurement claims against the actual selected packet, source procedure and existing job results before choosing a change. An inherited plan or prior-version value can be stale. Preserve the source-backed discrepancy when a finding is incorrect; do not change the RTL to satisfy it or rewrite audit-owned evidence. This does not waive independently demonstrated failures in the same report.

For missing computation or top integration, follow a small legal transaction from its supported external initialization/input path to the required state update and output. Implement and exercise the first unfinished resource/state boundary, then extend that working chain through the remaining required stages. Reuse applicable leaf evidence and focused numerical probes to support this work. The first-version minimum case in the main skill remains the integration target; completing more isolated tests does not close a missing executor. Keep the next boundary and runnable reproducer in the implementation checkpoint so a long repair can resume without restarting its exploration.

For a failure that is not locally obvious, use a stable input/configuration and progressively compare semantic boundaries. Keep operand layouts, scales, rounding, state versions and transaction identities explicit. An allowed numerical difference is not itself a defect; identify the first deviation that the source's numerical rules cannot explain. Capture only useful windows and summaries for model interaction. Full tensors, waveforms, simulator products and long logs remain on the server.

Narrow a failing full run to the affected computation or transaction when practical, retaining the relevant state and producer/consumer relationship. A local reproducer accelerates diagnosis but does not replace the final acceptance run. Treat timeouts, missing outputs, incomplete dumps and missing DONE as failures rather than scoring the available subset.

## Efficiency repairs and tradeoffs

Check whether the cited performance value is a mandatory source/caller requirement, an ideal optimization target or an estimate. Keep all functional, numerical, protocol and explicit hard resource gates intact; uncertainty does not justify silently changing that classification. A target gap alone is not proof of a material avoidable defect. Investigate the actual work, stalls, dependencies and resource/clock limits before attributing avoidability.

When actual evidence establishes a material avoidable efficiency defect, make the implementation change and compare the original and repaired versions under the same workload, memory conditions, initial state and accepted transaction boundaries. Retain both source identities and actual jobs, affected functional checks, whole-task/service results and any changed resource or clock assumptions. A changed estimate, diagnostic trace or proposed fix is not that repair.

Submit any claimed reasonable tradeoff for independent review with actual evidence, the considered alternatives and their feasibility or bounds, resource/clock budgets and residual risks. The reviewer must check the argument; implementation self-description is insufficient. A justified baseline may retain an unmet ideal target or unproven target feasibility, explicitly reported without claiming target met. It cannot waive a hard requirement or an established material avoidable defect. Reconcile changed architectural claims through the full revision path before returning the repair.

## Repair and return

Change the responsible RTL, data packing, initialization, scheduling or development test. Do not change the original acceptance threshold or remove a legal case. If an audit-owned test appears incorrect, report the source-backed discrepancy and a reproducer; the audit context decides whether to correct that test and retains its prior result. A genuine conflict between specification and reference is unresolved until an authorized source interpretation is available.

Use directed development checks to validate the fix and nearby boundaries. Rerun affected integration checks after modifying numeric behavior, state/control, interfaces or memory ordering. Synthesis follows the changed risk: use local structural/synthesis checks for changed datapaths or storage organization, and update whole-design evidence for the final relevant candidate. Adding a trace or changing only test stimuli does not by itself require a whole-design synthesis. Existing evidence is reusable only while its source, dependencies, configuration and tool conditions remain applicable.

For a changed resource or schedule, [measure the implemented schedule](../SKILL.md#measure-the-implemented-schedule) along the retained top transaction and use the observed service/cycle gap to choose the next focused check before repeating a long mapping run.

Before returning the repair, [reconcile the implemented baseline](architecture-revision.md#reconcile-the-implemented-baseline). Use the existing architecture revision interface for major resource, ABI, internal precision or budget changes. Ordinary implementation details remain within this repair. An unavailable architecture reviewer cannot confer approval, but does not prevent recording an implementation candidate and its unresolved conditions.

Update the plan and progress/handoff with the findings addressed, actual changes, commands and job IDs, remaining failures, and the new candidate's source fingerprints. Preserve failed runs. The independent audit must decide whether findings are closed and source acceptance is satisfied. If repeated attempts make no progress, tools are unavailable, or requirements conflict, hand off the concrete reproducer and blocking condition instead of changing rules or claiming completion.
