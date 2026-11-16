"""吾爱破解 52pojie.cn: Chinese reverse-engineering and software-security forum (Discuz! X 3.4).

Pages are GBK. Guests can read the board index, thread lists and the first page of every thread;
later thread pages and some boards are for members. Requests go through curl_cffi with the ESUM
browser's cookies (T2), so once the user has logged in there, every page reads as a member. The site
sends search requests to Bing, so this site's `search` overrides the engine's and only says so.
"""

from esum import SiteRefused, Tier, Transport, action, site

FORMAT = 1
PORTED_FROM = "original work (no external source)"

wuaipojie = site(
    name="wuaipojie",
    engine="discuz",
    base_url="https://www.52pojie.cn",
    transport=Transport.BROWSER_HTTP,   # T2 with the browser's login: thread pages 2+ are for members
    label={
        "en": "吾爱破解 (52pojie.cn): Chinese reverse-engineering and software-security forum; cracking "
              "and unpacking write-ups, Android security, malware analysis, programming and tools",
        "zh": "吾爱破解：逆向工程与软件安全论坛，脱壳破解、移动安全、病毒分析、编程语言与精品软件",
    },
    charset="gbk",
    extras={"min_interval": 1.0},
)


@action(wuaipojie, tier=Tier.READ)
def search(ctx, query: str):
    """Not available on 52pojie: the site sends searches to Bing. Search the web with
    `site:52pojie.cn`, or browse with `forum` and `latest`.

    Args:
        query: Words to look for (unused; the call stops without a request).
    """
    error = SiteRefused("wuaipojie has no search: 52pojie sends searches to Bing; search the web with "
                        "`site:52pojie.cn` instead")
    error.help = ("Find threads with `esum wuaipojie forum --id <id> --order dateline|replies|views|heats` "
                  "or `esum wuaipojie latest --view new|replied|hot|digest`")
    raise error
