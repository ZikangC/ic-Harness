# Task: build the complete Nano-KPU decoder RTL

Read the normative model at `/Users/chenzikang/code/work/k3demo/reference.py` before making changes.
Implement the complete Nano-KPU decoder in `rtl/`, with external top module
`nano_decoder_top` matching the port list instantiated by `tb/tb_nano_decoder_top.sv`.

Create all required child modules and list every synthesizable source in dependency order in
`rtl/filelist.f`. Use SystemVerilog only. Keep all candidate implementation files under `rtl/`.

Implementation order may be internally staged, but do not stop after planning: create the full
set of child modules and top-level connections. Follow the reference semantics for int4 weights,
KDA causal state, MLA cache, MoE routing, residual mixing and logits/argmax writes.

Do not modify `harness/`, `harness.toml`, `tasks/`, or `tb/`. The testbench and compilation
command are fixed harness infrastructure.

At the end of every repair iteration, run exactly:

```bash
bash scripts/remote_round.sh --task 00_build_nano_decoder --seed 20260706 --prompt 1
```

Use the downloaded `runs/.../summary.json`, `simulation.log`, and `score.log` to identify the
first failure and repair the relevant RTL. Completion requires a valid status word, per-token
cosine >= 0.98, and argmax agreement >= 0.99.
