# 一亩三分地 fixtures (api.1point3acres.com, trpc.1point3acres.com)

Saved over plain HTTPS as a **logged-out guest** on 2026-11-07 (no cookies were sent, so no
answer carries account data). Trimmed for size; field names and values are as the API sent them.
Poster names are public (most are the site's per-thread anonymous names, `地里匿名用户XXXX`).
`forums.json`'s content hash (`hashed`) is replaced by `SCRUBBED`; `widget` (a company card) and
`author_verification` were dropped from the thread answers.

| File | Request |
|---|---|
| `forums.json` | `/api/forums`, trimmed to groups 海外求职 (38) and 留学申请 (257), boards 28 (with thread types), 145, 27 and sub-boards 335, 79 |
| `hot.json` | `/api/threads?type=hot&pg=1&ps=30&includes=forum_name`, first 3 rows |
| `forum145.json` | `/api/forums/145/threads?pg=1&ps=30` (海外面经), first 3 rows |
| `thread.json` | `/api/v3/home-threads/1196058`: a 跳槽 thread, 48 replies, tags Amazon / Bytedance |
| `posts_p1.json` | `/api/threads/1196058/nested-posts?pg=1&ps=20&order=time_asc&sub_limit=50&with_total=1`: total 26, first 4 floors (the first with 8 nested replies) |
| `posts_p2.json` | the same, `pg=2`: first 2 floors |
| `locked.json` | `/api/v3/home-threads/1196170`: an opening post locked by `[hide=200]` (points) |
| `attach.json` | `/api/v3/home-threads/1196209`: an opening post with an outbound `link.1p3a.com` link and an `[attach]` image |
| `missing.json` | `/api/v3/home-threads/99999999` (HTTP 404) |
| `login.json` | `/api/users/449044/threads` as a guest (HTTP 401, `请先登录`) |
| `search.json` | tRPC `search.search` for 租车 (keywords), first 3 hits, cursor 20 |
