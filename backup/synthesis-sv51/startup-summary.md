# v19 synthesis migration startup

Observed: 2026-10-23T03:06:45.551198+00:00 (2026-10-23 11:06:45 Asia/Shanghai).

Destination: czk@100.85.224.17, hostname sv51, Docker eda.
Host run: /home/czk/workspace/harness_final/output/synth--20261023-v19-candidate3-sv51
Container run: /work/harness_final/output/synth--20261023-v19-candidate3-sv51

Job: synth--20261023-v19-candidate3-sv51--synthesis-01
Started: 2026-10-23T03:05:59.038027+00:00 (2026-10-23 11:05:59 Asia/Shanghai).
State observed: running, phase 01_elaborate.
Supervisor container PID 63, start ticks 58132002; Yosys PID 64, start ticks 58132073.
Boot ID: b89660a4-417a-44e6-913c-9167ec87d67d.
The launcher SSH returned successfully before this separate observation. Supervisor parent is PID1 and the Yosys child remains active.
Yosys CPU 45.74 seconds, RSS 672420 kB at observation.

Input: v19 derivative of frozen V5 candidate3.
Native candidate provenance SHA256: a5e26e78f7fa67465a92b238ff15324fb14d65891b20bea6e61dd860f575406f
10-file RTL closure SHA256: 6cd8e7137853241efee622acabfb664f41f3b4cf2bd47480de484c565ddbf296
Core SHA256: db019c536f5d60c9b9399ead1110c6aff235e1262d76098dd974c9e5170d8423.
The full native work+architecture inventory was not transferred or rehashed; closure bytes and scoped review report match the prior source-bound request exactly.

Top msh_chip_top; four .sv compilation units, six .svh headers; no top parameter overrides.
Exact Yosys 0.64, ABC and Nangate45 library hashes match the prior server.
All four original .ys scripts are byte-identical. Checkpoints are regenerated from v19 inputs.
Initial admission: no active EDA, 489.8 GiB memory available, 5.04 TiB disk free, 288 CPUs.

Current log: logs/01_elaborate.yosys.log, 3658 bytes.
Initial warning: cv_base memory replaced by registers, input/rtl/nano_core.sv:1091. This has not yet been quantified as final resources.
No completed phase, mapped area or STA result is available at this observation.
SRAM macros remain blackboxes; their physical area/timing models and complete I/O/clock constraints are missing.
The 100MHz specification target is not an STA pass.

Launch command already executed once:
ssh czk@100.85.224.17 'docker exec -w /work/harness_final/output/synth--20261023-v19-candidate3-sv51 eda /opt/eda/bin/python3 -B launch.py'
Do not repeat or delete launch state after a slow SSH response. Inspect status.json, launch.json, launch.claim.json, logs/ and reports/.
Exact stage commands are in scripts/01_elaborate.ys through scripts/04_map.ys and recorded in per-phase launch reports.

Old-server jobs were not interrupted or modified. Their current status was unavailable because old LAN and Tailscale SSH endpoints timed out.
All EDA outputs remain on the new server; only this small startup observation and summary returned locally.
