# Regional candidates

Use this path when a hardware responsibility crosses existing functions or files. The existing fixed-file overlay remains supported; use a regional workspace when files or the ordered build must change. All examples use arbitrary project names and paths.

## Freeze the boundary and baseline

Explain the suspected cost, affected region and its observable contract in an ordinary report. Include enough control, state and caller context to test the mechanism. Existing logs guide source inspection; they do not prescribe a particular edit.

```bash
python3 -B "$S/scripts/readiness.py" init \
  --project /project --workspace "$W" --top block_top \
  --source rtl/block.sv --source rtl/old_helper.sv \
  --include rtl --asset tests/check.py \
  --region-root rtl --build-file sources.f
```

`--region-root` grants creation of candidate RTL under that directory, not permission to rewrite the whole project or frozen verification assets. Existing editable RTL is still determined by `--allow-edit` or the original source/header default. `--build-file` declares a baseline source filelist that may change with the candidate. All changes stay inside the experiment workspace; original RTL, architecture inputs, historical evidence and running jobs are retained.

The first helper implementation supports plain ordered filelists: project-relative source paths, one per line, with blank lines and `#` comments. Concatenating multiple declared filelists must give the exact ordered `sources`. Tool flags, nested filelists, variable expansion and tool-specific build languages are not interpreted. Use a resolved supported list or the actual frontend via a bounded `command`, and label unsupported build coverage honestly. Explicit include directories remain part of the build metadata.

Regional workspaces use a separate versioned identity. Old workspaces, evidence and fixed-file overlays retain their original meanings; do not edit their JSON or migrate them in place to obtain new permissions.

## Create one complete candidate

Write changed and added files in an overlay with original project-relative paths. Put the candidate's actual ordered source list and include directories in a small build JSON:

```json
{"sources":["rtl/lane.sv","rtl/block.sv"],"include_dirs":["rtl"]}
```

Update the declared `sources.f` in the overlay to match. Then seal the whole candidate:

```bash
python3 -B "$S/scripts/readiness.py" variant "$W" extracted \
  --overlay "$W/overlays/extracted" --build "$W/extracted-build.json" \
  --delete rtl/old_helper.sv
```

The example includes the new lane module, modified caller and source filelist, and removes a superseded helper. Deletion is explicit; absence from an overlay means keep the baseline file. Source order changes are meaningful even when file bytes do not change. New source files must actually belong to the candidate's declared dependency closure. File paths, bytes, operations and ordered build are bound together before execution. Top-level observation boundary stays the same in this version; a new internal module does not require changing the top.

Do not add frozen wrapper sources to the integration operation set. Do not remove a source from the filelist just to hide changed behavior. Generated inputs and macro definitions must describe the same observed design and conditions on both sides.

## Experiment and verify

Use the existing `run`, `batch`, `compare`, `finalize` and `verify-finalization` commands. Each version is compiled from its own sealed sources and include directories. Pairing permits different source lists while requiring the same tool, options, parameters, defines, budgets and observation boundary; both actual builds remain in the receipts. This is not permission to change constraints between versions.

Establish baseline affordability from reusable completed measurements with matching source/scope/configuration/tool identities, or a short pilot when needed. Reserve work for the selected repetition strategy, paired verification and delivery. Preserve expensive control/data relationships in a bounded experiment; if measuring only part of a region, report that limit and verify the matching integration candidate separately. No automatic full-design synthesis follows.

Boundary-cycle equivalence can hold despite different internal state or hierarchy. Built-in equivalence may not prove such rewrites; use suitable regional proof, transition/cycle assertions or a real paired regression with the affected callers. An unsupported or unfinished proof stays unfinished. Test programs/data and measurement extractors remain frozen inputs.

## Deliver

Finalization exports a patch with file additions/deletions plus the complete source/build identities and operation set. Empty-file changes use Git extended file-operation headers; consumers applying the patch must support those headers. Pass them with the raw receipts and unresolved obligations to the consumer. A controller must validate the unchanged baseline, consume the whole operation set transactionally and use the same independent RTL review for the resulting candidate. Without a controller, these ordinary artifacts are the standalone deliverable; nothing applies source automatically.

Keep baseline when a fair comparison finds no worthwhile benefit. When evidence is insufficient, retain the complete proposal and explain the missing regional or functional check. Local compile-time improvement is not mapped-area, macro-area or timing closure.
