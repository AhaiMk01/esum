"""Body fidelity and deliberate inline-label collapse, including FORMAT 1 compatibility."""

import json

import pytest

from esum.core.output import dumps
from esum.core.posts import Post, format_post
from esum.core.text import html_to_body, html_to_label, html_to_text, sanitize_text, squeeze

URL = "https://x.invalid/a.png"


@pytest.mark.parametrize("keep_links", [False, True])
def test_named_jobs_keep_content_but_labels_collapse_structure(keep_links):
    from esum import html_to_body, html_to_label, html_to_text

    html = ('<p>A&amp;B&nbsp; <a href="https://x.invalid/">link</a></p>'
            '<pre>a  &lt; b\n\n    c </pre><p><s>old</s><code>x  y</code></p>'
            '<ol start="5"><li>first<ul><li>nested</li></ul></li><li>next</li></ol>'
            '<p><img src="https://x.invalid/a.png" alt="pic">'
            '<img src="https://x.invalid/icon.png" width="1" height="1"></p>'
            '<script>discard me</script>')
    link = 'link (https://x.invalid/)' if keep_links else 'link'
    body = (f'A&B {link}\n```\na  < b\n\n    c \n```\n~~old~~x y\n'
            '5. first\n  - nested\n6. next\n[image: pic https://x.invalid/a.png]')
    label = (f'A&B {link} ``` a < b c ``` ~~old~~x y '
             '5. first - nested 6. next [image: pic https://x.invalid/a.png]')
    assert html_to_body(html, keep_links=keep_links) == body
    assert html_to_text(html, keep_links=keep_links) == body
    assert html_to_label(html, keep_links=keep_links) == label


def test_label_requires_link_policy_and_removes_row_controls():
    with pytest.raises(TypeError, match='keep_links'):
        html_to_label('text')
    assert html_to_label('<p>one\x1b[31m\x00\ttwo</p><p>three</p>', keep_links=False) == 'one two three'
    assert html_to_label('<blockquote>a<br>b</blockquote>', keep_links=False) == '> a > b'
    assert html_to_label('', keep_links=False) == ''


def test_named_jobs_share_link_cap_and_compatibility_defaults():
    html = '<a href="https://x.invalid/very-long">name</a>'
    body = html_to_body(html, url_cap=12)
    assert html_to_text(html, url_cap=12) == body
    assert html_to_label(html, keep_links=True, url_cap=12) == body
    assert html_to_label(html, keep_links=False) == 'name'


def img(attrs: str) -> str:
    return html_to_text(f'<p>before <img src="{URL}" {attrs}> after</p>')


@pytest.mark.parametrize("size", ['width="1" height="1"', 'width="0" height="0"', "width=1 height=1",
                                  'width="1px" height="1PX"', 'width="47" height="47"',
                                  'width="16" height="16.5"'])
def test_declared_tiny_image_without_alt_prints_nothing(size):
    assert img(size) == "before after"
    assert img(f'{size} alt=""') == "before after"
    assert img(f'{size} alt="  "') == "before after"


def test_small_icon_alone_in_a_post_leaves_no_line():
    assert html_to_text(f'<p><img src="{URL}" width="16" height="16"></p><p>text</p>') == "text"


def test_tiny_image_with_alt_keeps_the_alt_without_the_url():
    assert img('width="16" height="16" alt="已验证"') == "before [image: 已验证] after"


@pytest.mark.parametrize("size", ['width="48" height="48"', 'width="80" height="80"',
                                  'width="95" height="39"', 'width="1000" height="1"'])
def test_image_with_a_side_of_48_or_more_is_kept(size):
    assert img(size) == f"before [image: {URL}] after"
    assert img(f'{size} alt="图"') == f"before [image: 图 {URL}] after"


@pytest.mark.parametrize("size", ["", 'width="16"', 'height="16"', "width", 'width="" height=""'])
def test_undeclared_size_is_kept(size):
    assert img(size) == f"before [image: {URL}] after"


