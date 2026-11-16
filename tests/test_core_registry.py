import pytest

from esum.core.registry import Registry, parse_frontmatter

from conftest import DEMO_SITE, NOTES_MD, write
from esum import engine
from esum import site
from esum.cli import main
from esum.core import registry as registry_mod
from esum.core.paths import writes_log
import json
import sys


def test_discovers_engines_builtin_and_custom_sites(example_registry):
    reg = example_registry
    assert reg.problems == []
    assert sorted(reg.engines) == ["demoeng"]
    assert reg.names() == ["demo", "notes", "one", "two"]
    assert sorted(reg.sites) == ["notes", "one"]  # custom modules are imported on first get()
    assert sorted(reg.pending) == ["demo", "two"]
    one, two, demo, notes = (reg.get(n) for n in ("one", "two", "demo", "notes"))
    assert one.builtin and not two.builtin and not demo.builtin and not notes.builtin
    assert sorted(one.all_actions) == ["extra", "topic"]
    assert sorted(two.all_actions) == ["topic"]
    assert one.engine_obj is two.engine_obj is reg.engines["demoeng"]
    assert demo.engine is None and demo.kind_label == "standalone"
    assert demo.format == 1 and demo.ported_from == "tests only"
    assert notes.kind == "markdown" and notes.description == "A Markdown-only demo site"
    assert notes.meta["verified"] == "2026-10-26"


def test_every_installed_builtin_site_loads_cleanly_in_format_1():
    """A contract over the registry: whichever built-in sites this tree has, and it has some."""
    reg = Registry.discover(custom_root=None)
    assert reg.problems == [], reg.problems
    assert reg.sites, "no built-in site is installed: nothing was checked"
    for s in reg.sites.values():
        assert s.builtin and s.format == 1


def test_broken_and_mismatched_modules_become_problems(example_tree):
    root = example_tree["custom_root"]
    write(root / "broken" / "site.py", "raise RuntimeError('kaput')\n")
    write(root / "future" / "site.py",
          DEMO_SITE.replace("FORMAT = 1", "FORMAT = 99").replace('name="demo"', 'name="future"'))
    write(root / "noformat" / "site.py",
          "from esum import site\nnoformat = site(name='noformat', base_url='https://n.invalid', label='N')\n")
    write(root / "demoeng" / "wrong" / "site.py",
          "from esum import site\nFORMAT = 1\nwrong = site(name='wrong', base_url='https://w.invalid', label='W')\n")
    write(root / "orphan" / "site.py",
          "from esum import site\nFORMAT = 1\norphan = site(name='orphan', engine='nope', "
          "base_url='https://o.invalid', label='O')\n")
    write(root / "badmd" / "SITE.md", "---\nname: badmd\nformat: 2\ndescription: x\n---\n")
    (root / "empty").mkdir()
    reg = Registry.discover(**example_tree)
    for name in ("broken", "future", "noformat", "wrong", "orphan"):
        assert reg.get(name) is None  # import problems show up when the site is used
    errors = {p.where.replace("\\", "/").split("/sites/")[-1]: p.error for p in reg.problems}
    assert "kaput" in errors["broken/site.py"]
    assert "upgrade esum" in errors["future/site.py"]
    assert "no FORMAT" in errors["noformat/site.py"]
    assert "declares engine=None" in errors["demoeng/wrong/site.py"]
    assert "not installed" in errors["orphan/site.py"]
    assert "upgrade esum" in errors["badmd/SITE.md"]
    assert "no site.py or SITE.md" in errors["empty"]
    assert not {"broken", "future", "noformat", "wrong", "orphan", "badmd"} & set(reg.names())
    assert reg.get("demo") is not None  # one bad module never hides the others


def test_custom_site_supports_relative_imports(example_tree):
    root = example_tree["custom_root"]
    write(root / "mine" / "helper.py", "WORD = 'relative import works'\n")
    write(root / "mine" / "site.py", '''
        from esum import Tier, action, site
        from .helper import WORD
        FORMAT = 1
        mine = site(name="mine", base_url="https://custom-mine.invalid", label=WORD)
    ''')
    reg = Registry.discover(**example_tree)
    assert reg.problems == []
    assert reg.get("mine").description == "relative import works"
    assert not reg.get("mine").builtin


@pytest.mark.parametrize("folder", ["one", "demoeng/one"])
def test_custom_site_never_replaces_a_builtin(example_tree, folder):
    """A custom folder named after a built-in site is a problem; its code is never imported."""
    root = example_tree["custom_root"]
    write(root / folder / "site.py", "raise RuntimeError('never imported')\n")
    reg = Registry.discover(**example_tree)
    assert reg.get("one") is reg.builtins["one"] and "one" not in reg.pending
    [problem] = reg.problems
    assert "'one' is a built-in site" in problem.error and "rename this folder" in problem.error
    assert problem.where == str(root / folder)


