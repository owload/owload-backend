"""Unit tests of how a request is described (browser, address) for the account activity; no infrastructure needed."""
from starlette.requests import Request

from app.core.config import Settings
from app.services.activity import _client_ip, describe_device

CHROME_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
SAFARI_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
FIREFOX_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0"
EDGE_WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
CHROME_ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36"


def test_describe_device():
    assert describe_device(CHROME_MAC) == ("Chrome on macOS", False)
    assert describe_device(SAFARI_IPHONE) == ("Safari on iOS", True)
    assert describe_device(FIREFOX_WINDOWS) == ("Firefox on Windows", False)
    assert describe_device(EDGE_WINDOWS) == ("Edge on Windows", False)
    assert describe_device(CHROME_ANDROID) == ("Chrome on Android", True)
    assert describe_device(None) == ("Unknown device", False)
    assert describe_device("curl/8.7.1") == ("Unknown device", False)


def _request(client_host: str | None, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "headers": headers, "client": (client_host, 1234) if client_host else None})


def test_the_address_is_the_connections_unless_proxies_are_trusted():
    settings = Settings(trusted_proxy_hops=0)
    # A forged header is ignored when no proxy is trusted.
    assert _client_ip(_request("203.0.113.9", "198.51.100.1"), settings) == "203.0.113.9"
    assert _client_ip(_request(None), settings) is None


def test_with_trusted_proxies_the_address_is_counted_from_the_right():
    one = Settings(trusted_proxy_hops=1)
    # The client sent "6.6.6.6" itself; the one trusted proxy appended the address it saw.
    assert _client_ip(_request("10.0.0.2", "6.6.6.6, 198.51.100.7"), one) == "198.51.100.7"
    two = Settings(trusted_proxy_hops=2)
    assert _client_ip(_request("10.0.0.2", "6.6.6.6, 198.51.100.7, 10.0.0.1"), two) == "198.51.100.7"
    # Fewer entries than trusted proxies: fall back to the connection.
    assert _client_ip(_request("10.0.0.2", "198.51.100.7"), two) == "10.0.0.2"


def test_an_invalid_address_is_not_kept():
    assert _client_ip(_request("10.0.0.2", "<script>"), Settings(trusted_proxy_hops=1)) is None
