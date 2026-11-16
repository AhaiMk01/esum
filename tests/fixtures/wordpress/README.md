# WordPress engine fixtures

The scrubbed responses the engine's own tests read (`tests/test_parsers_wordpress.py`). The sites'
tests read them from here too; a response only one site's tests read stays in that site's
`tests/fixtures/`.

| File | Response |
|---|---|
| `doc_search.json` | Doctor of Credit: `wp-json/wp/v2/posts?search=…`, the post list |
| `doc_post.json` | Doctor of Credit: one post, republished after its first comments |
| `doc_comments.json` | Doctor of Credit: `wp-json/wp/v2/comments` for that post (wpDiscuz stores ordinary WP comments) |
| `usccg_search.json` | US Credit Card Guide: the post list of a search |
| `usccg_categories.json` | US Credit Card Guide: `wp-json/wp/v2/categories` |
| `usccg_post_page.html` | US Credit Card Guide: a post's HTML page, which carries the Disqus configuration |
| `usccg_disqus_asc.html` | US Credit Card Guide: the Disqus embed for that post, oldest first (the newest-first page is the site's own fixture) |
