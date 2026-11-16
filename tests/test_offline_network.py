"""The offline suite cannot silently turn a missing fake into live traffic."""
import socket

import httpx
import pytest
from curl_cffi import requests


@pytest.mark.parametrize("client", ["httpx", "curl_cffi", "socket", "connect_ex"])
def test_outbound_network_is_refused(client):
    with pytest.raises(pytest.fail.Exception, match="offline test attempted outbound network"):
        if client == "httpx":
            with httpx.Client(trust_env=False) as http:
                http.get("https://example.com/")
        elif client == "curl_cffi":
            requests.get("https://example.com/")
        else:
            with socket.socket() as sock:
                getattr(sock, "connect" if client == "socket" else "connect_ex")(("203.0.113.1", 443))
