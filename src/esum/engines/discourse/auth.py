"""Discourse User API Keys: the user authorizes ESUM in their browser, and ESUM keeps the key.

The flow follows nitan-mcp's src/user-api-key-generator.ts (MIT), in fresh Python, and also checks the
nonce. `esum <site> login` is interactive, so the user runs it in their own terminal. It makes an RSA
key pair and an authorization URL; the user logs in themselves and approves the scopes they chose;
Discourse shows a payload encrypted to the public key, which they paste back. ESUM never sees a
password, and the private key lives only in that process's memory (D-46).

The key is kept by `core/keystore.py` (the OS credential store, or a user-only file; D-79). It
is not printed or logged. The key goes only in request headers to the site itself, never into a page:
T3 runs inside a site page, whose content is untrusted (request credentials are dropped by transport before T3).
"""

from __future__ import annotations

import base64
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode


from esum import ArgumentError
from esum.core import keystore

SCOPES = ("read", "write")     # what ESUM's actions can use; the user picks when they log in
APPLICATION_NAME = "ESUM"
PAYLOAD_CHARS = 344            # base64 of one 2048-bit RSA block; Discourse may wrap it over lines


@dataclass(repr=False)
class PendingLogin:
    """One login in progress. Kept in memory only, so the private key dies with the process."""

    url: str
    private_key: object
    client_id: str
    nonce: str
    scopes: list[str]


def load_key(site: str) -> dict | None:
    """The saved `{key, client_id, scopes, created}`, or None."""
    saved = keystore.load(site)
    return saved if saved and saved.get("key") and saved.get("client_id") else None


def headers(saved: dict) -> dict[str, str]:
    return {"User-Api-Key": saved["key"], "User-Api-Client-Id": saved["client_id"]}


def parse_scopes(value: str) -> list[str]:
    wanted = [x.strip() for x in value.split(",") if x.strip()]
    unknown = [x for x in wanted if x not in SCOPES]
    if unknown or not wanted:
        raise ArgumentError(f"scopes {value!r}: use read, or read,write",
                            help="read reads what the account can see; write also lets ESUM's write actions use "
                                 "the key")
    return ["read", *(x for x in wanted if x != "read")]   # every use starts with reading


def start(base_url: str, scopes: list[str], saved: dict | None) -> PendingLogin:
    """A new key pair and its authorization URL. A saved key's client id is reused, so Discourse
    replaces that key instead of leaving an old one active."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    client_id = saved["client_id"] if saved else f"esum-{uuid.uuid4()}"
    nonce = secrets.token_hex(16)
    url = f"{base_url}/user-api-key/new?" + urlencode({
        "application_name": APPLICATION_NAME, "client_id": client_id, "scopes": ",".join(scopes),
        "public_key": public_pem, "nonce": nonce})
    return PendingLogin(url, private, client_id, nonce, scopes)


def finish(site: str, pending: PendingLogin, payload: str) -> dict:
    """Decrypt the payload Discourse showed, check its nonce and save the key."""
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        clear = pending.private_key.decrypt(base64.b64decode("".join(payload.split())), padding.PKCS1v15())
        answer = json.loads(clear)
    except ValueError:
        raise ArgumentError("that payload can't be decrypted",
                            help="copy the whole payload from the page this login opened") from None
    if answer.get("nonce") != pending.nonce or not answer.get("key"):
        raise ArgumentError("that payload doesn't belong to this login",
                            help="approve the URL this login printed, not an older one")
    saved = {"key": answer["key"], "client_id": pending.client_id, "scopes": pending.scopes,
             "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    keystore.save(site, saved)
    return saved