@pytest.mark.parametrize("size", ['width="1%" height="1%"', 'width="100%" height="1"',
                                  'width="1em" height="1em"', 'width="auto" height="auto"',
                                  'width="-1" height="-1"'])
def test_a_size_that_is_not_pixels_is_unknown(size):
    assert img(size) == f"before [image: {URL}] after"


@pytest.mark.parametrize("style", ["width:1px;height:1px", "width: 100%", "color:red; height:auto"])
def test_css_sizing_overrides_tiny_attributes(style):
    assert img(f'width="1" height="1" style="{style}"') == f"before [image: {URL}] after"
    assert img(f'style="{style}"') == f"before [image: {URL}] after"


def test_css_that_does_not_size_the_image_leaves_it_tiny():
    assert img('width="1" height="1" style="max-width:100%;border:0"') == "before after"


@pytest.mark.parametrize("attrs", ['width="15" height="15"', 'width="15" height="15" smilieid="3" alt="笑"'])
def test_smiley_keeps_its_name_at_any_size(attrs):
    html = f'<img src="https://x.invalid/smilies/default/grin.gif" {attrs}>'
    assert html_to_text(html) == ":grin:"


def test_tiny_image_in_a_link_keeps_the_link():
    html = f'<a href="https://x.invalid/full.png"><img src="{URL}" width="40" height="40"></a>'
    assert html_to_text(html) == "(https://x.invalid/full.png)"


@pytest.mark.parametrize("quote", [False, True])
@pytest.mark.parametrize("prose", ["```", "``` example"])
def test_prose_backticks_do_not_change_pre_whitespace(quote, prose):
    html = f'<p>{prose}</p><pre>    first\n\n        second\n</pre><p>after   text</p>'
    expected = f'\\{prose}\n```\n    first\n\n        second\n```\nafter text'
    if quote:
        html = f"<blockquote>{html}</blockquote>"
        expected = "\n".join("> " + line for line in expected.split("\n"))
    text = html_to_text(html)
    assert text == expected
    assert squeeze(text) == expected
    assert squeeze(sanitize_text(text)) == expected
    post = Post(1, "writer", text)
    assert format_post(post) == "[writer #1]: " + expected.replace("\n", "\n    ")
    assert json.loads(dumps(post))["text"] == expected


def test_pre_backticks_are_code_not_a_closing_fence():
    text = html_to_text('<pre>    first\n```\n\n        second\n</pre><p>after   text</p>')
    expected = '````\n    first\n```\n\n        second\n````\nafter text'
    assert text == expected
    assert squeeze(sanitize_text(text)) == expected


@pytest.mark.parametrize("start,first", [("", 1), (' start="5"', 5), (' start="bad"', 1)])
def test_ordered_list_keeps_its_numbers(start, first):
    html = f'<ol{start}><li><p>Gather parts</p></li><li><p>Assemble</p></li><li>See step {first}</li></ol>'
    expected = f'{first}. Gather parts\n{first + 1}. Assemble\n{first + 2}. See step {first}'
    text = html_to_text(html)
    assert text == expected
    assert squeeze(sanitize_text(text)) == expected


@pytest.mark.parametrize("quote_depth", [0, 1, 2])
def test_nested_lists_keep_their_parent_numbers_and_indentation(quote_depth):
    html = ('<ol start="5"><li><p>Gather parts</p><ul><li><p>Bolts</p></li>'
            '<li>Tools<ol start="2"><li>Wrench</li></ol></li></ul></li><li>Assemble</li></ol>')
    expected = '5. Gather parts\n  - Bolts\n  - Tools\n    2. Wrench\n6. Assemble'
    for _ in range(quote_depth):
        html = f'<blockquote>{html}</blockquote>'
        expected = "\n".join("> " + line for line in expected.split("\n"))
    text = html_to_text(html)
    assert text == expected
    assert squeeze(sanitize_text(text)) == expected