def test_missing_custom_root_is_fine(tmp_path):
    reg = Registry.discover(custom_root=tmp_path / "nope")
    assert reg.problems == []


def test_frontmatter():
    assert parse_frontmatter("---\na: 1\nb: 'x: y'\n---\nbody") == {"a": "1", "b": "x: y"}
    assert parse_frontmatter("no frontmatter") == {}
    assert parse_frontmatter("---\na: 1\n") == {}


@pytest.mark.parametrize("body", ["---\nname: one\n---\nno description\n",
                                  "---\nname: one\nformat: 1\ndescription: mine\n---\nsteps\n"])
def test_custom_markdown_site_never_replaces_a_builtin(example_tree, body):
    write(example_tree["custom_root"] / "one" / "SITE.md", body)
    reg = Registry.discover(**example_tree)
    assert reg.get("one") is reg.builtins["one"]
    assert len(reg.problems) == 1


# --- Regression for review-r1 #1: custom site modules are imported only when their own site is used ---

EVIL_SITE = '''
from pathlib import Path
Path(__file__).with_name("ran").write_text("imported")
'''


WRITER_SITE = '''
from esum import Tier, Write, action, site

FORMAT = 1

writer = site(name="writer", base_url="https://writer.invalid", label="Built-in-style writer")


@action(writer, tier=Tier.REVERSIBLE)
def bookmark(ctx, ids: list[int]):
    """Bookmark topics."""
    return [Write(f"Bookmark #{i} on writer", lambda i=i: f"bookmarked {i}") for i in ids]
'''


def _log() -> list[dict]:
    return [json.loads(line) for line in writes_log().read_text(encoding="utf-8").splitlines()]


def _custom_imported() -> bool:
    return any(m.startswith(registry_mod.CUSTOM_PACKAGE + ".") for m in sys.modules)


def _plant_evil(isolated):
    write(isolated / "sites" / "evil" / "site.py", EVIL_SITE)
    return isolated / "sites" / "evil" / "ran"


def test_builtin_write_never_imports_custom_code(example_tree, tmp_path, isolated, capsys):
    write(tmp_path / "pkgs" / "esumtest_sites" / "writer.py", WRITER_SITE)
    marker = _plant_evil(isolated)
    reg = Registry.discover(**example_tree)
    assert main(["writer", "bookmark", "--ids", "1", "2"], registry=reg) == 0
    assert "done 2. Bookmark #2 on writer" in capsys.readouterr().out
    assert not marker.exists() and not _custom_imported()
    entries = _log()
    assert [e["kind"] for e in entries] == ["write", "write-result", "write-result"]
    assert all(e["custom_code"] == [] for e in entries)


def test_no_installed_builtin_sites_help_or_doc_imports_custom_code(isolated, capsys):
    """A contract over the registry: every built-in site's help, each action's help and its doc."""
    marker = _plant_evil(isolated)
    reg = Registry.discover()
    assert reg.builtins, "no built-in site is installed: nothing was checked"
    for name, site in sorted(reg.builtins.items()):
        assert main([name, "--help"], registry=reg) == 0
        for act in site.all_actions:
            assert main([name, act, "--help"], registry=reg) == 0
        assert main(["docs", name], registry=reg) == 0
    assert main(["--help"], registry=reg) == 0
    assert not marker.exists() and not _custom_imported()


def test_list_shows_custom_sites_without_importing_them(example_tree, isolated, capsys):
    marker = _plant_evil(isolated)
    reg = Registry.discover(**example_tree)
    assert main(["list"], registry=reg) == 0
    out = capsys.readouterr().out
    assert any(line.startswith("  evil*,standalone,?,")
               for line in out.splitlines())
    assert any(line.startswith("  two*,demoeng,") for line in out.splitlines())
    assert main(["list", "--json"], registry=reg) == 0
    sites = {s["name"]: s for s in json.loads(capsys.readouterr().out)["sites"]}
    assert {"demo", "evil", "notes", "one", "two"} <= set(sites)
    assert sites["evil"]["loaded"] is False and sites["evil"]["actions"] is None
    assert sites["notes"]["loaded"] is True  # Markdown is parsed, never executed
    assert not marker.exists() and not _custom_imported()


def test_invoked_custom_site_loads_alone_and_is_logged(example_tree, isolated, fake_http, capsys, action_runner):
    from esum.transport import Response
    fake_http.routes["https://demo.invalid/t/1/bookmark"] = Response(200, "https://demo.invalid/t/1/bookmark", {}, b"{}")
    marker = _plant_evil(isolated)
    reg = Registry.discover(**example_tree)
    assert action_runner.cli(["demo", "bookmark", "--ids", "1"], registry=reg) == 0
    assert not marker.exists()
    assert set(reg.pending) == {"evil", "two"}
    entry = _log()[-1]
    assert [p.replace("\\", "/").split("/sites/")[-1] for p in entry["custom_code"]] == ["demo/site.py"]


