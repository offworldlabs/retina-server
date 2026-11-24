"""The app-link files answer from settings and 404 while those are unset."""

APPLE = "/.well-known/apple-app-site-association"
ANDROID = "/.well-known/assetlinks.json"
FINGERPRINT = ":".join(["ab", "01", "cd", "23"] * 8)


def test_both_files_are_404_while_unset(client, monkeypatch):
    monkeypatch.delenv("MOBILE_APPLE_TEAM_ID", raising=False)
    monkeypatch.delenv("MOBILE_ANDROID_CERT_SHA256", raising=False)
    assert client.get(APPLE).status_code == 404
    assert client.get(ANDROID).status_code == 404


def test_both_files_are_served_from_settings(client, monkeypatch):
    monkeypatch.setenv("MOBILE_APPLE_TEAM_ID", "ABCDE12345")
    monkeypatch.setenv("MOBILE_ANDROID_CERT_SHA256", f" {FINGERPRINT} ")
    apple, android = client.get(APPLE), client.get(ANDROID)
    assert apple.headers["content-type"] == android.headers["content-type"] == "application/json"
    assert (
        apple.text
        == '{"applinks":{"details":[{"appIDs":["ABCDE12345.retina.fm"],"components":[{"/":"/auth/link/*"}]}]}}'
    )
    assert android.json() == [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "retina.fm",
                "sha256_cert_fingerprints": [FINGERPRINT.upper()],
            },
        }
    ]


def test_invalid_values_are_skipped_or_refused(client, monkeypatch):
    monkeypatch.setenv("MOBILE_APPLE_TEAM_ID", "abcde12345")
    monkeypatch.setenv("MOBILE_ANDROID_CERT_SHA256", f"AA:BB, {FINGERPRINT}")
    assert client.get(APPLE).status_code == 404
    assert client.get(ANDROID).json()[0]["target"]["sha256_cert_fingerprints"] == [FINGERPRINT.upper()]
