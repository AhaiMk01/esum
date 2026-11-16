# Fare rules, by question

Part of [fare-rules](fare-rules.md). Read the block for the user's question: its categories,
its traps, and the part the rules leave to the airline. Say that part too, and look for data
points on it (section 3 there).

## Refund or cancel: 16, 33

- Free to refund means 16 permits cancellation with no charge and 33 says `NO CHARGE`. A fare
  sold as refundable or flexible can still file a charge, on international routes above all.
  16 that permits a refund and names no charge leaves the charge to 33.
- Cash, credit or nothing: name a credit only when the text does (`CREDIT`, `VOUCHER`, `MAY BE
  USED TOWARD THE PURCHASE OF A NEW TICKET`). When a non-refundable fare names none, say the
  rules give no refund and mention no credit, and that the airline's policy may give one.
  `WHOLLY UNUSED TICKETS MAY BE USED TOWARDS THE PURCHASE OF A NEW TICKET` names a credit:
  quote it with its time limit, and say the text files no amount or fee for it. A note on how
  long a rebooked trip stays valid belongs to the change answer.
- A window the text states (`FULLY REFUNDABLE WITHIN 1 DAY AFTER … TICKETING`) is its own
  answer, apart from the alternatives after it.
- A `NO CHARGE` alternative next to a `NONREFUNDABLE` one with no condition: the fare is free
  to refund in the case the first names, and not confirmed fully refundable. Quote both.
- Taxes and YQ/YR: `detail`'s price split shows the fare, the carrier-imposed part (YQ/YR) and
  government tax, for all passengers; on a low fare most of the price can be YQ/YR. Report
  what 33 says in its words: `TAXES` doesn't name YQ/YR, and a free-refund clause that leaves
  taxes out doesn't promise them back.
- Left to the airline: free cancellation within 24 hours of buying from it, for a ticket bought
  7 or more days before departure (a US rule; ask when they bought it); unused government
  taxes, which it may return on request; whether a fare sold as refundable is charged a fee.

## Skip a flight, no-show: 16, 33, 50

- 16 either charges for a no-show, says how it is treated (`CONSIDERED A CANCELLATION AFTER
  DEPARTURE AND CHANGES ARE NOT PERMITTED`), or says nothing. Quote it; give an amount only
  when one is filed.
- 50 can say `THE TICKET IS INVALID FOR TRAVEL IF FLIGHT COUPONS ARE NOT USED IN SEQUENCE`.
  Ask which flight the user skips: flying out and skipping the return keeps the sequence,
  skipping the first flight breaks it.
- Left to the airline: whether the remaining flights stay valid, whether a round trip flown
  one way is repriced, and what it does to a traveller who makes a habit of it.

## Change: 16, 31

- `CHANGES PERMITTED FOR REISSUE…` or `NO CHARGE FOR REISSUE OR HIGHEST FEE OF ALL CHANGED FARE
  COMPONENTS` allows a change and files no fee here. Give no number; the difference to the new
  fare is still due. Give a filed charge as "filed in 16", with its quote. A fare with no
  `change fee` note from Matrix isn't thereby free to change.
- `CHANGES NOT PERMITTED` is the answer: the fare can't be changed, and there is no fee to
  quote. `CHARGE HIGHEST FEE OF ALL CHANGED FARE COMPONENTS` with no amount: quote it and say
  the text gives no number.
- Read the notes under the statement: a service fee for a reissue by phone or at an office is
  filed there. When the text doesn't say whether it covers a change made online, say that.
- 31 often opens with branches for `INFANT WITHOUT A SEAT`: the adult's come after.
- One direction of a round trip: read both fares; the charge is the highest of the changed
  ones. 31 can file one amount before the original flight and another after: report the user's
  case and quote the other. The difference to the new fare needs a search of the new dates:
  run it, or say you didn't.
- Left to the airline: schedule changes and irregular operations. Involuntary rebooking follows
  its own policy and its agreements with other carriers, and may override the fare's rules.

## Stopover, open jaw, combining fares: 8, 9, 10, 4

- Price the trip as flown: the rules can allow a stopover that this fare's class doesn't.
- In `detail`'s fare calculation `X/TYO` is a connection and a bare `TYO` a stopover; a
  stopover charge reads `1S119.31` and is inside that fare's price. To price a stop on a fare
  you picked, keep that fare (`--extension "f ..E11USRBA|..E21USRBA"`); the total rises by the
  charge and the stop's airport taxes.
- 10: `END-ON-END` is joining another fare at either end. `HALF ROUND TRIP` pairs this fare
  with one for the other direction, from the carriers and fare types under `PROVIDED`. `OPEN
  JAWS` can print several times, each with its own `PROVIDED` for where the open segment may
  lie and how long it may be: find the one that covers the user's.

## When to buy: 5, 15

- 5 often files only how soon after booking the ticket must be issued. Then no minimum number
  of days is filed: say so and give the ticketing limits.
- The last ticketing date is `TICKETS MUST BE ISSUED ON/BEFORE` in 15, and can be days away;
  `ON/AFTER` is when sales began. With no `ON/BEFORE`, the rules file no deadline, and the fare
  can still be withdrawn any day.

## Length of stay and dates: 6, 7, 2, 3, 11, 14

Check the weekday and season of the user's own dates against 2 and 3.

## Child or infant: 19

It gives a percentage of the fare. The rules leave YQ/YR and taxes out, so a total worked out
from them is an estimate: price the trip with `--children` instead (`esum matrix search` or
`detail` lists the child's fare and taxes), and read 19 for the age limits. Infants are not
priced: for them the percentage is all there is, and say so.

## Surcharges: 12

12 is the Q surcharge inside the fare. How a carrier computes YQ/YR is filed elsewhere.
