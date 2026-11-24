"""Refuse direct pytest invocations against anything but the explicit test cluster."""

import pytest

from scripts.check import invoice_test_target


@pytest.fixture(scope="session", autouse=True)
def require_isolated_invoice_database():
    if not invoice_test_target():
        pytest.fail(
            "Durable Invoice verification requires an explicitly isolated local test cluster."
        )
