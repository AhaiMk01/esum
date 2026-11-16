"""Stored site credentials, one per site (D-46, D-79).

Two stores hold keys. `OsStore` is the operating system's credential store through `keyring`
(Windows Credential Manager, macOS Keychain, Linux Secret Service), under the service `esum`.
`FileStore` is `<ESUM_HOME>/auth/<site>.json`, readable by the user alone where the OS supports it
and written atomically; it is used where no OS store works, or with `ESUM_KEYSTORE=file`.

A site's key lives in exactly one of them. The OS store can fail in a way a file can't: it may hold
a key and refuse to change it, or not answer at all. That is `Unavailable`, never mistaken for "no
key here". Reads degrade (an unanswering store reads as the file's answer); changes fail closed
(`KeystoreError`), because a key the store still holds would later shadow a file written beside it.

`load`, `save`, `delete` and `entries` are the whole API sites use; each site's key is serialized
with a per-site lock, so a concurrent login and a removal can't interleave. Callers never print or log
what they load; the agent never reads `<ESUM_HOME>/auth/` or the OS store.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from filelock import FileLock

from esum.core.errors import KeystoreError
from esum.core.paths import esum_home

STORE_ENV = "ESUM_KEYSTORE"     # `file` keeps keys in files even where a credential store works
META = ("created", "scopes")    # what `esum keys` shows; never the key
STORE_HELP = ("Unlock or repair the OS credential store (Credential Manager, Keychain or Secret Service) and "
              "rerun; ESUM_KEYSTORE=file keeps keys in files instead")


class Unavailable(Exception):
    """The OS store would not do what was asked, and why (`kept …`, `did not answer …`)."""


def _lock(name: str) -> FileLock:
    directory = esum_home() / "locks" / "auth"
    directory.mkdir(parents=True, exist_ok=True)
    return FileLock(str(directory / f"{name}.lock"))


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(value, f)
    os.replace(tmp, path)


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _meta(value: dict) -> dict:
    return {k: value[k] for k in META if k in value}


# --- the two stores ---------------------------------------------------------------------------

class FileStore:
    """`<ESUM_HOME>/auth/<site>.json`. It always answers."""

    name = "file"

    def path(self, site: str) -> Path:
        return esum_home() / "auth" / f"{site}.json"

    def get(self, site: str) -> dict | None:
        return _read(self.path(site))

    def put(self, site: str, value: dict) -> None:
        _write(self.path(site), value)

    def remove(self, site: str) -> None:
        self.path(site).unlink(missing_ok=True)

    def sites(self) -> dict[str, dict]:
        folder = esum_home() / "auth"
        found = {p.stem: _read(p) for p in sorted(folder.glob("*.json"))} if folder.is_dir() else {}
        return {site: _meta(value) for site, value in found.items() if value is not None}


class OsStore:
    """The OS credential store. Every method either does what it says or raises `Unavailable`.
    The store can't list its entries, so `<ESUM_HOME>/keys.json` records which sites have one, with
    their dates and scopes and never a key; it changes only after the store itself did."""

    name = "keyring"

    def __init__(self, keyring) -> None:
        self._kr = keyring

    def get(self, site: str) -> dict | None:
        try:
            text = self._kr.get_password(_service(), site)
        except Exception as error:
            raise Unavailable(f"did not answer ({type(error).__name__})") from None
        try:
            value = json.loads(text) if text else None
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def put(self, site: str, value: dict) -> None:
        try:
            self._kr.set_password(_service(), site, json.dumps(value))
        except Exception as error:
            raise Unavailable(f"did not take the key ({type(error).__name__})") from None
        self._record(site, _meta(value))

    def remove(self, site: str) -> None:
        """Make sure the store holds no entry for `site`. A refused delete counts only when the
        entry is then seen to be absent; a store that also won't say is `Unavailable`."""
        try:
            self._kr.delete_password(_service(), site)
        except Exception as error:
            try:
                present = self._kr.get_password(_service(), site) is not None
            except Exception:
                raise Unavailable(f"did not answer, so the saved {site} key may still be there "
                                  f"({type(error).__name__})") from None
            if present:
                raise Unavailable(f"kept the saved {site} key ({type(error).__name__})") from None
        self._record(site, None)

    def sites(self) -> dict[str, dict]:
        return _read(_index_path()) or {}

    def _record(self, site: str, meta: dict | None) -> None:
        with _lock("index"):
            index = self.sites()
            if meta is None:
                if site not in index:
                    return
                del index[site]
            else:
                index[site] = meta
            _write(_index_path(), index)


