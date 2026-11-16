# The fare ladder

Find the cheapest fare with a condition (refundable, not basic, free to change) on a trip. Part
of [fare-rules](fare-rules.md). A search lists each itinerary once, at its cheapest fare;
`ladder` climbs to the dearer ones: it searches, leaves out the fares that fail the condition,
and searches again. A search takes 20-60 seconds and a call runs up to 3: give it a timeout of
4 minutes, and 8 minutes with `--searches 6`.

```
esum matrix ladder --origin SFO --dest NRT --depart 2027-03-10 --return 2027-03-20 --routing "UA+" --currency USD --until not-basic
```

Search with the real passengers (`--adults 2`, `--children 1`): a rung's price is the total, a
fare line one passenger's.

## Before you climb: ask the airline's brands

`esum gflights fares --flights …` lists what the airline sells on one pair of flights, each
brand with its price and its change and refund terms in a word, in seconds
([flight-search](flight-search.md), step 3). Start there when:

- **The user names the flights.** One call answers.
- **The trip is a handful of flights**, such as one carrier's nonstops: list them with
  `esum gflights search … --stops 0` and `returns`, and run `fares --flights` on each pairing.
  Four pairings take under a minute. The same question took the ladder 45 rungs and 11 minutes
  on Delta ATL-CDG (2026-11-15), where every cheap fare is filed as basic under six letter
  codes, and its refundable fare was not reached in 30 rungs.

The brands don't give a fare basis or the filed rules, and some airlines have none on Google
(a `brands` line says so). The ladder gives those, for the whole trip, whichever flights. Use
it after the brands to name the fare: the brand's price tells you which rung to look for
(Main Classic $2,575 for two is the rung at USD 1,287.39 an adult), and when the ladder
doesn't reach a brand's fare, report the brand with its price and say its fare basis and rules
were not read. When a rung fits, a help line gives the `fares` command for the rung's flights:
what the airline sells on them now.

A routing such as `O:DL+` keeps the flights the carrier operates, under any number sold on
them: AF3611 and VS3970 beside DL90 are the same aircraft, each with its own airline's fares
to climb past. For one carrier's own fares use `DL+`: on Delta ATL-CDG it reached the first
fare that isn't basic in 8 searches against 10 (2026-11-15).

The ladder can't be made to skip ahead. A search lists each itinerary once, at its cheapest
fare left, so a trip with four pairings of flights shows one to five prices a search however
many itineraries are read (reading 20 a search instead of 6 took the same 10 searches), and a
carrier with seven layers of basic fares takes seven searches to get past them.

## The condition

`--until` is a test of each fare. A rung fits when none of its fares fails, and only failing
fares are left out, so one booking class can hold a basic fare and a standard one and the
ladder finds both.

| `--until` | A fare fails when |
|---|---|
| `not-basic` | its rule title calls it basic |
| `refundable` | its cancellation statements say non-refundable, or it states none (its terms are all notes) |
| `"CHANGES PERMITTED"`, any other words | its cancellation and change statements don't hold them |

- **Words** come from the `penalties` column, as this carrier writes them. Free to change is
  `CHANGES PERMITTED` and cancellable `CANCELLATIONS PERMITTED` on the carriers checked so
  far; the user's "no fee" is not `NO CHARGE` there. When `stopped` says no fare files your
  words, read the column and change them: words no fare files cost a whole call. Words can't
  ask for an amount: for "a small fee", read the charge on each rung.
- **No `--until`** lists the fares on the trip: every fare is left out once read, so an
  itinerary reusing one of them is passed over. Use it to see how a carrier words its
  penalties, never for "the cheapest that…".
- **Two conditions** take a ladder each, side by side. One climb can't answer both: a climb
  to refundable leaves out a standard non-refundable fare, and the cheapest not-basic rung
  may need that fare.

## Climb

- **Keep climbing.** While `stopped` says none fits, or that a rung fits and may not be the
  cheapest, run its last `help` line. The fare that fits can take 7 searches, most of a minute
  each, and many more on a carrier that files its cheap fares as basic under several codes:
  after two calls with none that fits, ask the brands (above) what the fare costs first. `--searches 6` halves the calls, and tell the user it takes a while. `--searches` is
  a ceiling: the ladder stops sooner when a rung fits.
- **The answer** is the rung `stopped` names, opened with its own `search` and `id`. The
  ladder stops when the cheapest itinerary of a search fits, since every cheaper one held a
  fare that fails. Rungs are numbered cheapest first in each call, can share an `id`, and can
  be listed below the answer when they cost more: they came from earlier searches.
- **The first call's rung 1** is the cheapest fare, so one climb answers "the cheapest, and
  the cheapest that…".
- **The trip's shape.** An open jaw or a multi-city trip climbs with `--legs`. `--stops 1`
  allows one stop and doesn't require it: check the stops of the rung you pick in `detail`.

## Read a rung

A row gives the rung's price, booking classes, fare basis, kind, notes and penalties. Where
classes, fares or penalties hold ` | `, the outbound comes first.

- **Penalties** quotes the first statements each fare files under `CANCELLATIONS` and
  `CHANGES` in category 16: the charge or the refusal (`cancel: any time: TICKET IS
  NON-REFUNDABLE. · change: any time: CHARGE GBP 150.00 FOR REISSUE/REVALIDATION.`). It is
  filed text, cut short: the notes under each statement, and 31 and 33, are in `rules`. A
  note can add a charge the statement doesn't name (a service fee for reissue by phone).
  `in the notes` marks a time (after departure) whose terms are all notes, and `as above` a
  rung that files what the row above does.
- **Kind** is read from the fares' rule titles. Blank means no basic or sell-up title. A kind
  such as `R is premium` marks a direction flown wholly in a higher cabin: not what the user
  asked for, and it never fits. A US domestic connection in first on a business trip is
  normal and isn't marked.
- **Notes** are Matrix's summary. Blank means no warning from Matrix, and `change fee` appears
  on fares whose rules file no fee: trust penalties over notes. When `stopped` adds that
  Matrix's note calls the answer non-refundable, the statements and the note disagree: read
  33 before you say which.

## Confirm and report

Confirm the rung you pick in its rules, with the row's own `search` and `id`: category 0 holds
the title, 16 with its notes and 33 the refund, 31 the change. `detail` with that `search`
and `id` says who flies it. The condition reads the statements of 16 and nothing else, so a
rung that fits is a candidate until its rules bear it out. When they don't, say so and climb
past it with the `help` line that offers that. A carrier can file the same unconditioned
`NONREFUNDABLE` alternative in every fare's 33, the refundable ones too (Delta does): then
climbing past finds the same line again, so stop and report the rung as free to refund in the
cases its `NO CHARGE` alternatives name.

Report the result as the cheapest the ladder found, with the rungs below it, and say that
those rest on kind, notes and penalties. The ladder leaves a fare out of the slice it priced
(`2:UA.TYO+SFO.SLW47FBH`), by carrier, city pair and fare basis code with its designator, so
the same code flown the other way, or with another designator, stays in. When a `caution` line says
that the filter can't name a fare alone (a code over 10 characters with its designator goes
out with the code's other designators), or that Matrix listed a fare its filter left out,
say so with the answer: a cheaper rung can exist.
