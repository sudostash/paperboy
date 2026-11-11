// Plays the product video from ../out/promo/script.json (written by record.py
// --promo): title cards around real scenarios, each played back in the demo viewer.
// record.py waits for window.__demoDone, as it does for a scenario.
const params = new URLSearchParams(location.search);
// ?speed=N plays N times faster; tests use it, recordings play at 1x.
const speed = Math.max(Number(params.get("speed")) || 1, 1);
const reel = document.getElementById("reel");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms / speed));

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

// Paperboy's mark: a folded newspaper, drawn in the text colour.
const MARK = '<svg class="mark" viewBox="0 0 32 32" aria-hidden="true" focusable="false"><path d="M6 5h18v19a3 3 0 0 0 3 3H9a3 3 0 0 1-3-3z" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/><path d="M24 12h3v12a3 3 0 0 1-3 3" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"/><rect x="10" y="9" width="10" height="4" rx="1" fill="#2f6fde"/><path d="M10 17h10M10 21h7" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>';

function mark() {
  const box = document.createElement("span");
  box.innerHTML = MARK;
  return box.firstElementChild;
}

let current = null;

async function scene(cls, build) {
  // Fade the previous scene out while the next one comes in.
  const next = el("section", `scene ${cls}`);
  build(next);
  if (current) {
    const old = current;
    old.classList.add("out");
    setTimeout(() => old.remove(), 450 / speed);
  }
  reel.append(next);
  current = next;
  return next;
}

function rise(node, delay) {
  node.classList.add("rise");
  node.style.animationDelay = `${delay / speed}ms`;
  return node;
}

function masthead(promo) {
  const box = el("div", "masthead");
  const name = el("div", "name");
  name.append(mark(), promo.name);
  const dateline = el("div", "dateline");
  promo.dateline.forEach((part) => dateline.append(el("span", null, part)));
  box.append(el("div", "rules"), name, dateline);
  return box;
}

async function intro(promo) {
  await scene("intro", (s) => {
    s.append(masthead(promo), rise(el("div", "tagline", promo.tagline), 700));
  });
  await sleep(4200);
}

async function hook(promo) {
  await scene("", (s) => {
    const box = el("div", "hook");
    promo.hook.forEach((line, i) => box.append(rise(el("div", "line", line), 200 + i * 650)));
    box.append(rise(el("div", "line ask", promo.ask), 200 + promo.hook.length * 650 + 300));
    s.append(box);
  });
  await sleep(5200);
}

async function what(promo) {
  await scene("", (s) => {
    const box = el("div", "what");
    const chips = el("div", "chips");
    promo.clients.forEach((name, i) => chips.append(rise(el("span", "chip", name), 900 + i * 160)));
    box.append(rise(el("div", "count", String(promo.tools)), 100),
      rise(el("div", "label", "PDF tools, one MCP server. Works with"), 400), chips);
    s.append(box);
  });
  await sleep(4600);
}

function waitFor(frame, slug) {
  // The viewer sets __demoDone when it finishes, or __demoError if it can't play.
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const timer = setInterval(() => {
      const win = frame.contentWindow;
      if (win && win.__demoError) {
        clearInterval(timer);
        reject(new Error(`${slug}: ${win.__demoError}`));
      } else if (win && win.__demoDone) {
        clearInterval(timer);
        resolve();
      } else if (Date.now() - started > 120_000) {
        clearInterval(timer);
        reject(new Error(`${slug}: did not finish playing`));
      }
    }, 100);
  });
}

async function feature(promo, item, n) {
  let frame;
  await scene("feature", (s) => {
    const header = el("header");
    const title = el("div", "title");
    title.append(el("span", "kicker", `No. ${n} of ${promo.features.length}`),
      el("h2", null, item.headline));
    header.append(title, el("p", null, item.detail));
    const win = el("div", "window");
    frame = el("iframe");
    frame.title = item.headline;
    frame.src = `index.html?s=${encodeURIComponent(item.slug)}&speed=${promo.playback * speed}&embed=1`;
    win.append(frame);
    s.append(header, win);
  });
  await waitFor(frame, item.slug);
}

async function trust(promo) {
  await scene("", (s) => {
    const box = el("div", "trust");
    const columns = el("div", "columns");
    promo.trust.forEach((item, i) => {
      const col = rise(el("div", "column"), 500 + i * 350);
      col.append(el("h3", null, item.title), el("p", null, item.detail));
      columns.append(col);
    });
    box.append(rise(el("h2", null, "Built to be trusted with your documents"), 100), columns);
    s.append(box);
  });
  await sleep(7600);
}

async function typeInto(pre, text, perChar) {
  const caret = pre.querySelector(".caret");
  for (const ch of text) {
    caret.before(ch);
    await sleep(perChar);
  }
}

async function cta(promo) {
  let pre;
  await scene("cta", (s) => {
    const term = el("div", "terminal");
    const dots = el("div", "dots");
    dots.append(el("i"), el("i"), el("i"));
    pre = el("pre");
    pre.append(el("span", "caret"));
    term.append(dots, pre);
    s.append(term);
  });
  await sleep(600);
  for (const command of promo.install) {
    pre.querySelector(".caret").before(el("span", "prompt", "$ "));
    await typeInto(pre, command, 38);
    await sleep(500);
    pre.querySelector(".caret").before("\n");
  }
  await sleep(900);
  const sign = rise(el("div", "signoff"), 0);
  const name = el("div", "name");
  name.append(mark(), promo.name);
  sign.append(name, el("p", null, promo.tagline), el("div", "url", promo.url));
  current.append(sign);
  await sleep(4500);
}

async function play() {
  const res = await fetch("../out/promo/script.json");
  if (!res.ok) throw new Error("no recorded promo script");
  const promo = await res.json();
  await intro(promo);
  await hook(promo);
  await what(promo);
  for (const [i, item] of promo.features.entries()) await feature(promo, item, i + 1);
  await trust(promo);
  await cta(promo);
  window.__demoDone = true;
}

play().catch((err) => {
  console.error(err);
  window.__demoError = String(err);
  reel.replaceChildren(el("p", "error", `Could not play the product video: ${err.message}`));
});
