// The demo gallery: index.html without ?s= lists every demo to watch or replay.
// A plain script, not a module, so it also runs when index.html is opened as a file;
// its data is ../out/catalog.js (window.PAPERBOY_DEMOS), which record.py writes.
(function () {
  const params = new URLSearchParams(location.search);
  if (params.get("s")) return;  // a scenario is playing: app.js has the page

  const served = location.protocol !== "file:";
  const catalog = window.PAPERBOY_DEMOS;
  const gallery = document.getElementById("gallery");
  const dialog = document.getElementById("video-dialog");
  const video = document.getElementById("video");

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function command(text) {
    return el("code", "command", text);
  }

  function watch(title, file, opener) {
    document.getElementById("video-title").textContent = title;
    video.src = `../out/videos/${file}`;
    dialog.returnFocus = opener;
    dialog.showModal();
    video.play().catch(() => {});  // autoplay may be refused; the controls still work
  }

  function actions(item, file, liveUrl) {
    const row = el("div", "actions");
    if (item.video) {
      const button = el("button", "primary", "▶ Watch video");
      button.type = "button";
      button.setAttribute("aria-label", `Watch video: ${item.title}`);
      button.addEventListener("click", () => watch(item.title, file, button));
      row.append(button);
    }
    if (item.live && served) {
      const link = el("a", "secondary", "Play live");
      link.href = liveUrl;
      link.setAttribute("aria-label", `Play live: ${item.title}`);
      row.append(link);
    }
    if (!row.children.length) {
      row.append(el("span", "missing", served ? "Not recorded yet" : "No video recorded yet"));
    }
    return row;
  }

  function card(item) {
    const node = el("article", "card");
    node.append(el("h3", null, item.title), el("p", null, item.subtitle),
      actions(item, `${item.slug}.webm`, `index.html?s=${encodeURIComponent(item.slug)}`));
    return node;
  }

  function empty() {
    const box = el("section", "empty");
    box.append(el("h2", null, "No demos recorded yet"),
      el("p", null, "From the repository folder, record the videos, or prepare every demo " +
        "and open this gallery in one step:"),
      command("python demos/record.py"), command("python demos/record.py --gallery"));
    return box;
  }

  function render() {
    document.body.classList.add("browsing");
    document.getElementById("player").hidden = true;
    gallery.hidden = false;
    document.title = "Paperboy Demos";
    document.getElementById("title").textContent = "Demos";
    const count = catalog ? catalog.groups.reduce((n, g) => n + g.demos.length, 0) : 0;
    document.getElementById("subtitle").textContent = catalog
      ? `${count} short demos, each a real conversation with the Paperboy server.`
      : "Short demos, each a real conversation with the Paperboy server.";
    if (!catalog) {
      gallery.append(empty());
      return;
    }
    const promo = catalog.promo;
    const hero = el("section", "hero");
    const text = el("div");
    text.append(el("p", "kicker", "Start here"), el("h2", null, promo.title),
      el("p", null, promo.subtitle));
    hero.append(text, actions(promo, promo.file, "promo.html"));
    gallery.append(hero);
    if (!served) {
      const note = el("p", "note", "Opened as a file, so videos only. To replay demos " +
        "live in the viewer, run ");
      note.append(command("python demos/record.py --gallery"));
      gallery.append(note);
    }
    for (const group of catalog.groups) {
      const section = el("section", "group");
      const grid = el("div", "cards");
      group.demos.forEach((item) => grid.append(card(item)));
      section.append(el("h2", null, group.name), grid);
      gallery.append(section);
    }
  }

  function close() {
    video.pause();
    video.removeAttribute("src");
    video.load();
    if (dialog.open) dialog.close();
  }

  document.getElementById("video-close").addEventListener("click", close);
  dialog.addEventListener("close", () => {
    close();
    if (dialog.returnFocus) dialog.returnFocus.focus();
  });
  // A click on the backdrop (outside the video box) closes it too.
  dialog.addEventListener("click", (event) => { if (event.target === dialog) close(); });

  render();
})();
