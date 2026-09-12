import os
import ssl

import certifi

from apple_reminders_mcp.auth import _install_default_request_timeout, ensure_ca_bundle


def test_ca_bundle_uses_certifi_without_disabling_verification(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    assert ensure_ca_bundle() == certifi.where()
    assert os.environ["SSL_CERT_FILE"] == certifi.where()
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_ca_bundle_preserves_operator_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom.pem"
    custom.write_text(certifi.contents())
    monkeypatch.setenv("SSL_CERT_FILE", str(custom))
    assert ensure_ca_bundle() == str(custom)


def test_default_timeout_is_added_only_when_pyicloud_omits_one():
    calls = []

    class Session:
        def request(self, *args, **kwargs):
            calls.append((args, kwargs))

    api = type("Api", (), {"session": Session()})()
    _install_default_request_timeout(api)

    api.session.request("POST", "https://example.invalid/pcs")
    api.session.request("POST", "https://example.invalid/query", timeout=(1, 2))

    assert calls[0][1]["timeout"] == (10.0, 60.0)
    assert calls[1][1]["timeout"] == (1, 2)