def test_squeeze_plain_markdown_keeps_its_existing_fence_behavior():
    assert squeeze('before   text\n```python\n    first\n\n    second\n```\nafter   text') == (
        'before text\n```python\n    first\n\n    second\n```\nafter text')


HTML_WITH_CODE_AND_LIST = (
    '<p>Use ``` for code</p><p>```</p><pre><code>def f():\n\n    return 1\n</code></pre><p>after</p>'
    '<ol start="5"><li>Parts<ol start="12"><li>Bolts</li></ol></li><li>Assemble</li></ol>'
)
TEXT_WITH_CODE_AND_LIST = (
    'Use ``` for code\n\\```\n```\ndef f():\n\n    return 1\n```\nafter\n'
    '5. Parts\n  12. Bolts\n6. Assemble'
)


@pytest.mark.parametrize("transform", [str.strip, lambda text: "Flair: x\n\n" + text,
                                      lambda text: f"{text}", lambda text: "".join([text]),
                                      lambda text: text.replace("after", "after")],
                         ids=["strip", "concatenate", "f-string", "join", "replace"])
@pytest.mark.parametrize("quote_depth", [0, 1, 2])
def test_code_and_nested_lists_survive_string_operations(transform, quote_depth):
    html, expected = HTML_WITH_CODE_AND_LIST, TEXT_WITH_CODE_AND_LIST
    for _ in range(quote_depth):
        html = f"<blockquote>{html}</blockquote>"
        expected = "\n".join("> " + line for line in expected.split("\n"))
    text = transform(html_to_text(html))
    if text.startswith("Flair: x\n\n"):
        expected = "Flair: x\n" + expected
    assert squeeze(sanitize_text(text)) == expected
    assert format_post(Post(1, "writer", text)) == "[writer #1]: " + expected.replace("\n", "\n    ")


@pytest.mark.parametrize("html", [HTML_WITH_CODE_AND_LIST,
                                  '<pre>first\n```\n\n    second\n`````\nlast</pre><p>after</p>'])
def test_html_text_squeeze_is_idempotent(html):
    text = html_to_text(html)
    assert squeeze(squeeze(text)) == squeeze(text) == text
    assert squeeze(squeeze(text.strip())) == text


def test_only_an_exact_matching_backtick_run_closes_a_fence():
    text = 'before   text\n````python\n    first\n```\n\n    second\n`````\n    third\n````\nafter   text'
    expected = text.replace("before   text", "before text").replace("after   text", "after text")
    assert squeeze(text) == expected
    assert squeeze(squeeze(text)) == expected


def test_split_inline_html_at_line_start_cannot_open_a_fence():
    assert html_to_text('<p>  <code>``</code>` example</p><p>after   text</p>') == '\\``` example\nafter text'


def test_only_list_markers_keep_leading_spaces_outside_code():
    text = '   prose   line\n  - nested   item\n    12. numbered   item\n>   - quoted   item'
    expected = 'prose line\n  - nested item\n    12. numbered item\n>   - quoted item'
    assert squeeze(text) == expected
    assert squeeze(squeeze(text)) == expected


@pytest.mark.parametrize("text", ['```\nfirst\n\n', '```\n\n'])
def test_an_unclosed_fence_keeps_trailing_code_newlines_on_every_pass(text):
    assert squeeze(text) == text
    assert squeeze(squeeze(text)) == text


def test_pre_fence_is_longer_than_every_run_when_a_code_line_starts_with_backticks():
    code = '```\nvalue = "`````"\n\n    last'
    expected = f'``````\n{code}\n``````'
    text = html_to_text(f'<pre>{code}</pre>')
    assert text == expected
    assert squeeze(squeeze(text.strip())) == expected


def test_quote_keeps_code_lines_images_and_struck_text():
    from esum.engines.vbulletin.parse import message_text

    html = ('<div><div><label>Quote:</label></div><div class="panel">'
            'Originally Posted by <strong>Alice</strong>'
            '<div style="font-style:italic"><pre>first\n    second</pre>'
            '<img src="https://example.org/a.png"><s>old</s></div></div></div>')
    text, _ = message_text(html)
    assert text == ('> @Alice:\n> ```\n> first\n>     second\n> ```\n'
                    '> [image: https://example.org/a.png]~~old~~')


