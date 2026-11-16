# vBulletin engine fixtures (FlyerTalk)

The pages the engine's own tests read (`tests/test_parsers_vbulletin.py`); flyertalk's tests read them
from here too.

Saved from www.flyertalk.com/forum through ESUM's T3 in-page fetch as a **logged-out guest**
(2026-10-26), decoded from ISO-8859-1 and stored as UTF-8. Trimmed for size: scripts, styles, ads,
the site header and footer, the login form, menus and most posts / rows were removed; the vBulletin
markup the parser reads is unchanged. No cookies, tokens (only vBulletin's literal guest
`securitytoken=guest`), session ids or account data. Usernames are public forum posters.

| File | Page |
|---|---|
| `thread_p1.html` | `showthread.php?t=2213102` (82 posts, 15 per page): posts #1–#4, quotes, likes |
| `thread_p6.html` | the same thread, page 6: posts #76–#82 |
| `wiki_p1.html` | `showthread.php?t=2156048` page 1 with a wikipost: wikipost + posts #1–#2 |
| `thread_by_post.html` | `showthread.php?p=37986891`, redirected to page 342 of thread 1307256: 3 posts |
| `forum.html` | `forumdisplay.php?f=599` (Credit Card Programs): a sticky + 4 threads, "Today" dates |
| `search_posts.html` | post search for "altitude reserve" (989 hits): 4 hits |
| `search_threads.html` | titles-only thread search for "altitude reserve" in forum 599: 4 threads, 2 closed |
| `search_none.html` | a search with no matches ("vBulletin Message" error box) |
| `rss.xml` | `external.php?type=RSS2` (2026-11-08, site-wide feed of 15 threads): 3 items, stored as UTF-8 |
| `rss_empty.xml` | `external.php?type=RSS2&forumids=374` (a section whose threads are all in sub-forums): no items |
| `footer.html` | synthetic: the page footer ("All times are GMT -5. The time now is …"), which dates "Today" |
