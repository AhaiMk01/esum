"""Generated site docs must match the code: a contract over the registry, for every built-in site this
tree has (and it must have some). On failure run `uv run esum docs <site> --write` (or
`uv run esum docs --all --write`) and commit the result; only the "## Quirks" section is hand-written."""

from esum.core.docs import MARKER, builtin_sites_dir, doc_path, extract_quirks, render_site_doc
from esum.core.registry import Registry


def builtin_registry() -> Registry:
    return Registry.discover(custom_root=None)


def test_builtin_sites_load_without_problems():
    reg = builtin_registry()
    assert reg.problems == [], "\n".join(f"{p.where}: {p.error}" for p in reg.problems)


def test_every_builtin_site_doc_is_in_sync():
    drift = []
    sites = builtin_registry().sorted_sites()
    assert sites, "no built-in site is installed: no doc was checked"
    for site in sites:
        path = doc_path(site)
        assert path is not None, "run the tests from a source checkout"
        if not path.is_file():
            drift.append(f"{site.name}: missing {path}")
            continue
        committed = path.read_text(encoding="utf-8")
        if site.kind == "markdown":
            expected = site.source.read_text(encoding="utf-8")
        else:
            expected = render_site_doc(site, extract_quirks(committed))
        if committed != expected:
            drift.append(f"{site.name}: {path} is out of date")
    assert not drift, ("site docs drifted from code; run `uv run esum docs --all --write`:\n"
                       + "\n".join(drift))


def test_no_orphan_generated_docs():
    known = {doc_path(site).resolve() for site in builtin_registry().builtins.values()}
    orphans = [str(p) for p in sorted(builtin_sites_dir().rglob("doc.md")) if p.resolve() not in known
               and p.read_text(encoding="utf-8").startswith(MARKER)]
    assert not orphans, f"generated docs without a site: {orphans}"