@pytest.mark.parametrize('tag', ['div', 'td', 'blockquote'])
def test_balanced_elements_share_end_inner_and_removal(tag):
    from esum import element_end, element_inner, remove_elements

    opening = rf'<{tag} class="target">'
    block = f'<{tag} class="target">outer<{tag}>inner</{tag}>tail</{tag}>'
    html = f'before{block}between{block}after'
    assert element_end(html, len('before'), tag) == len('before' + block)
    assert element_inner(html, opening, tag) == f'outer<{tag}>inner</{tag}>tail'
    assert remove_elements(html, opening, tag, '[removed]') == 'before[removed]between[removed]after'
    assert remove_elements(block, opening, tag, block) == block
    assert element_inner(html, '<missing>', tag) is None
    assert element_inner(html, opening, tag, start=len('before' + block)) == f'outer<{tag}>inner</{tag}>tail'


def test_balanced_elements_at_eof_and_quoted_attributes():
    from esum import element_end, element_inner, inner_div, remove_elements

    html = '<DIV class="target"><div title="x > y"/><div>nested</DIV>tail</div>after'
    assert element_end(html, 0) == html.index('after')
    assert element_inner(html, r'<DIV class="target">') == '<div title="x > y"/><div>nested</DIV>tail'
    unclosed = '<div class="target">one<div>two</div>end'
    assert element_end(unclosed, 0) == len(unclosed)
    assert element_inner(unclosed, r'<div class="target">') == 'one<div>two</div>end'
    assert inner_div(unclosed, 0) == 'one<div>two</div>end'
    assert remove_elements(unclosed, r'<div class="target">') == ''


def test_quote_html_default_cap_and_source_hint():
    from esum import quote_html

    assert html_to_text(quote_html('@ann', '#3', '<p>' + 'x' * 200 + '</p>')) == '> @ann #3: ' + 'x' * 200
    assert html_to_text(quote_html('@ann', None, 'x' * 201)) == '> @ann: ' + 'x' * 200 + '…'
    assert html_to_text(quote_html('@ann', None, 'x' * 201, hint='; whole in #3')) == (
        '> @ann: ' + 'x' * 200 + '… [cut at 200 chars; whole in #3]')
    assert html_to_text(quote_html('', None, 'x' * 201, cap=None)) == '> ' + 'x' * 201


def test_quote_html_keeps_structure_and_escapes_plain_labels():
    from esum import quote_html

    body = '<p>first</p><pre>  a\n\n    b</pre><p><s>old</s><img src="https://x.invalid/a"></p>'
    text = html_to_text(quote_html('@<ann>', 'https://x.invalid/?a=1&b=2', body))
    assert text == ('> @<ann> https://x.invalid/?a=1&b=2: first\n> ```\n>   a\n> \n>     b\n'
                    '> ```\n> ~~old~~[image: https://x.invalid/a]')
    assert html_to_text(quote_html('', None, '<a href="https://x.invalid/">link</a>')) == '> link'
    assert html_to_text(quote_html('', None, '<a href="https://x.invalid/">link</a>', keep_links=True)) == '> link (https://x.invalid/)'


def test_quote_cap_closes_code_and_does_not_change_following_prose():
    from esum import quote_html

    text = html_to_text(quote_html('@ann', None, '<pre>' + 'x' * 300 + '</pre>') + '<p>after   text</p>')
    assert text == '> @ann:\n> ```\n> ' + 'x' * 196 + '…\n> ```\nafter text'


