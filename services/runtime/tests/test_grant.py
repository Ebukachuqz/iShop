"""Tests for session grant validation and tenant isolation (T-15, T-22, S-11)."""

from ishop.domain.models import SessionGrant

SECRET = "super_secure_signing_secret_32_bytes_long_1234"


def test_valid_signed_grant():
    grant = SessionGrant.create_signed(
        grant_id="grant_01",
        shop_id="test.myshopify.com",
        permitted_origin="https://test.myshopify.com",
        anonymous_session_id="sess_0123456789abcdef",
        config_revision="rev_1",
        asr_profile_id="sahara-stream-pcm",
        llm_profile_id="groq-gpt-oss-120b",
        tts_profile_id="sahara-tts-female-pcm",
        issued_at_ms=1000,
        ttl_ms=60000,
        signing_secret=SECRET,
    )

    assert grant.is_valid_at(1500)
    assert grant.verify_signature(SECRET)
    assert grant.verify_tenancy("test.myshopify.com", "https://test.myshopify.com")


def test_typescript_and_python_grant_signatures_match():
    grant = SessionGrant.create_signed(
        grant_id="grant_test",
        shop_id="example.myshopify.com",
        permitted_origin="https://shop.example.com",
        anonymous_session_id="sess_1234567890abcdef",
        config_revision="default-v1",
        asr_profile_id="sahara-stream-pcm",
        llm_profile_id="groq-gpt-oss-120b",
        tts_profile_id="sahara-tts-female-pcm",
        issued_at_ms=1000,
        ttl_ms=300000,
        signing_secret="a-development-secret-with-32-characters",
    )
    assert grant.signature == (
        "d1bc2a29189b2e6e25e1bfecb4bbb0732cb540a5750d624f101ea093f9c83064"
    )


def test_expired_grant_rejected():
    # T-15: Expired grant
    grant = SessionGrant.create_signed(
        grant_id="grant_02",
        shop_id="test.myshopify.com",
        permitted_origin="https://test.myshopify.com",
        anonymous_session_id="sess_0123456789abcdef",
        config_revision="rev_1",
        asr_profile_id="sahara-stream-pcm",
        llm_profile_id="groq-gpt-oss-120b",
        tts_profile_id="sahara-tts-female-pcm",
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
        asr_profile_id="sahara-stream-pcm",
        llm_profile_id="groq-gpt-oss-120b",
        tts_profile_id="sahara-tts-female-pcm",
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
        asr_profile_id="sahara-stream-pcm",
        llm_profile_id="groq-gpt-oss-120b",
        tts_profile_id="sahara-tts-female-pcm",
        issued_at_ms=1000,
        ttl_ms=60000,
        signing_secret=SECRET,
    )

    # Attacker tries to use grant against another shop
    assert not grant.verify_tenancy("attacker-store.myshopify.com", "https://victim-store.myshopify.com")
    # Attacker tries to use grant from unauthorized origin
    assert not grant.verify_tenancy("victim-store.myshopify.com", "https://malicious-site.com")
