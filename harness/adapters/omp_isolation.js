/** Explicit OMP extension: no native tool may execute in input-only model calls. */
import fs from 'node:fs';
export default function (pi) {
  const receipt = process.env.HARNESS_TOOL_ISOLATION_RECEIPT;
  if (!receipt) throw new Error('Missing harness tool-isolation receipt path');
  const evidence = {version: 1, checks: [], blocked: []};
  const save = () => fs.writeFileSync(receipt, JSON.stringify(evidence, null, 2) + '\n');
  const clear = async () => {
    await pi.setActiveTools([]);
    const active = pi.getActiveTools();
    if (active.length) throw new Error('Native tools remain active');
    evidence.checks.push({active_tool_names: active});
    save();
  };
  pi.on('session_start', clear);
  pi.on('before_agent_start', clear);
  pi.on('tool_call', async event => {
    evidence.blocked.push({id: event.toolCallId, name: event.toolName});
    save();
    return {block: true, reason: 'Native tools are disabled for this input-only call. Submit the caller text protocol; the host executes only the frozen skill calculator.'};
  });
}