@pytest.mark.parametrize('engine', ['discuz', 'discourse'])
def test_forum_quotes_keep_code_images_and_struck_text(engine):
    body = '<pre>a\n  b</pre><img src="https://x.invalid/a"><s>old</s>'
    if engine == 'discuz':
        from esum.engines.discuz.parse import message_text
        text, _ = message_text('<div class="quote"><blockquote>' + body + '</blockquote></div>')
    else:
        from esum.engines.discourse.parse import post_text
        text = post_text({'cooked': '<aside class="quote"><blockquote>' + body + '</blockquote></aside>', 'post_type': 1})
    assert text == '> ```\n> a\n>   b\n> ```\n> [image: https://x.invalid/a]~~old~~'



def test_compact_quote_inside_an_ordinary_blockquote_has_one_prefix_per_level():
    from esum import quote_html

    html = '<blockquote><p>outer</p>' + quote_html('@ann', '#3', '<p>inner</p><p>next</p>') + '</blockquote>'
    assert html_to_text(html) == '> outer\n> > @ann #3: inner\n> > next'



def test_shared_html_field_and_clipping_helpers():
    from esum import clip, text_of

    assert text_of('<b>A&amp;B</b>\n C') == 'A&B C'
    assert text_of(None) == ''
    assert clip('abcd', None) == 'abcd'
    assert clip('abcd', 4) == 'abcd'
    assert clip('abc def', 4) == 'abc…'
    assert clip('abc def', 4, '; whole in #2') == 'abc… [cut at 4 chars; whole in #2]'


def test_quote_cap_is_a_body_budget_with_ellipsis_added_after_it():
    from esum import quote_html

    assert html_to_text(quote_html('', None, 'x' * 79, cap=79)) == '> ' + 'x' * 79
    assert html_to_text(quote_html('', None, 'x' * 80, cap=79)) == '> ' + 'x' * 79 + '…'


def test_unclosed_quote_body_does_not_discard_its_text():
    from esum import quote_html

    assert html_to_text(quote_html('', None, '<blockquote>inner')) == '> > inner'



def test_quote_keeps_a_longer_fence_when_code_contains_shorter_backticks():
    from esum import quote_html

    assert html_to_text(quote_html('', None, '<pre>```\n    code</pre>')) == (
        '> ````\n> ```\n>     code\n> ````')



def test_discourse_omits_an_inner_quote_with_nested_blockquotes():
    from esum.engines.discourse.parse import post_text

    inner = '<aside class="quote"><blockquote><blockquote>inner</blockquote></blockquote></aside>'
    html = '<aside class="quote"><blockquote>' + inner + '<p>outer</p></blockquote></aside>'
    assert post_text({'cooked': html, 'post_type': 1}) == '> outer'


@pytest.mark.parametrize('padding', [196, 197])
def test_quote_cut_on_fence_cannot_capture_following_prose(padding):
    from esum import quote_html

    text = html_to_text(quote_html('@a', None, 'a' * padding + '<pre>code</pre>')
                        + '<p>after    spaced</p><p>next</p>')
    assert text.endswith('\nafter spaced\nnext')
    assert text.count('```') % 2 == 0


@pytest.mark.parametrize('quote', ["'", '"'])
def test_balanced_elements_do_not_swallow_lone_attribute_quotes(quote):
    from esum import element_inner

    html = '<div class="q"><div class=a' + quote + 'b>x</div></div><p>it' + quote + 's</p><div>after</div>'
    assert element_inner(html, '<div class="q">') == '<div class=a' + quote + 'b>x</div>'


@pytest.mark.parametrize('engine', ['discourse', 'discuz', 'vbulletin'])
def test_empty_quote_does_not_fall_back_to_its_title(engine):
    if engine == 'discourse':
        from esum.engines.discourse.parse import post_text
        html = ('<aside class="quote" data-username="alice" data-post="3">'
                '<div class="title"><img src="https://x/av.png">alice:</div><blockquote></blockquote></aside>')
        text = post_text({'cooked': html, 'post_type': 1})
        assert text == '> @alice #3'
    elif engine == 'discuz':
        from esum.engines.discuz.parse import message_text
        text, _ = message_text('<div class="quote"><span>title</span><blockquote></blockquote></div>')
        assert text == ''
    else:
        from esum.engines.vbulletin.parse import _quote_box
        html, _ = _quote_box('<span>title</span><div class="panel"></div>', {})
        assert html_to_text(html) == ''


