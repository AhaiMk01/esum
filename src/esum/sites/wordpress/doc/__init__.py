from esum import Transport, site

FORMAT = 1
PORTED_FROM = "nitan_plugin My.user.js (WordPress adapter: Doctor of Credit, WP REST + wpDiscuz comments)"

doc = site(
    name="doc",
    engine="wordpress",
    base_url="https://www.doctorofcredit.com",
    transport=Transport.DIRECT,   # T1: public WP REST
    label={
        "en": "Doctor of Credit: US credit cards, bank account bonuses and deals; the comments under "
              "each post hold readers' data points (DPs), results and discussion",
        "zh": "Doctor of Credit：美国信用卡、银行开户奖励与各类羊毛资讯博客；文章下的评论区是读者的数据点 DP、成败经验和讨论",
    },
    # wpDiscuz stores ordinary WP comments, so WP REST serves all of them. Staff comment from
    # registered accounts, which the engine marks OP without a name list.
    extras={"op_user": "DoC", "comments": "wp"},
)
