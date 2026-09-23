#!/usr/bin/env python3
"""Environment identity for the isolated doule RTL harness experiment.

The original project deliberately keeps its historic hard-coded environment.
This copy must never default to that project, its runs, or its container.
"""
import argparse
import shlex

REMOTE_HOST = "txjg7-2@100.118.125.71"
REMOTE_PROJECT = "/home/txjg7-2/ic-project/doule_RTLharness"
CONTAINER = "doule-rtl-eda"
CONTAINER_WORKDIR = "/work/doule_RTLharness"


def shell_exports() -> str:
    values = {
        "HARNESS_REMOTE_HOST": REMOTE_HOST,
        "HARNESS_REMOTE_PROJECT": REMOTE_PROJECT,
        "HARNESS_CONTAINER": CONTAINER,
        "HARNESS_CONTAINER_WORKDIR": CONTAINER_WORKDIR,
    }
    return "\n".join(f"export {key}={shlex.quote(value)}" for key, value in values.items())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell", action="store_true", help="print safely quoted shell exports")
    args = parser.parse_args()
    if not args.shell:
        parser.error("use --shell")
    print(shell_exports())
