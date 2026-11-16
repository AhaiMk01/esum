from typing import Literal

from esum import Tier, Transport, action, site
from esum.engines.wordpress import list_posts

FORMAT = 1
PORTED_FROM = ("xgoose skills/uscreditcardguide.source.xgs.js (github.com/xGoose-dev/xgoose); "
               "nitan_plugin My.user.js (WordPress adapter: 美国信用卡指南)")

Lang = Literal["zh", "en", "all"]

uscreditcardguide = site(
    name="uscreditcardguide",
    engine="wordpress",
    base_url="https://www.uscreditcardguide.com",
    transport=Transport.DIRECT,   # T1: public WP REST
    label={
        "en": "美国信用卡指南 (US Credit Card Guide): Chinese-language blog on US credit cards, bank bonuses "
              "and points; the comments under each post hold readers' data points and questions",
        "zh": "美国信用卡指南：中文的美国信用卡、银行奖励与积分攻略博客；文章下的评论区是读者的数据点 DP 和提问",
    },
    # Readers comment through Disqus; the WP comment table holds old imports and spam.
    # physixfan is the blog's author on Disqus.
    extras={"op_user": "USCCG", "op_aliases": ["physixfan"], "comments": "disqus"},
)


def _lang(lang: str) -> dict:
    # Polylang: posts exist in a Chinese and an English edition; without `lang` both are mixed
    return {} if lang == "all" else {"lang": lang}


@action(uscreditcardguide, tier=Tier.READ, name="search")
def search(ctx, query: str, since: str | None = None, category: str | None = None, limit: int = 20,
           page: int = 1, lang: Lang = "zh"):
    """Search posts by keyword, most relevant first.

    Each row has the post id (for `thread`), date, title and a short excerpt (`--fields url` adds
    the URL). Comment counts are not available from the search API. English keywords also match
    Chinese posts.

    Args:
        query: Search words (Chinese or English).
        since: Only posts dated on or after this (UTC): YYYY-MM-DD, an ISO time, or relative like 2w / 7d
            / 12h (counted from now to the minute).
        category: Only this category (id or slug; see `categories`).
        limit: Results per page (max 100).
        page: Result page.
        lang: zh (the Chinese edition, default), en (the English edition) or all.
    """
    return list_posts(ctx, query=query, since=since, category=category, limit=limit, page=page,
                      extra=_lang(lang), keep={"lang": lang})


@action(uscreditcardguide, tier=Tier.READ, name="recent")
def recent(ctx, since: str | None = None, category: str | None = None, limit: int = 20, page: int = 1,
           lang: Lang = "zh"):
    """Recent posts, newest first, optionally since a date or in one category.

    Evergreen card reviews are re-dated when they are updated, so they reappear here; `thread`
    says `Republished:` for them.

    Args:
        since: Only posts dated on or after this (UTC): YYYY-MM-DD, an ISO time, or relative like 2w / 7d
            / 12h (counted from now to the minute).
        category: Only this category (id or slug; see `categories`).
        limit: Results per page (max 100).
        page: Result page.
        lang: zh (the Chinese edition, default), en (the English edition) or all.
    """
    return list_posts(ctx, query=None, since=since, category=category, limit=limit, page=page,
                      extra=_lang(lang), keep={"lang": lang})
