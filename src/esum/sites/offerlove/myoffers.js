// Page script (D-95) for Offer.love's My Offers page. The page gets the signed-in user's offers
// from the site's database over a WebSocket, keeps them in the site's own IndexedDB store and
// shows them 50 to a page; no request a client could send returns the whole list. This waits
// until the page says the user is signed in and the offers are synced, then reads that store and
// hands back a fixed set of fields per offer. It leaves out the store's tokens and account ids,
// and it types, clicks, submits and signs in to nothing. Parsing is in parse.py.
async (a) => {
  const deadline = Date.now() + a.timeout_ms;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const visible = (el) => !!el && el.offsetParent !== null;
  const body = () => document.body.innerText;
  const header = () => (body().match(/[\d,]+ offers? from [\d,]+ merchants?/) || [""])[0];
  const signedIn = () => visible(document.getElementById("logout-btn"));
  const signedOut = () => visible(document.getElementById("login-btn"));
  const synced = () => /Offers synced/i.test(body());
  const FIELDS = ["bank", "merchantName", "domainSld", "title", "details", "expiresAt", "startedAt",
                  "cardNumber", "accountLabel", "enrollmentStatus", "offerStatus", "activationRequired",
                  "isActivated"];

  while (Date.now() < deadline && !signedOut() && !(signedIn() && header() && synced())) await sleep(250);
  // signed out only when the sign-in form shows: a page showing neither is not a sign-out
  const state = {signed_in: signedIn(), signed_out: signedOut(), synced: synced(), header: header()};
  if (!state.signed_in) return {...state, offers: null};

  const db = await new Promise((resolve, reject) => {
    const open = indexedDB.open("OFFER_LOVE_DB");
    open.onsuccess = () => resolve(open.result);
    open.onerror = () => reject(open.error);
  });
  const get = (key) => new Promise((resolve, reject) => {
    const req = db.transaction("OFFER_LOVE_OFFERS_DATA_PERSONAL", "readonly")
      .objectStore("OFFER_LOVE_OFFERS_DATA_PERSONAL").get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  try {
    const data = await get("offersData");
    const updated = await get("offersDataUpdatedAt");
    const offers = [];
    for (const [merchant, list] of Object.entries(data || {})) {
      for (const o of list || []) {
        const kept = {merchant: merchant};
        for (const f of FIELDS) if (o[f] !== undefined && o[f] !== null) kept[f] = o[f];
        offers.push(kept);
      }
    }
    const when = updated instanceof Date ? updated : updated ? new Date(updated) : null;
    const iso = when && !isNaN(when) ? when.toISOString() : null;
    return {...state, updated: iso, offers: data ? offers : null};
  } finally {
    db.close();
  }
}
