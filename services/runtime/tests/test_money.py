"""Tests for exact money representation and minor unit conversion."""

from decimal import Decimal
import pytest

from ishop.domain.models import Money


def test_money_creation_and_minor_units():
    # USD (2 decimals)
    usd = Money.from_string("19.99", "USD")
    assert usd.to_minor_units() == 1999
    assert str(usd) == "19.99 USD"

    # NGN (2 decimals)
    ngn = Money.from_string("15000.50", "NGN")
    assert ngn.to_minor_units() == 1500050
    assert str(ngn) == "15000.50 NGN"

    # JPY (0 decimals)
    jpy = Money.from_string("2500", "JPY")
    assert jpy.to_minor_units() == 2500
    assert str(jpy) == "2500 JPY"

    # KWD (3 decimals)
    kwd = Money.from_string("4.250", "KWD")
    assert kwd.to_minor_units() == 4250
    assert str(kwd) == "4.250 KWD"


def test_money_from_minor_units():
    usd = Money.from_minor_units(1999, "USD")
    assert usd.amount == Decimal("19.99")

    jpy = Money.from_minor_units(2500, "JPY")
    assert jpy.amount == Decimal("2500")

    kwd = Money.from_minor_units(4250, "KWD")
    assert kwd.amount == Decimal("4.250")


def test_money_arithmetic():
    a = Money.from_string("10.50", "USD")
    b = Money.from_string("5.25", "USD")

    res_add = a + b
    assert res_add.amount == Decimal("15.75")

    res_sub = a - b
    assert res_sub.amount == Decimal("5.25")

    assert b < a
    assert b <= a


def test_mismatched_currency_rejection():
    usd = Money.from_string("10.00", "USD")
    ngn = Money.from_string("10.00", "NGN")

    with pytest.raises(ValueError, match="Cannot add mismatched currencies"):
        _ = usd + ngn

    with pytest.raises(ValueError, match="Cannot compare mismatched currencies"):
        _ = usd < ngn
