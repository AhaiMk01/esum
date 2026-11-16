from esum import Transport, site

FORMAT = 1

meb = site(
    name="meb",
    engine="wordpress",
    base_url="https://milesearnandburn.com",
    transport=Transport.DIRECT,   # T1: public WP REST
    label={
        "en": "Miles Earn and Burn (MEAB, milesearnandburn.com): manufactured spending, gift cards, credit "
              "cards, points and miles; near-daily roundup posts (Quick Hits) and MS hints",
        "zh": "Miles Earn and Burn：制造消费（MS）、礼品卡、信用卡与积分里程博客；几乎每天一篇综合快讯（Quick Hits）",
    },
    # One author (Matt). Comments are plain WP comments, closed on every post.
    extras={"op_user": "MEAB", "comments": "wp"},
)
