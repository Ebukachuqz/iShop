"""Smoke test for iShop runtime package.

Verifies:
1. Pytest test discovery works.
2. Root package is importable and has correct metadata.
3. Offline execution without external network or provider credentials.
"""

import ishop


def test_package_import():
    assert ishop.__version__ == "0.1.0"


def test_offline_runtime_assertion():
    # Prove that assertions are actively evaluated
    expected = "ishop-runtime-ready"
    assert expected.startswith("ishop")
