import base64
import json
import os
from uuid import uuid4

import pytest
from coinpup_private_v1 import PrivateFieldError, load_key

from scripts.init_private_key import generate


@pytest.fixture
def keyfile(tmp_path, monkeypatch):
    path = tmp_path / "fictional-secret.key"
    generate(path, "fixture")
    monkeypatch.setenv("COINPUP_PRIVATE_KEY_FILE", str(path))
    return path


def test_key_generation_is_exclusive_and_recoverable(keyfile, tmp_path, monkeypatch):
    if os.name == "posix":
        assert keyfile.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        generate(keyfile, "replacement")
    original = load_key()
    copy = tmp_path / "escrow-copy.key"
    copy.write_bytes(keyfile.read_bytes())
    monkeypatch.setenv("COINPUP_PRIVATE_KEY_FILE", str(copy))
    context = ("accounts", "card_number", uuid4(), uuid4())
    value = "fictional-" + uuid4().hex
    first = original.encrypt(value, *context)
    assert first.startswith("v1.fixture.")
    assert first != original.encrypt(value, *context)
    assert value not in first
    assert load_key().decrypt(first, *context) == value


def test_envelope_authenticates_table_field_and_all_record_identifiers(keyfile):
    key = load_key()
    context = ["account_assets", "memo", uuid4(), uuid4(), "custodial:USDT"]
    value = uuid4().hex
    envelope = key.encrypt(value, *context)
    for index in range(len(context)):
        other_context = context.copy()
        other_context[index] = str(uuid4())
        with pytest.raises(PrivateFieldError):
            key.decrypt(envelope, *other_context)


def test_corruption_wrong_key_and_plaintext_are_rejected(keyfile, tmp_path, monkeypatch):
    key = load_key()
    context = ("accounts", "details", uuid4(), uuid4())
    value = uuid4().hex
    envelope = key.encrypt(value, *context)
    version, kid, data = envelope.split(".")
    sealed = bytearray(base64.b64decode(data))
    for position in [0, 12, -1]:
        changed = sealed.copy()
        changed[position] ^= 1
        tampered = ".".join([version, kid, base64.b64encode(changed).decode("ascii")])
        with pytest.raises(PrivateFieldError):
            key.decrypt(tampered, *context)
    for invalid in [
        value,
        envelope.replace("v1.", "v2."),
        envelope.replace(".fixture.", ".other."),
    ]:
        with pytest.raises(PrivateFieldError) as exc:
            key.decrypt(invalid, *context)
        assert str(exc.value) == "Private account storage is unavailable"

    wrong = tmp_path / "another.key"
    generate(wrong, kid)
    monkeypatch.setenv("COINPUP_PRIVATE_KEY_FILE", str(wrong))
    with pytest.raises(PrivateFieldError):
        load_key().decrypt(envelope, *context)


def test_custom_name_uniqueness_is_scoped_and_requires_the_secret(keyfile, tmp_path, monkeypatch):
    ledger = uuid4()
    name = uuid4().hex
    key = load_key()
    index = key.custom_platform_key(name, ledger)
    assert index == load_key().custom_platform_key(name, ledger)
    assert index != key.custom_platform_key(name, uuid4())
    assert index != key.custom_platform_key(name + "x", ledger)
    other = tmp_path / "other.key"
    generate(other, "fixture")
    monkeypatch.setenv("COINPUP_PRIVATE_KEY_FILE", str(other))
    assert index != load_key().custom_platform_key(name, ledger)


def test_missing_invalid_key_or_document_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COINPUP_PRIVATE_KEY_FILE", raising=False)
    with pytest.raises(PrivateFieldError):
        load_key()
    path = tmp_path / "fixture.key"
    monkeypatch.setenv("COINPUP_PRIVATE_KEY_FILE", str(path))
    with pytest.raises(PrivateFieldError):
        load_key()
    for content in [
        b"",
        json.dumps({"id": "odd.kid", "key": base64.b64encode(os.urandom(32)).decode()}).encode(),
        json.dumps({"id": "fixture", "key": base64.b64encode(os.urandom(16)).decode()}).encode(),
        b'{"id":"fixture", "key":"*invalid*"}',
        b'{"id":"fixture"}',
        b"x" * 1025,
    ]:
        path.write_bytes(content)
        with pytest.raises(PrivateFieldError) as exc:
            load_key()
        assert str(exc.value) == "Private account storage is unavailable"


def test_source_checkout_env_file_and_runtime_override(keyfile, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COINPUP_PRIVATE_KEY_FILE", raising=False)
    (tmp_path / ".env").write_text(f"COINPUP_PRIVATE_KEY_FILE={keyfile}\n")
    assert load_key().id == "fixture"
    monkeypatch.setenv("COINPUP_PRIVATE_KEY_FILE", "")
    with pytest.raises(PrivateFieldError):
        load_key()