def test_broken_custom_site_fails_with_the_repair_step(example_tree, isolated, capsys):
    write(isolated / "sites" / "mine" / "site.py", "raise RuntimeError('kaput')\n")
    reg = Registry.discover(**example_tree)
    # a broken module is an error, not a usage mistake: exit 1, and help names the repair (AXI §6, §9)
    assert main(["mine", "extra"], registry=reg) == 1
    out = capsys.readouterr().out
    assert out.startswith("error: site 'mine' failed to load\n") and "kaput" in out
    assert "help: Fix that module (the esum-site-author skill repairs a site)" in out


@pytest.mark.parametrize("typed", ["sites", "Bad", "o", "esum"])
def test_an_unknown_name_is_unknown_even_when_some_module_is_broken(example_tree, isolated, capsys, typed):
    """A broken module's path contains many names; only its own site name reports it."""
    write(isolated / "sites" / "Bad_Name" / "site.py", "x = 1\n")
    write(isolated / "sites" / "mine" / "site.py", "raise RuntimeError('kaput')\n")
    reg = Registry.discover(**example_tree)
    reg.get("mine")  # load the broken one too, so its problem is on record
    assert main([typed, "x"], registry=reg) == 2
    assert capsys.readouterr().out.startswith(f"error: unknown site or command '{typed}'\n")


def test_a_custom_folder_with_a_bad_name_reports_it_by_that_name(example_tree, isolated, capsys):
    write(isolated / "sites" / "Bad_Name" / "site.py", "x = 1\n")
    reg = Registry.discover(**example_tree)
    assert main(["Bad_Name", "x"], registry=reg) == 1
    assert "must match" in capsys.readouterr().out


def test_custom_site_name_is_its_folder_name(example_tree, isolated):
    write(isolated / "sites" / "folder" / "site.py",
          "from esum import site\nFORMAT = 1\nother = site(name='other', base_url='https://o.invalid', "
          "label='O')\n")
    write(isolated / "sites" / "Bad_Name" / "site.py", "raise RuntimeError('never imported')\n")
    reg = Registry.discover(**example_tree)
    assert "folder" in reg.names() and "other" not in reg.names()
    assert any("Bad_Name" in p.where for p in reg.problems)
    assert reg.get("folder") is None
    assert any("defines no site named 'folder'" in p.error for p in reg.problems)


# --- Registry checks on the module contract (review P1, P2) ------------------------------------------

def _problems(reg: Registry) -> dict[str, str]:
    """{file name: error}. A built-in module's problems name the module, others the file."""
    def key(where: str) -> str:
        last = where.replace("\\", "/").split("/")[-1]
        return last if last.endswith(".py") else last.split(".")[-1] + ".py"
    return {key(p.where): p.error for p in reg.problems}


def _sites(example_tree):
    return example_tree["custom_root"].parents[1] / "pkgs" / "esumtest_sites"


def test_builtin_scan_never_imports_a_sites_tests_folder(example_tree):
    # A built-in site keeps its offline tests and fixtures in its own tests/ folder.
    one = _sites(example_tree) / "demoeng" / "one"
    write(one / "tests" / "test_one.py", "raise RuntimeError('the registry imported a tests folder')")
    write(one / "tests" / "fixtures" / "SITE.md", NOTES_MD)
    reg = Registry.discover(**example_tree)
    assert reg.problems == [] and reg.get("one") is not None
    assert not any(".tests" in m for m in sys.modules if m.startswith("esumtest_"))


# --- extras ------------------------------------------------------------------------------------------

def test_unknown_extras_key_on_an_engine_site_is_a_problem_not_an_error(example_tree):
    write(_sites(example_tree) / "demoeng" / "typo.py", """
        from esum import site
        FORMAT = 1
        typo = site(name="typo", engine="demoeng", base_url="https://t.invalid", label="T",
                    extras={"browser_cookie": False, "min_interval": 1, "login_check": {"status": [401]}})
    """)
    reg = Registry.discover(**example_tree)
    assert reg.get("typo") is not None  # still usable
    error = _problems(reg)["typo.py"]
    assert "unknown extras key(s) 'browser_cookie'" in error
    assert "min_interval" in error.split("known:")[1]  # the transport keys are known…
    assert "'min_interval'" not in error.split("known:")[0]  # …and not reported


