"""Supply an explicit operator policy to existing receipt producer fixtures.

Native receipt suites must use a private --basetemp below publisher-controlled
ancestors on the same filesystem as their artifacts. The policy/adversarial
integration files supply their own contexts and deliberately bypass this fixture.
No production admission or native recovery operation is mocked here.
"""

from contextlib import ExitStack
import os
import sys

import pytest


@pytest.fixture(autouse=True)
def receipt_fixture_publisher_policy(request, monkeypatch, tmp_path):
    if os.name == "nt" or request.path.name not in {
        "test_classroom_release_evidence.py",
        "test_source_head_receipt_publication.py",
        "test_running_container_receipt_publication.py",
        "test_receipt_input_close.py",
        "test_capacity_receipt_inputs.py",
        "test_preflight_receipt_inputs.py",
        "test_verify_classroom_release.py",
    }:
        yield
        return
    from tests.scripts import test_classroom_release_evidence as support

    loaded_count = 0
    with ExitStack() as scopes:

        def load_with_operator_policy(name, path, *, loader):
            nonlocal loaded_count
            module = loader(name, path)
            if name == "classroom_release_evidence_under_test":
                recovery = tmp_path / f"receipt-recovery-{loaded_count}"
                loaded_count += 1
                recovery.mkdir(mode=0o700)
                scopes.enter_context(
                    module.receipt_publication(
                        recovery_root=recovery,
                        worker_uid=1000 if os.geteuid() != 1000 else 1001,
                    )
                )
                cli = module.main

                def main_with_operator_policy(argv=None):
                    arguments = list(sys.argv[1:] if argv is None else argv)
                    # CLI forwarding fixtures launch a separate explicit policy;
                    # the production CLI admission is not bypassed or mocked.
                    token = module._RECEIPT_ATTEMPT.set(None)
                    try:
                        return cli(
                            [
                                "--receipt-recovery-root",
                                str(recovery),
                                "--receipt-worker-uid",
                                str(1000 if os.geteuid() != 1000 else 1001),
                                *arguments,
                            ]
                        )
                    finally:
                        module._RECEIPT_ATTEMPT.reset(token)

                module.main = main_with_operator_policy
            return module

        # Pytest may collect this file as a top-level module while other fixtures
        # import tests.scripts.test_classroom_release_evidence separately.
        targets = {support}
        if request.path.name == "test_classroom_release_evidence.py":
            targets.add(request.module)
        for target in targets:
            loader = target._load_path
            monkeypatch.setattr(
                target,
                "_load_path",
                lambda name, path, loader=loader: load_with_operator_policy(
                    name, path, loader=loader
                ),
            )
        yield
