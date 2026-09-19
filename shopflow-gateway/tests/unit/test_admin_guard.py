"""The administrative guard fails closed (contracts.md §2)."""

from __future__ import annotations

import asyncio

import pytest

from app.security.admin_guard import ADMIN_ROLE, ADMIN_ROLE_HEADER, require_admin
from app.upstream.errors import GatewayError


def check(value: str | None) -> None:
    asyncio.run(require_admin(value))


@pytest.mark.parametrize("value", [None, "", "   ", "user", "Admin", "ADMIN", "admins", "administrator"])
def test_missing_or_wrong_role_is_forbidden(value: str | None) -> None:
    with pytest.raises(GatewayError) as excinfo:
        check(value)

    assert excinfo.value.code == "forbidden"
    assert excinfo.value.status_code == 403
    assert ADMIN_ROLE_HEADER in excinfo.value.message


@pytest.mark.parametrize("value", ["admin", " admin "])
def test_admin_role_passes(value: str) -> None:
    check(value)


def test_header_name_matches_the_contract() -> None:
    assert ADMIN_ROLE_HEADER == "X-ShopFlow-Role"
    assert ADMIN_ROLE == "admin"