def _index_path() -> Path:
    return esum_home() / "keys.json"


def _service() -> str:
    """One entry per ESUM_HOME, so a second home (tests, a sandbox) never sees the first's keys."""
    home = esum_home()
    return "esum" if home == Path.home() / ".esum" else f"esum ({home})"


def _keyring():
    """The working `keyring` backend, or None: forced off, not installed, or no real store here."""
    if os.environ.get(STORE_ENV, "").lower() == "file":
        return None
    try:
        import keyring
        backend = keyring.get_keyring()
    except Exception:
        return None
    try:
        return keyring if backend.priority >= 1 else None   # fail/null backends sit below 1
    except Exception:
        return None


def _stores() -> tuple[OsStore | None, FileStore]:
    kr = _keyring()
    return (OsStore(kr) if kr is not None else None), FileStore()


def store_name() -> str:
    """Where a new key goes: `keyring` or `file`."""
    return OsStore.name if _keyring() else FileStore.name


# --- the API sites use: where a key lives, and what a failing store means --------------------------

def _current(system: OsStore | None, files: FileStore, site: str) -> dict | None:
    """The saved key: the OS store's, else the file's. A store that doesn't answer reads as empty,
    so a read never fails; changes check the store again and fail closed."""
    if system is not None:
        try:
            value = system.get(site)
        except Unavailable:
            value = None
        if value is not None:
            return value
    return files.get(site)


def _clear(system: OsStore, site: str) -> None:
    try:
        system.remove(site)
    except Unavailable as why:
        raise KeystoreError(f"the OS credential store {why}", help=STORE_HELP) from None


def load(site: str) -> dict | None:
    """The saved credential, or None. A key file from before moves into the OS store."""
    system, files = _stores()
    value = _current(system, files, site)
    if system is not None and value is not None and files.get(site) == value:
        with _lock(f"{site}.json"):
            if files.get(site) == value:
                try:
                    system.put(site, value)
                except Unavailable:
                    return value          # it stays a file; nothing was lost
                files.remove(site)
    return value


def save(site: str, value: dict) -> str:
    """Save a site's credential; returns where it went (`keyring` or `file`). It goes to a file
    only when the OS store holds nothing for the site: otherwise that older key would shadow it."""
    system, files = _stores()
    with _lock(f"{site}.json"):
        if system is not None:
            try:
                system.put(site, value)
            except Unavailable:
                _clear(system, site)
            else:
                files.remove(site)
                return system.name
        files.put(site, value)
        return files.name


def delete(site: str, *, expected: dict | None = None) -> bool:
    """Delete a site's credential. With `expected`, only if it is still that one (a login may have
    replaced it meanwhile). True when something was deleted; KeystoreError, with nothing changed,
    when the OS store won't let go of it."""
    system, files = _stores()
    with _lock(f"{site}.json"):
        current = _current(system, files, site)
        if current is None or (expected is not None and current != expected):
            return False
        if system is not None:
            _clear(system, site)
        files.remove(site)
        return True


def entries() -> list[dict]:
    """Every saved credential's site, store, date and scopes, sorted by site. Never a key."""
    system, files = _stores()
    rows = {site: {"site": site, "store": files.name, **meta} for site, meta in files.sites().items()}
    if system is not None:
        rows |= {site: {"site": site, "store": system.name, **meta} for site, meta in system.sites().items()}
    return [rows[site] for site in sorted(rows)]

