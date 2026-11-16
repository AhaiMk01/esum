// Page script (D-95) for Google Flights' booking page. The page fills in its booking options
// after load, through a request only its own JavaScript can sign. This waits for them and hands
// back the page's text; it clicks nothing and sends nothing. Parsing is in parse.py.
async (a) => {
  const deadline = Date.now() + a.timeout_ms;
  let last = "";
  while (Date.now() < deadline) {
    const text = document.body ? document.body.innerText : "";
    // the options arrive in pieces: done when they are there and the text has stopped changing
    if (text.includes("Book with ") && text === last) return {ready: true, text: text};
    last = text;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return {ready: false, text: last};
}
