import pytest

from esum.core.urls import is_absolute, is_web_url


@pytest.mark.parametrize("url, web", [
    ("https://forum.example/t/1", True),
    ("HTTP://Forum.Example", True),
    ("https://[2001:db8::1]:8443/x", True),
    ("https://user@forum.example/", True),
    ("file:///C:/Users/x", False),
    ("ftp://forum.example/a", False),
    ("https://", False),
    ("//forum.example/x", False),
    ("forum.example", False),
    ("javascript:alert(1)", False),
    ("http://[bad/x", False),
    ("https://[::1", False),
])
def test_is_web_url(url, web):
    assert is_web_url(url) is web


@pytest.mark.parametrize("url, absolute", [
    ("https://forum.example/x", True),
    ("FILE:///etc/passwd", True),
    ("chrome-extension://abc/x", True),
    ("/t/1.json", False),
    ("t/1.json", False),
    ("search?q=a://b", False),
    ("//forum.example/x", False),
    ("mailto:x@example.com", False),
])
def test_is_absolute(url, absolute):
    assert is_absolute(url) is absolute
