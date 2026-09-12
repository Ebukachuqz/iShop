"""Tests for session grant validation and tenant isolation (T-15, T-22, S-11)."""

import pytest

from ishop.domain.models import SessionGrant

SECRET = "super_secure_signing_secret_32_bytes_long_1234"


def test_valid_signed_grant():
    grant = SessionGrant.create_signed(
        grant_id="grant_01",
        shop_id="test.myshopify.com",
        permitted_origin="https://test.myshopify.com",
        anonymous_session_id="sess_0123456789abcdef",
        config_revision="rev_1",
        issued_at_ms=1000,
        ttl_ms=60000,
        signing_secret=SECRET,
    )

    assert grant.is_valid_at(1500)
    assert grant.verify_signature(SECRET)
    assert grant.verify_tenancy("test.myshopify.com", "https://test.myshopify.com")


def test_expired_grant_rejected():
    # T-15: Expired grant
    grant = SessionGrant.create_signed(
        grant_id="grant_02",
        shop_id="test.myshopify.com",
        permitted_origin="https://test.myshopify.com",
        anonymous_session_id="sess_0123456789abcdef",
        config_revision="rev_1",
        issued_at_ms=1000,
        ttl_ms=5000,
        signing_secret=SECRET,
    )

    assert not grant.is_valid_at(7000)  # Current time past expiry


def test_forged_signature_rejected():
    # T-15: Forged signature
    grant = SessionGrant.create_signed(
        grant_id="grant_03",
        shop_id="test.myshopify.com",
        permitted_origin="https://test.myshopify.com",
        anonymous_session_id="sess_0123456789abcdef",
        config_revision="rev_1",
        issued_at_ms=1000,
        ttl_ms=60000,
        signing_secret=SECRET,
    )

    wrong_secret = "wrong_secret_key_that_does_not_match_at_all"
    assert not grant.verify_signature(wrong_secret)


def test_tenant_spoofing_rejected():
    # T-15 & T-22: Wrong shop or wrong origin
    grant = SessionGrant.create_signed(
        grant_id="grant_04",
        shop_id="victim-store.myshopify.com",
        permitted_origin="https://victim-store.myshopify.com",
        anonymous_session_id="sess_0123456789abcdef",
        config_revision="rev_1",
        issued_at_ms=1000,
        ttl_ms=60000,
        signing_secret=SECRET,
    )

    # Attacker tries to use grant against another shop
    assert not grant.verify_tenancy("attacker-store.myshopify.com", "https://victim-store.myshopify.com")
    # Attacker tries to use grant from unauthorized origin
    assert not grant.verify_tenancy("victim-store.myshopify.com", "https://malicious-site.com")
