# Discuz! engine fixtures

The pages the engine's own tests read (`tests/test_parsers_discuz.py`). A site config's tests may read
them too (wuaipojie's do); a page only one site's tests read stays in that site's `tests/fixtures/`.

## 吾爱破解 (52pojie.cn)

Saved from www.52pojie.cn over plain HTTP as a **logged-out guest** (2026-10-27), decoded from GBK
and stored as UTF-8 (the tests encode them back to GBK). Trimmed for size: scripts, styles, the
site header and footer, menus, user cards and most posts / rows / boards were removed; the Discuz!
markup the parser reads is unchanged. `formhash` values and attachment download tokens are replaced
by `SCRUBBED`; a guest page carries no other token, cookie or account data. User names and uids are
public forum posters. Each page keeps a stand-in for the header's guest login form (`id="lsform"`).

| File | Page |
|---|---|
| `index.html` | `forum.php`: group 软件安全 (boards 2, 5 with three sub-boards, 24 with one) and group 活动策划专区 (grid layout, boards 62 and 42) |
| `forum.html` | `forum.php?mod=forumdisplay&fid=24` page 1 of 428: announcement row, 2 stickies (one closed), 3 threads (one with a read permission) |
| `guide_hot.html` | `forum.php?mod=guide&view=hot` page 1 of 2: 3 threads with their boards |
| `thread_p1.html` | `forum.php?mod=viewthread&tid=2134210` page 1 of 2 (17 replies): floors #1 (OP, rated, attachments, code block cut to 6 lines), a 推荐 copy of a reply, #2, #3 (quotes #2), #7 (quotes #3), #10 |
| `thread_login.html` | page 2 of that thread for a guest: the "您需要先登录才能继续本操作" notice |
| `notice_noboard.html` | `forumdisplay&fid=99999`: the "指定的版块不存在" notice |

## Stage1st

52pojie sends searches to Bing, so the search pages come from another Discuz! forum.

| File | Page |
|---|---|
| `search_s1.html` | from **Stage1st** (stage1st.com/2b, Discuz! X3.5, UTF-8; 52pojie sends searches to Bing): `search.php?mod=forum&srchtxt=switch` as a guest (2026-11-07), page 1 of 2, 100 hits; trimmed to the result header, 3 rows (one with a highlighted title) and the pager. Two superscript digits in an excerpt became plain `1`/`2` so the page encodes as GBK in the tests |
| `search_s1_empty.html` | from **Stage1st** as a guest (2026-11-08): `search.php?mod=forum&srchtxt=qzxjvkwpmzq`, no hits; trimmed to the result header (相关内容 0 个) and the "对不起，没有找到匹配结果。" notice (`p.emp`) |
