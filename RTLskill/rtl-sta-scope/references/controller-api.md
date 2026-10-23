# Controller API

Use the Python interface:

```python
from scope_gate import run
decision = run(change_packet)
```

or the CLI:

```text
python3 scope_gate.py [change.json]
```

With no path, the CLI reads JSON from stdin. It emits one JSON object with
`scope`, `minimum_scope`, `requires_sta`, `reason_codes`, `affected_modules`,
`design_context_fingerprint`, and `measurement_plan`.

The caller must reject an unavailable gate. A model may help populate change
facts, but the controller must treat missing/unknown safety facts as top-level
scope and must not let model prose override the gate.
