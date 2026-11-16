"""FlyerTalk forums: the largest frequent-flyer community (vBulletin 3.9 under /forum/).

Cloudflare answers plain HTTP clients with a challenge, so every request runs as a generic fetch()
in a browser tab (T3). Pages are served as ISO-8859-1. The forum catalog below is static (`esum
flyertalk forums` prints it), so `--forum united-airlines-mileageplus` needs no lookup request.
"""

from esum import Listing, Next, Tier, Transport, action, site

FORMAT = 1
PORTED_FROM = ("xgoose skills/flyertalk.source.xgs.js (github.com/xGoose-dev/xgoose) "
               "(forum catalog, URL forms); nitan_plugin My.user.js (FlyerTalk adapter: in-page fetch, "
               "ISO-8859-1, 15 posts per page)")

# (id, slug, name): top-level sections, then airline, hotel and credit-card program forums
FORUMS = [
    (1, "miles-points", "Miles & Points"),
    (161, "travel-dining", "Travel & Dining"),
    (176, "travelbuzz", "TravelBuzz"),
    (177, "trip-reports", "Trip Reports"),
    (178, "travel-news", "Travel News"),
    (366, "omni", "OMNI"),
    (367, "community", "Community"),
    (370, "milesbuzz", "MilesBuzz"),
    (372, "mileage-run-deals", "Mileage Run Deals"),
    (374, "airlines-mileage-programs", "Airlines & Mileage Programs"),
    (389, "hotels-loyalty-programs", "Hotels & Loyalty Programs"),
    (438, "americas", "Americas"),
    (599, "credit-card-programs", "Credit Card Programs"),
    (702, "europe", "Europe"),
    (704, "destinations", "Destinations"),
    (705, "asia", "Asia"),
    (375, "air-canada-aeroplan", "Air Canada | Aeroplan"),
    (440, "air-new-zealand-koru", "Air New Zealand | Koru"),
    (442, "alaska-airlines-atmos-rewards", "Alaska Airlines | Atmos Rewards"),
    (443, "all-nippon-airways-ana-mileage-club", "All Nippon Airways | ANA Mileage Club"),
    (446, "british-airways-british-airways-club", "British Airways | The British Airways Club"),
    (487, "cathay-pacific-cathay", "Cathay Pacific | Cathay"),
    (490, "emirates-skywards", "Emirates | Skywards"),
    (492, "jetblue-trueblue", "JetBlue | TrueBlue"),
    (495, "lufthansa-austrian-swiss-brussels-lot-ita-other-partners-miles-more", "Lufthansa Miles & More"),
    (498, "qantas-frequent-flyer", "Qantas | Frequent Flyer"),
    (500, "singapore-airlines-krisflyer", "Singapore Airlines | KrisFlyer"),
    (501, "southwest-airlines-rapid-rewards", "Southwest Airlines | Rapid Rewards"),
    (506, "virgin-atlantic-airways-flying-club", "Virgin Atlantic Airways | Flying Club"),
    (594, "air-france-klm-other-partners-flying-blue", "Air France/KLM Flying Blue"),
    (641, "turkish-airlines-miles-smiles", "Turkish Airlines | Miles&Smiles"),
    (644, "qatar-airways-privilege-club", "Qatar Airways | Privilege Club"),
    (665, "delta-air-lines-skymiles", "Delta Air Lines | SkyMiles"),
    (668, "frontier-airlines-frontier-miles-program", "Frontier Airlines | Frontier Miles"),
    (681, "united-airlines-mileageplus", "United Airlines | MileagePlus"),
    (692, "etihad-airways-etihad-guest", "Etihad Airways | Etihad Guest"),
    (716, "avianca-lifemiles-amigo", "Avianca | LifeMiles"),
    (718, "aer-lingus-aerclub", "Aer Lingus | AerClub"),
    (733, "american-airlines-aadvantage", "American Airlines | AAdvantage"),
    (774, "spirit-airlines-free-spirit", "Spirit Airlines | Free Spirit"),
    (416, "best-western-best-western-rewards", "Best Western | Best Western Rewards"),
    (417, "hilton-hilton-honors", "Hilton | Hilton Honors"),
    (418, "hyatt-world-hyatt", "Hyatt | World of Hyatt"),
    (426, "intercontinental-hotels-ihg-one-rewards-intercontinental-ambassador",
     "InterContinental Hotels | IHG One Rewards"),
    (428, "radisson-international-radisson-rewards", "Radisson International | Radisson Rewards"),
    (430, "wyndham-wyndham-rewards", "Wyndham | Wyndham Rewards"),
    (640, "choice-choice-privileges", "Choice | Choice Privileges"),
    (667, "accor-all-accor-live-limitless", "Accor | ALL (Accor Live Limitless)"),
    (766, "marriott-marriott-bonvoy", "Marriott | Marriott Bonvoy"),
    (410, "american-express-membership-rewards", "American Express | Membership Rewards"),
    (722, "chase-ultimate-rewards", "Chase | Ultimate Rewards"),
    (739, "citi-thankyou-rewards", "Citi | ThankYou Rewards"),
    (778, "capital-one-discover-diners-club-rewards-miles", "Capital One, Discover, and Diners Club"),
    (719, "manufactured-spending", "Manufactured Spending"),
]

flyertalk = site(
    name="flyertalk",
    engine="vbulletin",
    base_url="https://www.flyertalk.com/forum",
    transport=Transport.IN_PAGE,   # T3: Cloudflare challenges plain HTTP clients
    label={
        "en": "FlyerTalk forums: the largest frequent-flyer community; airline, hotel and credit-card "
              "program forums full of data points, trip reports and mileage-run deals",
        "zh": "FlyerTalk 论坛：全球最大的常旅客社区，航空、酒店与信用卡项目分论坛，大量数据点 DP、游记和里程跑 deal",
    },
    charset="iso-8859-1",
    # Dates show in US Central time with daylight saving as of each date (an October post in CDT,
    # UTC-5, under a November footer of GMT -6), as the RSS feed's UTC times confirm.
    extras={"forums": FORUMS, "browser_home": "/forum/", "time_zone": "America/Chicago"},
)


@action(flyertalk, tier=Tier.READ)
def forums(ctx, query: str | None = None):
    """The built-in forum catalog: id, slug and name of the main sections and of the airline, hotel
    and credit-card program forums (for `--forum` and `forum --id`).

    Any other forum works by id or URL too; the catalog only saves looking ids up.

    Args:
        query: Only forums whose slug or name contains this.
    """
    rows = [{"id": i, "slug": s, "name": n} for i, s, n in FORUMS]
    if query:
        q = query.lower()
        rows = [r for r in rows if q in r["slug"] or q in r["name"].lower()]
    return Listing(name="forums", rows=rows, fields=("id", "slug", "name"), about={"query": query},
                   empty=f"no catalog forum matches {query!r}; any forum works by id or URL",
                   next=[Next("forum", {"id": "<id>"}, "list a forum's threads"),
                         Next("search", {"query": "<words>", "forum": "<slug>"}, "search one forum")])