@pytest.mark.parametrize('label', ['```js', '> ```js', '\n```js'])
def test_quote_labels_cannot_open_code_fences(label):
    from esum import quote_html

    text = html_to_text(quote_html(label, '#3', 'x') + '<p>after    spaced</p>')
    assert text.endswith('\nafter spaced')
    assert '\\```js' in text


def test_quote_hint_cannot_open_code_fences():
    from esum import quote_html

    text = html_to_text(quote_html('@a', None, 'x' * 201, hint='\n```js\nafter    hint')
                        + '<p>after    spaced</p>')
    assert text.endswith('\nafter spaced')
    assert '\\```js' in text


@pytest.mark.parametrize('marker', ['', 'forged'])
def test_raw_html_cannot_forge_compact_quote_metadata(marker):
    html = ('<blockquote data-esum-quote="' + marker + '" data-cap="-2" data-head="```js">'
            'actual body</blockquote><p>after    spaced</p>')
    assert html_to_text(html) == '> actual body\nafter spaced'


@pytest.mark.parametrize('cap', [0, -1])
def test_quote_html_rejects_nonpositive_caps(cap):
    from esum import quote_html

    with pytest.raises(ValueError, match='positive'):
        quote_html('@a', None, 'body', cap=cap)


def test_quote_cap_counts_converted_body_including_nested_markers_and_fences():
    from esum import quote_html

    assert html_to_text(quote_html('', None, '<blockquote>abcdef</blockquote>', cap=5)) == '> > abc…'
    assert html_to_text(quote_html('', None, '<pre>abcdef</pre>', cap=5)) == '> ```\n> a…\n> ```'



def test_discourse_passes_a_body_budget_for_its_quote_output_limit():
    from esum.engines.discourse.parse import post_text

    def quoted(size):
        html = '<aside class="quote" data-username="a"><blockquote>' + 'x' * size + '</blockquote></aside>'
        return post_text({'cooked': html, 'post_type': 1})

    assert quoted(299) == '> @a: ' + 'x' * 299
    assert quoted(300) == '> @a: ' + 'x' * 299 + '…'
    assert quoted(301) == '> @a: ' + 'x' * 299 + '…'


def test_table_cells_are_separated():
    html = ('<div class="md-table"><table><thead><tr><th>Card</th><th>Fee</th><th>Bonus</th></tr></thead>'
            '<tbody><tr><td>Sapphire Preferred</td><td>$95</td><td>60,000</td></tr></tbody></table></div>')
    assert html_to_body(html) == "Card | Fee | Bonus\nSapphire Preferred | $95 | 60,000"


def test_quote_marker_survives_a_raw_newline():
    html = "<blockquote><p>quoted line one<br />\nquoted line two</p></blockquote><p>my reply</p>"
    assert html_to_body(html) == "> quoted line one\n> quoted line two\nmy reply"
    discourse_quote = ('<aside class="quote"><div class="title">bob:</div><blockquote>\n<p>first<br>\nsecond</p>\n'
                       '</blockquote></aside>\n<p>reply</p>')
    assert html_to_body(discourse_quote) == "bob:\n> first\n> second\nreply"


def test_newlines_in_text_are_whitespace_outside_pre():
    assert html_to_body("<p>one\ntwo</p>") == "one two"
    assert html_to_body("<blockquote><pre>a = 1\nb = 2</pre></blockquote><p>ok</p>") == "> ```\n> a = 1\n> b = 2\n> ```\nok"


def test_self_closing_skip_tag_keeps_the_rest():
    assert html_to_body('<p>Before</p><svg class="icon"/><p>After</p>') == "Before\nAfter"
    assert html_to_body("<p>Before</p><script/><p>After</p>") == "Before\nAfter"


