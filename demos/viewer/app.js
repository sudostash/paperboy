// Plays back a recorded scenario transcript (../out/<slug>/script.json) as an
// animated chat + result stage. record.py waits for window.__demoDone.
import * as pdfjs from "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.4.168/build/pdf.min.mjs";

pdfjs.GlobalWorkerOptions.workerSrc =
  "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.4.168/build/pdf.worker.min.mjs";

const params = new URLSearchParams(location.search);
const slug = params.get("s");
// ?speed=N plays N times faster; tests use it, recordings play at 1x.
const speed = Math.max(Number(params.get("speed")) || 1, 1);
const base = `../out/${slug}`;
const log = document.getElementById("log");
const stage = document.getElementById("stage");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms / speed));

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function append(node) {
  log.append(node);
  log.scrollTop = log.scrollHeight;
  return node;
}

async function type(node, text, perChar = 22) {
  for (let i = 1; i <= text.length; i++) {
    node.textContent = text.slice(0, i);
    if (i % 3 === 0) log.scrollTop = log.scrollHeight;
    await sleep(perChar);
  }
}

function badge(readOnly) {
  return el("span", `badge ${readOnly ? "ro" : "rw"}`,
    readOnly ? "read-only · runs automatically" : "writes a file · asks first");
}

function brief(value, maxLines = 9) {
  const lines = JSON.stringify(value, null, 2).split("\n");
  return lines.length > maxLines ? [...lines.slice(0, maxLines), "  …"].join("\n") : lines.join("\n");
}

function clearStage() {
  stage.replaceChildren();
}

function showTools(tools) {
  clearStage();
  const ro = tools.filter((t) => t.readOnly).length;
  stage.append(el("h2", null, `Paperboy exposes ${tools.length} tools`));
  const legend = el("div", "legend");
  legend.append(badge(true), el("span", null, `${ro} tools`),
    badge(false), el("span", null, `${tools.length - ro} tools`));
  stage.append(legend);
  const grid = el("div", "tool-grid");
  const sorted = [...tools].sort((a, b) => b.readOnly - a.readOnly);
  sorted.forEach((t, i) => {
    const card = el("div", `tool ${t.readOnly ? "ro" : ""}`);
    card.style.animationDelay = `${i * 45}ms`;
    card.append(el("span", null, t.title), el("span", null, t.readOnly ? "👁" : "✎"));
    grid.append(card);
  });
  stage.append(grid);
}

async function renderStrip(file, maxHeight, password) {
  const doc = await pdfjs.getDocument({ url: `${base}/files/${file}`, password }).promise;
  const pages = [];
  for (let n = 1; n <= doc.numPages; n++) pages.push(await doc.getPage(n));
  const views = pages.map((p) => p.getViewport({ scale: 1 }));
  const gap = 14;
  const width = stage.clientWidth - 44 - gap * (views.length - 1);
  const scale = Math.min(maxHeight / Math.max(...views.map((v) => v.height)),
    width / views.reduce((s, v) => s + v.width, 0));
  const strip = el("div", "strip");
  for (const page of pages) {
    const vp = page.getViewport({ scale: scale * 2 });
    const canvas = document.createElement("canvas");
    canvas.width = vp.width;
    canvas.height = vp.height;
    canvas.style.width = `${vp.width / 2}px`;
    canvas.style.height = `${vp.height / 2}px`;
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
    strip.append(canvas);
  }
  return strip;
}

async function showFiles(show) {
  clearStage();
  const both = show.before && show.after;
  const height = (stage.clientHeight - 44 - (both ? 90 : 40)) / (both ? 2 : 1);
  for (const [label, file] of [["Before", show.before], ["After", show.after]]) {
    if (!file) continue;
    const strip = await renderStrip(file, height, show.password);
    stage.append(el("div", "strip-label", both ? `${label} · ${file}` : file), strip);
    await sleep(both ? 700 : 300);
  }
}

function typeOf(prop) {
  if (prop.anyOf) return prop.anyOf.map(typeOf).join(" | ");
  if (prop.enum) return prop.enum.map((v) => JSON.stringify(v)).join(" | ");
  if (prop.type === "array") return `${typeOf(prop.items || {})}[]`;
  if (prop.type === "object") return "object";
  return prop.type || "any";
}

function showSchema(step) {
  clearStage();
  const props = (step.schema && step.schema.properties) || {};
  stage.append(el("h2", null, `Typed result · ${step.tool}`));
  const table = el("table", "schema-table");
  const head = el("tr");
  head.append(el("th", null, "Field"), el("th", null, "Type"), el("th", null, "Value"));
  table.append(head);
  Object.entries(props).forEach(([name, prop], i) => {
    const value = step.result[name];
    const shown = JSON.stringify(value);
    const row = el("tr");
    row.style.animationDelay = `${i * 70}ms`;
    row.append(
      el("td", "field", name),
      el("td", "type", typeOf(prop)),
      el("td", value === null ? "value null" : "value",
        shown.length > 46 ? `${shown.slice(0, 44)}…` : shown),
    );
    table.append(row);
  });
  stage.append(table);
}

async function approve(card, title) {
  const bar = el("div", "approve");
  bar.append(el("span", null, `Allow Paperboy to run “${title}”?`));
  const btns = el("div", "btns");
  const allow = el("button", "allow", "Allow");
  btns.append(el("button", null, "Deny"), allow);
  bar.append(btns);
  card.append(bar);
  log.scrollTop = log.scrollHeight;
  await sleep(1400);
  allow.classList.add("pressed");
  await sleep(450);
  bar.remove();
}

async function call(step) {
  const card = append(el("div", "call"));
  const head = el("div", "call-head");
  const name = el("span");
  name.append(`🔧 ${step.title} `, el("code", null, step.tool));
  head.append(name, badge(step.readOnly));
  card.append(head, el("pre", null, JSON.stringify(step.args, null, 2)));
  log.scrollTop = log.scrollHeight;
  await sleep(900);
  if (!step.readOnly) await approve(card, step.title);
  await sleep(300);
  card.append(el("pre", "result", `✓ ${brief(step.result)}`));
  log.scrollTop = log.scrollHeight;
  if (step.showSchema) {
    showSchema(step);
    await sleep(2600);
  } else if (step.show) {
    await showFiles(step.show);
  }
  await sleep(1800);
}

async function play() {
  const res = await fetch(`${base}/script.json`);
  if (!res.ok) throw new Error("no recorded transcript");
  const script = await res.json();
  document.getElementById("title").textContent = script.title;
  document.getElementById("subtitle").textContent = script.subtitle;
  await sleep(800);
  await type(append(el("div", "msg user")), script.prompt, 24);
  await sleep(700);
  for (const step of script.steps) {
    if (step.type === "say") {
      await type(append(el("div", "msg assistant")), step.text, 14);
      await sleep(1200);
    } else if (step.type === "tools") {
      showTools(step.tools);
      await sleep(3200);
    } else if (step.type === "call") {
      await call(step);
    }
  }
  await sleep(2500);
  window.__demoDone = true;
}

function explain(message) {
  const box = el("div", "placeholder");
  box.append(el("p", null, message),
    el("code", null, "python demos/record.py --preview tool-annotations"));
  stage.replaceChildren(box);
}

if (!slug) {
  explain("No scenario selected. Open this page through the preview server:");
} else {
  play().catch((err) => {
    console.error(err);
    window.__demoError = String(err);
    explain(`Could not load scenario “${slug}” (${err.message}). Record it first, or run:`);
    throw err;
  });
}
