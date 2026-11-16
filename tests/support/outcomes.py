"""The failure contract (D-52) as checks: a site's tests call them with that site's own command, and
tests/ calls them on synthetic sites, so the assertion is written once."""

from __future__ import annotations


def refused_before_any_request(offline_site, capsys, action_runner, argv: list[str]) -> None:
    """A malformed id or URL is ESUM's own usage error: exit 2, help that says what to pass, no request."""
    registry, backend = offline_site
    assert action_runner.cli(argv, registry=registry) == 2
    output = capsys.readouterr().out
    assert "error:" in output and "help:" in output
    help_line = output.split("help:", 1)[1]
    # ESUM's own check says what to pass, never that the site rejected anything
    assert "rejected" not in help_line
    assert help_line.lstrip().startswith("Pass ") or f"esum {argv[0]} {argv[1]} --help" in help_line
    assert not backend.requests


def missing_item(offline_site, capsys, action_runner, argv: list[str]) -> None:
    """A well-formed id the site answers 404 for: exit 1, after asking, with the no-such-item help."""
    registry, backend = offline_site
    assert action_runner.cli(argv, registry=registry) == 1
    output = capsys.readouterr().out
    assert "check the id or name" in output and "may have been removed" in output
    assert backend.requests


def layout_failure(offline_site, capsys, action_runner, argv: list[str]) -> None:
    """A page that isn't shaped as the site's pages are: exit 1 with the repair guidance."""
    assert action_runner.cli(argv, registry=offline_site[0]) == 1
    assert "esum-site-author" in capsys.readouterr().out
