"""Tests for canonical cart multiset equivalence and property normalization (T-25, S-08)."""

import pytest

from ishop.domain.models import CartLine, CartSnapshot


def test_canonical_line_key_normalization():
    # Properties with out-of-order keys must produce identical canonical key
    line1 = CartLine(
        variant_id="var_1",
        quantity=2,
        selling_plan_id=None,
        properties={"size": "M", "color": "blue"},
    )
    line2 = CartLine(
        variant_id="var_1",
        quantity=2,
        selling_plan_id=None,
        properties={"color": "blue", "size": "M"},
    )
    assert line1.canonical_key == line2.canonical_key
    assert line1.canonical_key == "var_1::none::color=blue&size=M"


def test_unicode_and_diacritics_preserved():
    # T-25: Unicode and diacritics preserved
    line = CartLine(
        variant_id="var_1",
        quantity=1,
        properties={"ọ̀rọ̀": "dídùn", "café": "crème"},
    )
    assert "ọ̀rọ̀=dídùn" in line.canonical_key
    assert "café=crème" in line.canonical_key


def test_cart_multiset_equivalence_ignores_order():
    # Line 1 and Line 2 in different order in cart A and cart B
    line_a = CartLine("var_1", quantity=2, properties={"color": "red"})
    line_b = CartLine("var_2", quantity=1, selling_plan_id="sub_monthly")

    cart1 = CartSnapshot(shop_id="test.myshopify.com", currency="USD", lines=(line_a, line_b))
    cart2 = CartSnapshot(shop_id="test.myshopify.com", currency="USD", lines=(line_b, line_a))

    assert cart1.is_equivalent(cart2)
    assert cart1.fingerprint() == cart2.fingerprint()


def test_cart_different_selling_plan_not_equivalent():
    line_a = CartLine("var_1", quantity=1, selling_plan_id="sub_monthly")
    line_b = CartLine("var_1", quantity=1, selling_plan_id="sub_annual")

    cart1 = CartSnapshot(shop_id="test.myshopify.com", currency="USD", lines=(line_a,))
    cart2 = CartSnapshot(shop_id="test.myshopify.com", currency="USD", lines=(line_b,))

    assert not cart1.is_equivalent(cart2)
    assert cart1.fingerprint() != cart2.fingerprint()


def test_cart_different_quantity_not_equivalent():
    line_a = CartLine("var_1", quantity=1)
    line_b = CartLine("var_1", quantity=2)

    cart1 = CartSnapshot(shop_id="test.myshopify.com", currency="USD", lines=(line_a,))
    cart2 = CartSnapshot(shop_id="test.myshopify.com", currency="USD", lines=(line_b,))

    assert not cart1.is_equivalent(cart2)
    assert cart1.fingerprint() != cart2.fingerprint()


def test_cart_v1_fingerprint_matches_browser_contract():
    assert CartSnapshot("store.myshopify.com", "USD").fingerprint() == (
        "0b67ea4fa5fd4ee2aa90a7b316be131823a2ce3359a887d95ac8fe38bc2cf6e0"
    )
    cart = CartSnapshot(
        "store.myshopify.com",
        "USD",
        (
            CartLine("v2", 1, "plan-1", {"x": "a&b=c"}),
            CartLine("v1", 2, properties={"size": "M", "note": "雪"}),
            CartLine("v1", 1, properties={"note": "雪", "size": "M"}),
        ),
    )
    assert cart.fingerprint() == "b0181186fd87e3564b9f7f22f80d4538c25bd4956ba602e7e8d3ad1129ed6b17"


def test_browser_line_key_survives_runtime_cart_deserialization():
    cart = CartSnapshot.from_dict({
        "shop_id": "store.myshopify.com", "currency": "USD",
        "lines": [{"line_key": "shopify-line-7", "variant_id": "101", "quantity": 1, "properties": {}}],
    })
    assert cart.lines[0].shopify_line_key == "shopify-line-7"
