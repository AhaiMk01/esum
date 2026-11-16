"""USCardForum (美卡论坛): a Discourse forum about US credit cards, bank bonuses, points and miles.

The site is behind Cloudflare, which challenges plain HTTP clients but lets Chrome's network
fingerprint through: reads go over T2 without browser cookies, so no browser starts. A call that
meets a challenge anyway switches to T3 in the ESUM browser window (D-40). The bookmark
needs the user's login and runs as a generic fetch() in the ESUM browser window (T3). The category map is static (`esum uscf categories`
prints it), so category names and `--category 信用卡` need no `/site.json` request.
"""

from esum import Transport, site

FORMAT = 1
PORTED_FROM = ("xgoose skills/uscardforum.source.xgs.js (github.com/xGoose-dev/xgoose); "
               "nitan-mcp src/tools/categories.ts (category map); "
               "nitan_plugin My.user.js (USCF label)")


def _cat(id: int, name: str, slug: str = "", parent_id: int | None = None, restricted: bool = False) -> dict:
    return {"id": id, "name": name, "slug": slug, "parent_id": parent_id, "restricted": restricted}


CATEGORIES = [
    _cat(12, "玩卡", "rewards"),
    _cat(5, "信用卡", "credit-cards", 12),
    _cat(6, "银行账户", "bank-accounts", 12),
    _cat(32, "信用分数", "credit-score", 12),
    _cat(56, "Refer专区", "special", 12),
    _cat(15, "旅行", "travel"),
    _cat(38, "航空常旅客", "airline-programs", 15),
    _cat(7, "酒店常旅客", "hotel-programs", 15),
    _cat(17, "游记攻略", "trip-report", 15),
    _cat(50, "租车", "car-rental", 15),
    _cat(58, "驴友", "travel-friends", 15),
    _cat(9, "理财", "investment"),
    _cat(13, "股市投资", "stock-market", 9),
    _cat(14, "房地产", "real-estate", 9),
    _cat(10, "税务", "tax", 9),
    _cat(43, "加密货币", "coins", 9),
    _cat(20, "败家", "shopping"),
    _cat(26, "好物推荐", "good-stuff", 20),
    _cat(21, "购物折扣", "deals", 20),
    _cat(23, "电子产品", "tech", 20),
    _cat(25, "汽车", "", 20),
    _cat(44, "手机卡", "wireless-services", 20),
    _cat(51, "生活", "life"),
    _cat(22, "吃货", "foodie", 51),
    _cat(47, "影音娱乐", "movies", 51),
    _cat(49, "游戏", "games", 51),
    _cat(55, "健康", "health", 51),
    _cat(52, "园艺种菜", "", 51),
    _cat(37, "宠物", "pets", 51),
    _cat(53, "体育", "", 51),
    _cat(60, "育儿", "children", 51),
    _cat(62, "社会新闻", "news-in-the-us", 51),
    _cat(45, "回国or留美", "china-us-comparison", 51),
    _cat(18, "法律", "laws"),
    _cat(19, "签证与身份（美国）", "visa", 18),
    _cat(61, "签证与身份（美国以外）", "visa-other-countries-and-regions", 18),
    _cat(27, "新政", "orders", 18),
    _cat(28, "情感", "feelings"),
    _cat(29, "爱情", "love", 28),
    _cat(31, "鹊桥", "piebridge", 28),
    _cat(33, "搬砖", "jobs"),
    _cat(34, "面经", "interviews", 33),
    _cat(36, "内推", "job-refer", 33),
    _cat(48, "学术", "academics", 33),
    _cat(54, "求学", "study", 33),
    _cat(57, "文艺", "literature-and-art"),
    _cat(1, "闲聊"),
    _cat(42, "吵架", "politics"),
    _cat(3, "公告", "announcements"),
    _cat(68, "白金", restricted=True),
    _cat(67, "钛金", restricted=True),
    _cat(40, "疫情（已关闭）", "covid-19"),
]

uscf = site(
    name="uscf",
    engine="discourse",
    base_url="https://www.uscardforum.com",
    transport=Transport.BROWSER_HTTP,  # T2, Chrome fingerprint only
    fallback=Transport.IN_PAGE,        # T3 when Cloudflare challenges T2
    label={
        "en": "USCardForum (uscardforum.com): US credit cards, sign-up bonuses, points/miles, bank "
              "policies; threads hold readers' data points (DPs)",
        "zh": "美卡论坛：美国信用卡、开卡奖励、积分里程、银行政策，帖子里有大量 DP",
    },
    extras={"categories": CATEGORIES, "browser_cookies": False, "write_transport": "in_page"},
)
