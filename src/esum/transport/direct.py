"""T1 direct HTTP: httpx, no browser. Used for public APIs (HN Algolia, WordPress REST, public
Discourse reads). Retries, caching and decoding happen in `HttpClient`; this only sends."""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

import httpx

from .. import __version__
from . import Request, Response

if TYPE_CHECKING:
    from ..core.context import Options
    from ..core.model import Site

USER_AGENT = f"Mozilla/5.0 (compatible; esum/{__version__})"


class DirectBackend:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(follow_redirects=True,
                                              headers={"User-Agent": USER_AGENT})

    def send(self, request: Request, *, before_send: Callable[[], None] = lambda: None) -> Response:
        before_send()
        r = self._client.request(request.method, request.url, headers=request.headers, content=request.content,
                                 timeout=request.timeout, follow_redirects=request.follow_redirects)
        headers: dict[str, str] = {}
        for name, value in r.headers.multi_items():
            name = name.lower()
            headers[name] = f"{headers[name]}, {value}" if name in headers else value
        return Response(status=r.status_code, url=str(r.url), headers=headers, content=r.content)

    def close(self) -> None:
        self._client.close()


def make_backend(site: "Site", options: "Options") -> DirectBackend:
    return DirectBackend()
