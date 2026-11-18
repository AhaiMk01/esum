# Third-party notices

ESUM ports site knowledge (endpoints, parameters, quirks) from the projects below into fresh Python.
No third-party code is copied or run. Each ported module records its source in `PORTED_FROM`.

## nitan_plugin

`nitan_plugin` (`My.user.js` in the `PORTED_FROM` lines) is the ESUM author's own userscript, an
in-page summarizer for forums and blogs, and is not published. Its adapters for Discourse,
vBulletin, WordPress, Hacker News, Reddit and Substack, and its rate-limit and charset handling,
are the author's work; ESUM's engines carry that knowledge, rewritten in Python.

The userscript began from [nitan-AI-summary](https://github.com/nitan-ALousaBao/nitan-AI-summary)
by ALousaBao, a Tampermonkey script that summarizes USCardForum topics. That repository states no
license, so nothing of it is redistributed here: ESUM holds none of its code. What ESUM shares
with it is how a Discourse topic is read (the topic's post list, then its posts in batches from
`posts.json`), which is Discourse's own public interface. Thanks to ALousaBao for the idea.

## nitan-mcp

[nitan-mcp](https://github.com/nitansde/nitan-mcp), a uscardforum.com MCP server derived from
[Discourse MCP](https://github.com/discourse/discourse-mcp), used as a reference for Discourse
endpoints and USCardForum quirks (raw-page topic reading, search modifiers, category map, topic
lists). Its authentication and Cloudflare-bypass code is not ported.

```
MIT License

Copyright (c) 2025 Discourse

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## xgoose

Site plugins of [xgoose](https://github.com/xGoose-dev/xgoose), used as a reference for endpoints and
quirks of forums, offer pages, award searches and social sites.

```
MIT License

Copyright (c) 2026 xGoose contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## itamx

[itamx](https://github.com/YogevKr/itamx), a CLI and MCP server for ITA Matrix, used as a reference
for the Matrix API's request shapes (`/v1/search`, `/v1/summarize`, location lookup, cabin codes). Its
API-key scraping, cache and El Al award code are not ported.

```
MIT License

Copyright (c) 2026 YogevKr

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## fli

[fli](https://github.com/punitarani/fli), a Python library, CLI and MCP server for Google Flights, used as
a reference for the `tfs` search and booking tokens, the `ds:1` page payload and the flight-row positions. Its
CLI, MCP server, booking-results RPC and models are not ported.

```
MIT License

Copyright (c) 2025 Punit Arani

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Wings of the Points (test fixtures only)

The `wotp` site reads [Wings of the Points](https://github.com/USCreditCardGuide/Wings-of-the-Points),
a chart by Martin Gao, Runjie Guan and physixfan, licensed
[CC BY-NC-ND 4.0](https://creativecommons.org/licenses/by-nc-nd/4.0/). Two of its files,
`_data/transfers.csv` and `README.md`, are kept whole and unchanged in
`src/esum/sites/wotp/tests/fixtures/` as test input. They stay under that license, not ESUM's MIT
license; `NOTICE.md` beside them has the details. ESUM's packages don't include them.
