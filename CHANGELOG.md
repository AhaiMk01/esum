# Changelog

## 0.1.0

The first public release.

- The `esum` command: one command line for 23 built-in sites in four groups (cards, points and
  deals; flights; tech and security; communities and newsletters), on four engines (Discourse,
  WordPress, vBulletin, Discuz!) and standalone modules. `esum list` shows them, and `esum docs <site>`
  shows each one's actions and quirks.
- Whole threads in one compact format, lists as TOON tables, results in pages, large results
  written to a file, and the next commands to run under every result.
- Three ways to reach a site, set per site: plain HTTP, a Chrome-fingerprint client, and a real
  Chrome tab driven by patchright-cli, opened in the background in the default mode.
- Fare tools: `matrix ladder` finds the cheapest fare that meets a condition, `gflights fares`
  lists what the airline sells on the flights you name, `gflights search --all` reads the rows
  behind "View more flights".
- One write, behind the user's yes and logged: a Discourse forum bookmark.
- Two skills: `esum`, which teaches an agent when and how to use the command, and
  `esum-site-author`, which lets an agent add a site of your own from its URL.
