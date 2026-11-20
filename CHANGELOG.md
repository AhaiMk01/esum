# Changelog

## 0.2.1

- A new workflow in the `esum` skill: which of your own card-linked offers (every bank Offer.love
  reads) pay off as gift cards you resell. It matches your offers to Aligned Incentives and
  CardCenter deals, recomputes the profit from your offer, and checks face value, standing prices
  and terms.
- Fixed: in `attach` and `native` mode, downloads you started in your own Chrome tabs while ESUM
  was connected were saved to a temporary folder and deleted, and dialogs in your tabs were
  dismissed ([#1](https://github.com/AhaiMk01/esum/issues/1)). The fix is in patchright-cli
  0.15.1, which ESUM now requires in every mode: run `uv tool install -U patchright-cli`, then
  `patchright-cli kill-all`.

## 0.2.0

- A new site, `wotp` (Wings of the Points): which US card points (Chase UR, AmEx MR, Citi TYP,
  Capital One, Bilt, Marriott Bonvoy) transfer to which airline programs and at what ratio, by
  airline, currency or alliance. `programs` compares the currencies, `changes` reads the chart's
  change log, and `notes` says what the ratios assume. That makes 24 built-in sites.
- `offerlove merchants` lists Offer.love's whole merchant directory in one request (about 5,900
  merchants with their offer counts), filtered by name or by offer count; `merchant` then reads
  one merchant's offers. The directory is a day behind at times, and the output says so and
  points to the merchant's own live page.
- Fixed: Offer.love's lists (`bank`, `latest`, `expiring`, `my-offers`) said nothing matched when
  `--offset` was past the last row; they now say how many rows there are.
- Fixed: `--fields all` prints the columns in the rows' own order, whichever row first has a value.

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
- `offerlove my-offers` reads every offer on your own cards from Offer.love's My Offers page, in a
  background tab of the browser you signed in with, and says which ones are already added.
- One write, behind the user's yes and logged: a Discourse forum bookmark.
- Two skills: `esum`, which teaches an agent when and how to use the command, and
  `esum-site-author`, which lets an agent add a site of your own from its URL.
