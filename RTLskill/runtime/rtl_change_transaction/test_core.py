#!/usr/bin/env python3
"""No-network tests for atomic RTL transaction primitives."""
from __future__ import annotations

import tempfile
from pathlib import Path

from rtl_change_transaction.core import (
    TransactionError, acquire_lease, begin_transaction, commit_transaction,
    inspect_transaction, release_lease, rollback_transaction,
)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    project = root / "project"
    (project / "rtl").mkdir(parents=True)
    a = project / "rtl/a.sv"
    b = project / "rtl/b.sv"
    a.write_text("module a; endmodule\n", encoding="utf-8")
    b.write_text("module b; endmodule\n", encoding="utf-8")

    lease_path = root / "write.lease"
    lease = acquire_lease(lease_path, owner="test")
    try:
        acquire_lease(lease_path, owner="other")
        raise AssertionError("second lease unexpectedly succeeded")
    except TransactionError:
        pass

    tx = root / "tx-one"
    begin_transaction(
        project_root=project, transaction_root=tx, operation_id="op-1",
        managed_paths=["rtl/a.sv", "rtl/b.sv", "rtl/new.sv"],
        allowed_paths=["rtl/a.sv", "rtl/new.sv"], max_changed_files=2)
    a.write_text("module a; wire changed; endmodule\n", encoding="utf-8")
    (project / "rtl/new.sv").write_text("module new; endmodule\n", encoding="utf-8")
    inspected = inspect_transaction(tx)
    assert inspected["within_contract"] is True
    assert inspected["changed_paths"] == ["rtl/a.sv", "rtl/new.sv"]
    committed = commit_transaction(tx)
    assert committed["phase"] == "COMMITTED"
    restored = rollback_transaction(tx)
    assert restored["rollback_proof"]["verified"] is True
    assert a.read_text(encoding="utf-8") == "module a; endmodule\n"
    assert not (project / "rtl/new.sv").exists()

    tx2 = root / "tx-two"
    begin_transaction(
        project_root=project, transaction_root=tx2, operation_id="op-2",
        managed_paths=["rtl/a.sv", "rtl/b.sv"], allowed_paths=["rtl/a.sv"])
    b.write_text("module b; wire unauthorized; endmodule\n", encoding="utf-8")
    inspected = inspect_transaction(tx2)
    assert inspected["within_contract"] is False
    assert inspected["unauthorized_paths"] == ["rtl/b.sv"]
    rollback_transaction(tx2)
    assert b.read_text(encoding="utf-8") == "module b; endmodule\n"
    release_lease(lease_path, token=str(lease["token"]))

print("rtl change transaction tests: PASS")
