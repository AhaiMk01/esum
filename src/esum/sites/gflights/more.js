// Page script (D-95) for Google Flights' results page. The page lists some itineraries and keeps
// the rest behind "View more flights", which it loads through a request only its own JavaScript
// can sign. This clicks that button, which only reveals the rest of the list, waits for the list
// to fill, and hands back each row's own attributes, labels and text; it sends and submits
// nothing. Parsing is in parse.py.
async (a) => {
  const deadline = Date.now() + a.timeout_ms;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const shown = (label) => [...document.querySelectorAll(`button[aria-label="${label}"]`)]
    .find((b) => b.offsetParent !== null);
  const attr = (el, name) => (el ? el.getAttribute(name) : null);
  let unread = 0;     // visible rows with a price and no flights attribute: rows this can't name
  const rows = () => {
    unread = 0;
    return [...document.querySelectorAll("li")].filter((li) => li.offsetParent !== null).map((li) => {
      const flights = li.querySelector("[data-travelimpactmodelwebsiteurl]");
      const price = li.querySelector("[data-gs]");
      if (!flights) {
        if (price) unread += 1;
        return null;
      }
      return {flights: flights.getAttribute("data-travelimpactmodelwebsiteurl"),
              labels: [...li.querySelectorAll("[aria-label]")].map((el) => el.getAttribute("aria-label")),
              text: li.innerText,
              price: attr(price, "aria-label"), token: attr(price, "data-gs"),
              co2: flights.getAttribute("data-co2currentflight"), co2_typical: flights.getAttribute("data-co2typical")};
    }).filter(Boolean);
  };
  while (!rows().length && Date.now() < deadline) await sleep(250);
  // the button can render after the rows: wait for it as long as the page's data says it exists,
  // and a moment when it says it doesn't
  const patience = a.expect_more ? deadline : Math.min(deadline, Date.now() + 1500);
  let more = shown("View more flights");
  while (!more && Date.now() < patience) {
    await sleep(250);
    more = shown("View more flights");
  }
  rows();     // counts the rows it can't name
  if (!more) return {button: false, expanded: false, unread: unread, rows: rows()};
  more.click();
  let count = -1, steady = 0;
  while (Date.now() < deadline) {
    await sleep(250);
    const n = rows().length;
    steady = n === count ? steady + 1 : 0;
    count = n;
    // done when the button says it is expanded and the list has stopped growing: the rows arrive
    // in one batch (12 of 12 timed runs), and Google's later additions need another page load
    const open = more.getAttribute("aria-expanded") === "true" || shown("View fewer flights");
    if (open && steady >= 3) return {button: true, expanded: true, unread: unread, rows: rows()};
  }
  return {button: true, expanded: false, unread: unread, rows: rows()};
}
