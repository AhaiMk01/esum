"""Execute the published templates themselves, so copied examples cannot silently drift."""
import importlib.util
import io
import json
import re
import sys
from pathlib import Path

from esum import Options
from esum.transport import HttpClient
from esum.core.context import Context
from esum.core.registry import Registry
from conftest import json_response

TEMPLATES = Path(__file__).resolve().parents[1] / "skills/esum-site-author/references/templates.md"


def blocks(language):
    return re.findall(r"```" + language + r"\n(.*?)\n```", TEMPLATES.read_text(encoding="utf-8"), re.S)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_standalone_templates(isolated, fake_http, action_runner):
    site_code, parse_code, parser_tests, action_tests, write_code, write_tests, _ = blocks("python")
    root = isolated / "sites/mysite"
    tests = root / "tests"
    fixtures = tests / "fixtures"
    fixtures.mkdir(parents=True)
    (root / "site.py").write_text(site_code.replace("Next, Tier", "Next, Write, Tier") + "\n" + write_code, encoding="utf-8")
    (root / "parse.py").write_text(parse_code, encoding="utf-8")
    latest = {"topics": [{"id": 1, "title": "<b>Topic</b>\nname", "author": "alice", "reply_count": 1}], "next_page": True}
    thread = {"id": 1, "title": "<b>Topic</b>\nname", "author": "alice", "posts_count": 2, "highest_post_number": 4,
              "posts": [{"author": "alice", "html": "<p>Opening</p><pre>  code\n  line</pre>"},
                        {"post_number": 4, "author": "bob", "html": "<p>Reply</p>"}]}
    for name, data in (("latest.json", latest), ("thread.json", thread)):
        (fixtures / name).write_text(json.dumps(data), encoding="utf-8")
    for name, code in (("test_mysite.py", parser_tests), ("test_write.py", write_tests)):
        path = tests / name
        path.write_text(code, encoding="utf-8")
        mod = load("template_" + path.stem, path)
        for key, value in vars(mod).items():
            if key.startswith("test_"):
                value()
    registry = Registry.discover()
    try:
        s = registry.get("mysite")
        base = "https://mysite.example"
        fake_http.routes.update({base + "/api/topics?page=1": json_response(base, latest),
                                 base + "/api/topics/1": json_response(base, thread)})
        output = io.StringIO()
        action_runner(s, s.all_actions["latest"], {}, Options(), stream=output)
        assert "Topic name" in output.getvalue()
        assert "esum mysite thread" in output.getvalue()
        act = s.all_actions["thread"]
        ctx = Context(site=s, action=act, options=Options(), http=HttpClient(s, Options(), dedupe=True, backend_factory=action_runner.factory))
        try:
            result = act.func(ctx, id=1, mode="full")
            assert not result.next
            assert result.summary.endswith("2 of 2 posts shown")
            assert [p.number for p in result.data["posts"] if hasattr(p, "number")] == [1, 4]
            partial = act.func(ctx, id=1, from_=1, to=1)
            assert partial.next[0].args == {"id": 1, "mode": "full", "from_": None, "to": None}
            assert "deleted or hidden" in result.text and "  code\n" in result.text
            assert result.data["title"] == "Topic name"
        finally:
            ctx.close()
        # Execute the published whole-action example with the actual shared fixtures.
        namespace = {}
        exec(action_tests, namespace)
        fake_http.requests.clear()
        namespace["test_latest_action"](action_runner, fake_http)
        # The write template runs only against this fake backend.
        fake_http.routes[base + "/api/topics/1/bookmark"] = json_response(base, {})
        action_runner(s, s.all_actions["bookmark"], {"ids": [1]}, Options(), stream=io.StringIO())
        assert fake_http.requests[-1].method == "PUT"
    finally:
        for name in list(sys.modules):
            if name.startswith(("esum_custom_sites", "mysite_", "template_")):
                del sys.modules[name]


def test_engine_config_template(isolated, fake_http, action_runner):
    root = isolated / "sites/discourse/mysite"
    root.mkdir(parents=True)
    (root / "site.py").write_text(blocks("python")[-1], encoding="utf-8")
    registry = Registry.discover()
    try:
        s = registry.get("mysite")
        assert s.engine == "discourse" and s.transport.value == "browser_http"
        assert {"latest", "thread", "bookmark"} <= s.all_actions.keys()
        # Run the inherited T2 actions through the shared offline entry point.
        fake_http.routes["https://forum.example/latest.json"] = json_response(
            "https://forum.example/latest.json", {"topic_list": {"topics": []}, "users": []})
        fake_http.routes["https://forum.example/t/1.json"] = json_response(
            "https://forum.example/t/1.json",
            {"id": 1, "title": "Topic", "posts_count": 1, "highest_post_number": 1,
             "post_stream": {"stream": [11], "posts": [
                 {"id": 11, "post_number": 1, "username": "alice", "user_id": 1, "cooked": "<p>Opening</p>"}]}})
        action_runner(s, s.all_actions["latest"], {}, Options(), stream=io.StringIO())
        output = io.StringIO()
        action_runner(s, s.all_actions["thread"], {"id": 1}, Options(), stream=output)
        assert "Opening" in output.getvalue()
    finally:
        for name in list(sys.modules):
            if name.startswith("esum_custom_sites"):
                del sys.modules[name]


def test_markdown_site_template(isolated):
    root = isolated / "sites/mysite"
    root.mkdir(parents=True)
    (root / "SITE.md").write_text(blocks("markdown")[0], encoding="utf-8")
    s = Registry.discover().get("mysite")
    assert s.kind == "markdown" and not s.all_actions
    assert "esum docs mysite" in blocks("markdown")[1]


def test_balanced_element_template_examples():
    from esum import element_end, element_inner, remove_elements

    html = "<blockquote><blockquote>nested</blockquote>tail</blockquote><p>after</p>"
    namespace = {"html": html, "opening_pattern": r"<blockquote\b[^>]*>",
                 "pattern": r"<blockquote\b[^>]*>", "opening_offset": 0,
                 "element_end": element_end, "element_inner": element_inner,
                 "remove_elements": remove_elements}
    text = TEMPLATES.read_text(encoding="utf-8")
    expected = {"element_inner": "<blockquote>nested</blockquote>tail",
                "element_end": html.index("<p>"), "remove_elements": "<p>after</p>"}
    for helper, result in expected.items():
        expression = re.search(r"`(" + helper + r"\(html, [^`]+\))`", text).group(1)
        assert eval(expression, namespace) == result