def test_engine_and_site_modules_declare_their_extras(example_tree):
    engines = example_tree["custom_root"].parents[1] / "pkgs" / "esumtest_engines"
    write(engines / "keyed.py", """
        from esum import engine
        FORMAT = 1
        EXTRAS = ("forums",)
        keyed = engine("keyed", description="Engine that reads extras['forums']")
    """)
    write(_sites(example_tree) / "keyed" / "fine.py", """
        from esum import site
        FORMAT = 1
        EXTRAS = ("preset",)
        fine = site(name="fine", engine="keyed", base_url="https://f.invalid", label="F",
                    extras={"forums": [], "preset": 1, "browser_home": "/forum/"})
        bad = site(name="bad", engine="keyed", base_url="https://b.invalid", label="B",
                   extras={"forum": []})
    """)
    reg = Registry.discover(**example_tree)
    errors = [p.error for p in reg.problems]
    assert errors == ["site 'bad': unknown extras key(s) 'forum'; known: browser_cookies, "
                      "browser_home, challenge_text, forums, key_entry, login_check, min_interval, preset"]


def test_standalone_sites_are_checked_only_when_their_module_declares_extras(example_tree):
    write(_sites(example_tree) / "loose.py", """
        from esum import site
        FORMAT = 1
        loose = site(name="loose", base_url="https://l.invalid", label="L", extras={"anything": 1})
    """)
    write(_sites(example_tree) / "strict.py", """
        from esum import site
        FORMAT = 1
        EXTRAS = ("mode",)
        strict = site(name="strict", base_url="https://s.invalid", label="S",
                      extras={"mode": 1, "mod": 2, "browser_cookies": False})
    """)
    write(_sites(example_tree) / "badkeys.py", """
        from esum import site
        FORMAT = 1
        EXTRAS = "mode"
        badkeys = site(name="badkeys", base_url="https://k.invalid", label="K")
    """)
    reg = Registry.discover(**example_tree)
    errors = _problems(reg)
    assert "loose.py" not in errors
    assert "unknown extras key(s) 'mod';" in errors["strict.py"]
    assert "EXTRAS must be a tuple" in errors["badkeys.py"]
    assert {"loose", "strict", "badkeys"} <= set(reg.names())


def test_custom_engine_site_extras_are_checked_when_loaded(example_tree):
    write(example_tree["custom_root"] / "demoeng" / "three" / "site.py", """
        from esum import site
        FORMAT = 1
        three = site(name="three", engine="demoeng", base_url="https://3.invalid", label="3",
                     extras={"min_intervall": 1})
    """)
    reg = Registry.discover(**example_tree)
    assert reg.problems == []  # nothing imported yet
    assert reg.get("three") is not None
    assert "'min_intervall'" in _problems(reg)["site.py"]


def test_every_installed_builtin_site_declares_every_extras_key_it_sets():
    """A contract over the registry: an undeclared key is a problem, and some site here sets extras."""
    reg = Registry.discover(custom_root=None)
    assert reg.problems == []
    assert any(s.extras for s in reg.sites.values()), "no installed built-in site sets extras: nothing was checked"


# --- registration origin -----------------------------------------------------------------------------

FACTORY = """
from esum import site


def forum(name, **kw):
    return site(name=name, engine="demoeng", base_url=f"https://{name}.invalid", label=name, **kw)
"""


def test_site_from_a_factory_is_a_problem_not_silently_dropped(example_tree):
    write(_sites(example_tree) / "_factory.py", FACTORY)  # private: never scanned itself
    write(_sites(example_tree) / "made.py", """
        from esumtest_sites._factory import forum
        FORMAT = 1
        made = forum("made")
    """)
    reg = Registry.discover(**example_tree)
    assert reg.get("made") is None
    error = _problems(reg)["made.py"]
    assert "site 'made' was created in module 'esumtest_sites._factory'" in error
    assert "origin=__name__" in error


def test_origin_override_registers_a_factory_site(example_tree):
    write(_sites(example_tree) / "_factory.py", FACTORY)
    write(_sites(example_tree) / "made.py", """
        from esumtest_sites._factory import forum
        FORMAT = 1
        made = forum("made", origin=__name__)
    """)
    reg = Registry.discover(**example_tree)
    assert reg.problems == []
    made = reg.get("made")
    assert made is not None and made.origin == "esumtest_sites.made" and made.format == 1


def test_imports_of_registered_objects_are_not_problems(example_tree):
    write(_sites(example_tree) / "again.py", """
        from esumtest_engines.demoeng import demoeng
        from esumtest_sites.demoeng.one import one
        FORMAT = 1
    """)
    reg = Registry.discover(**example_tree)
    assert reg.problems == []
    assert reg.get("one").origin == "esumtest_sites.demoeng.one"


def test_origin_must_be_a_module_name():
    assert site("x", base_url="https://x.invalid", label="X").origin == __name__
    assert site("x", base_url="https://x.invalid", label="X", origin="pkg.mod").origin == "pkg.mod"
    assert engine("e", description="E", origin="pkg.eng").origin == "pkg.eng"
    with pytest.raises(TypeError, match="origin must be a module name"):
        site("x", base_url="https://x.invalid", label="X", origin="")