def test_code_keeps_its_indentation_blank_lines_and_a_fence():
    from esum.core.posts import compact_text

    html = "<p>脚本：</p><pre><code>def f(x):\n    if x  &lt; 1:\n        return 0\n\n    return x\n</code></pre><p>完</p>"
    text = html_to_body(html)
    assert text.splitlines() == ["脚本：", "```", "def f(x):", "    if x  < 1:", "        return 0", "", "    return x",
                                 "```", "完"]
    assert compact_text(text) == text   # the text output keeps it too


def test_quoted_code_keeps_its_marker_and_prose_is_still_collapsed():
    text = html_to_body("<blockquote><pre>a\n  b</pre></blockquote><p>x   y</p>")
    assert "> ```" in text and ">   b" in text and "x y" in text


def test_struck_text_stays_marked():
    assert html_to_body("<p>原价<del>50</del>现在<s>80</s></p>") == "原价~~50~~现在~~80~~"


def test_an_image_keeps_both_its_description_and_its_url():
    assert html_to_body('<img src="https://x.invalid/a.png" alt="收据">') == "[image: 收据 https://x.invalid/a.png]"
    assert html_to_body('<img src="a.png" alt="pic">') == "[image: pic]"
    assert html_to_body('<img class="wp-smiley" src="https://s.invalid/s.png" alt="😀">') == "[image: 😀]"


def test_a_linked_image_prints_its_url_once():
    html = '<a href="https://x.invalid/a.png"><img src="https://x.invalid/a.png" alt=""></a>'
    assert html_to_body(html) == "[image: https://x.invalid/a.png]"


def test_adjacent_links_to_one_page_print_as_one_link():
    # Block editors split one link into two anchors mid-word (seen on milesearnandburn.com).
    url = "https://www.ana.invalid/fuelsurcharge/"
    html = f'<a href="{url}">Fuel surcharges a</a><a href="{url}">re bad on ANA now</a>, but not that bad.'
    assert html_to_body(html) == f"Fuel surcharges are bad on ANA now ({url}), but not that bad."
    two = f'<a href="{url}">one</a> <a href="https://other.invalid/">two</a>'
    assert html_to_body(two) == f"one ({url}) two (https://other.invalid/)"
    apart = f'<a href="{url}">one</a> and <a href="{url}">two</a>'
    assert html_to_body(apart) == f"one ({url}) and two ({url})"
    spaced = f'<a href="{url}">Fuel </a><a href="{url}">surcharges</a> rise.'
    assert html_to_body(spaced) == f"Fuel surcharges ({url}) rise."


def test_a_linked_image_before_a_text_link_to_the_same_page_keeps_its_url():
    url = "https://www.ana.invalid/fuelsurcharge/"
    html = f'<a href="{url}"><img src="https://x.invalid/a.png" alt=""></a><a href="{url}">Fuel surcharges</a>'
    assert html_to_body(html).startswith(f"[image: https://x.invalid/a.png] ({url})")


def test_a_space_at_the_end_of_a_link_stays_after_its_url():
    url = "https://shop.invalid/giftcards"
    html = f'cash them out for a <a href="{url}">gift card </a>to resell.'
    assert html_to_body(html) == f"cash them out for a gift card ({url}) to resell."


def test_code_keeps_trailing_spaces():
    assert "--no-mmproj-offload " in html_to_body("<pre>--no-mmproj-offload \nnext</pre>").splitlines()


def test_a_smiley_is_its_name_not_a_long_url():
    html = '<img src="https://static.invalid/image/smiley/default/lol.gif" smilieid="4" alt="">'
    assert html_to_body(html) == ":lol:"
    assert html_to_body('<img src="https://www.invalid/images/smilies/smile.gif" alt="">') == ":smile:"


# --- V2EX and NodeSeek against their raw pages: nothing unknown shown as zero, nothing silently missing ---

def test_terminal_colors_go_whole():
    assert sanitize_text("\x1b[36;1mGeekbench\x1b[0m 990\x08") == "Geekbench 990"
