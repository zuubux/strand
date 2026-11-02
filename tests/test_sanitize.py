import pytest

from strand.sanitize import REDACTED, is_secret_token, sanitize_text

SECRETS = {
    "openai": "sk-" + "a1B2c3D4e5" * 3,
    "anthropic": "sk-ant-api03-" + "Ab_-" * 8,
    "ghp": "ghp_" + "a1B2c3" * 6,
    "github_pat": "github_pat_" + "A1b2_" * 12,
    "google": "AIza" + "Ab1-_" * 7,
    "aws": "AKIA" + "ABCDEFGH12345678",
    "bearer": "Bearer abcdefghij0123456789.xyz",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig_nature-1",
    "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\ndef==\n-----END RSA PRIVATE KEY-----",
    "db": "postgres://admin:hunter2pw@db.example.com:5432/app",
}


@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_sanitize_masks_each_pattern(kind):
    secret = SECRETS[kind]
    out = sanitize_text(f"before {secret} after")
    assert out == f"before {REDACTED} after"


def test_sanitize_leaves_clean_text_alone():
    text = "Marcus manages Atlas at https://example.com/path."
    assert sanitize_text(text) == text
    assert sanitize_text("") == ""


@pytest.mark.parametrize("kind", sorted(SECRETS))
def test_is_secret_token_flags_credentials(kind):
    assert is_secret_token(SECRETS[kind])


def test_is_secret_token_flags_marker_and_entropy():
    assert is_secret_token(f"Key {REDACTED}")
    assert is_secret_token("x9Kq2LmZ7vR4tB8nW3yH6cD1")


@pytest.mark.parametrize("name", ["Marcus", "Atlas", "Acme W34", "Database Migration", "", "Internationalization"])
def test_is_secret_token_accepts_normal_names(name):
    assert not is_secret_token(name)
