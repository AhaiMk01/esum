# Discourse fixtures

Captured from meta.discourse.org (public, T1) on 2026-10-26 and trimmed with a scrub script:
usernames and user ids are replaced by consistent pseudonyms (`user1`, …), avatars, names, flair,
drafts and search log ids are dropped, quote headers read `someone:`, and long post bodies are cut.

- `topic.json`: `/t/395331.json` (100 visible posts, highest post number 117: deletions leave gaps)
- `topic_posts.json`: `/t/395331/posts.json?post_ids[]=…` for every post not inline in `topic.json`
- `posts_after_50.json` / `posts_before_50.json`: `/t/395331/posts.json?post_number=50&asc=true|false`
- `search.json`, `latest.json` (`?order=created`), `category_latest.json` (`/c/67.json`), `hot.json`,
  `top.json` (`?period=yearly`), `site.json` (categories only)
- `latest_posts.json` / `latest_posts_before.json`: `/posts.json` and `/posts.json?before=<4th id>`
  (captured 2026-10-30; quoted authors read `someone`)
- `user.json`, `user_summary.json`, `user_actions.json` (`?filter=4,5`): one long-standing account,
  as `user1`, trimmed to the fields the engine reads

Hand-written (uscardforum.com answers plain HTTP with a Cloudflare challenge, so nothing is captured
from it): `cf_challenge.html` (the challenge page, ray id scrubbed), `not_logged_in.json` (Discourse's
error shape), `csrf.json` (token scrubbed). Megatopics are simulated in the tests by removing
`post_stream.stream` from `topic.json`, which is what Discourse does for very long topics.
