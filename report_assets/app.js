/* Wyeast Case Explorer — front-end engine.
   Pure vanilla JS, no build step, no network. Reads window.* data globals that
   ship as data/*.js (loaded via <script src>, CORS-exempt under file://).
   Renders the page named in <body data-page>. */
(function () {
  "use strict";

  var PAGE_SIZE = 120;

  // ── helpers ────────────────────────────────────────────────────────────
  function $(sel, root) { return (root || document).querySelector(sel); }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  // Geo place names are "City_..._Region" (last token = state / 2-letter country):
  // "Portland_Oregon" → "Portland, Oregon", "Depoe_Bay_Oregon" → "Depoe Bay,
  // Oregon". Display-only; the raw name stays the filter key.
  function prettyPlace(s) {
    s = String(s || "");
    if (!s || s === "Unknown_Location") return s ? "Unknown location" : s;
    var parts = s.split("_");
    if (parts.length < 2) return s;
    return parts.slice(0, -1).join(" ") + ", " + parts[parts.length - 1];
  }
  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function num(n) { return (n == null ? 0 : n).toLocaleString(); }
  function stars(sig) {
    var n = parseInt(sig, 10) || 0;
    if (!n) return "";
    return '<span class="stars">' + "★".repeat(n) + "☆".repeat(Math.max(0, 5 - n)) + "</span>";
  }
  function app() { return document.getElementById("app"); }
  function heading(title, lead) {
    var h = el("div");
    h.appendChild(el("h1", null, esc(title)));
    if (lead) h.appendChild(el("p", "lead", esc(lead)));
    return h;
  }

  // Paginated grid/table renderer: keeps the full dataset in memory but only
  // mounts PAGE_SIZE DOM nodes at a time (so 37k-row corpora stay responsive).
  function paginate(container, items, renderItem, label) {
    var shown = 0;
    var holder = el("div", container.dataset.kind === "grid" ? "grid" : "");
    container.appendChild(holder);
    var note = el("p", "count-note");
    container.appendChild(note);
    var btn = el("button", "load-more", "Load more");
    function step() {
      var next = items.slice(shown, shown + PAGE_SIZE);
      next.forEach(function (it, i) { holder.appendChild(renderItem(it, shown + i)); });
      shown += next.length;
      note.textContent = "Showing " + num(shown) + " of " + num(items.length) + " " + (label || "items");
      if (shown >= items.length && btn.parentNode) btn.remove();
    }
    container.appendChild(btn);
    btn.addEventListener("click", step);
    step();
    if (!items.length) { note.textContent = "Nothing to show."; btn.remove(); }
  }

  function photoCard(r) {
    var c = el("div", "card");
    if (r.thumb) {
      var img = el("img");
      img.loading = "lazy"; img.src = r.thumb; img.alt = r.name;
      c.appendChild(img);
    } else {
      c.appendChild(el("div", "ph", esc(r.scene || "image")));
    }
    var cap = el("div", "cap");
    cap.innerHTML = '<span class="nm">' + esc(r.name) + "</span>" +
      '<span class="meta">' + esc([r.scene, prettyPlace(r.place), (r.ts || "").slice(0, 10)].filter(Boolean).join(" · ")) + "</span>";
    c.appendChild(cap);
    return c;
  }

  // ── pages ──────────────────────────────────────────────────────────────
  var PAGES = {};

  PAGES.index = function () {
    var d = window.OVERVIEW || {};
    var a = app();
    a.appendChild(heading("Case " + esc(d.case_id || ""), "An overview of everything recovered from this estate."));

    var gate = d.export_gate || {};
    if (gate.delivery_blocked) {
      var b = el("div", "banner crit");
      b.innerHTML = "<h3>Delivery blocked</h3><p>" +
        esc((gate.reasons || [gate.waiver_reason]).join("; ")) +
        "</p><p>This examiner bundle is for on-workstation review only.</p>";
      a.appendChild(b);
    } else if (d.role === "family") {
      var ok = el("div", "banner ok");
      ok.innerHTML = "<h3>Family handoff bundle</h3><p>A sanitized, self-contained explorer. Open <code>index.html</code> in any browser — no internet needed.</p>";
      a.appendChild(ok);
    }

    var c = d.counts || {};
    var tiles = el("div", "tiles");
    [["photos", "Photos"], ["people", "People"], ["places", "Places"],
     ["documents", "Documents"], ["emails", "Documents/Emails"], ["audio", "Audio"]].forEach(function (p) {
      var t = el("a", "tile");
      t.href = p[0] === "emails" ? "documents.html" : p[0] + ".html";
      t.innerHTML = '<span class="n">' + num(c[p[0]]) + "</span><span class=\"l\">" + p[1] + "</span>";
      tiles.appendChild(t);
    });
    a.appendChild(tiles);

    if ((d.ranked_top || []).length) {
      a.appendChild(el("h2", null, "Most significant"));
      var ol = el("div");
      d.ranked_top.slice(0, 24).forEach(function (r) {
        var row = el("div", "result");
        row.innerHTML = "<a href=\"" + (r.type === "photo_cluster" ? "people.html" : "documents.html") + "\">" +
          esc(r.label || r.person_id || r.file || "item") + "</a>" +
          '<span class="k">' + esc(r.type) + "</span>" +
          '<div class="s">score ' + esc((r.score || 0).toFixed ? r.score.toFixed(1) : r.score) + "</div>";
        ol.appendChild(row);
      });
      a.appendChild(ol);
    }

    if ((d.limitations || []).length) {
      a.appendChild(el("h2", null, "Limitations"));
      var ul = el("ul");
      d.limitations.forEach(function (l) {
        ul.appendChild(el("li", null, "<strong>" + esc(l.capability) + ":</strong> " + esc(l.detail)));
      });
      a.appendChild(ul);
    }
  };

  PAGES.photos = function () {
    var rows = window.PHOTOS || [];
    var a = app();
    a.appendChild(heading("Photos", num(rows.length) + " images, browsable by scene and place."));
    var bar = el("div", "toolbar");
    var scenes = {}, places = {};
    rows.forEach(function (r) { if (r.scene) scenes[r.scene] = 1; if (r.place) places[r.place] = 1; });
    bar.appendChild(selectFilter("Scene", scenes));
    bar.appendChild(selectFilter("Place", places, prettyPlace));
    a.appendChild(bar);
    var grid = el("div"); grid.dataset.kind = "grid"; a.appendChild(grid);

    function draw() {
      grid.innerHTML = "";
      var sc = bar.querySelector('[data-f="Scene"]').value;
      var pl = bar.querySelector('[data-f="Place"]').value;
      var f = rows.filter(function (r) {
        return (!sc || r.scene === sc) && (!pl || r.place === pl);
      });
      paginate(grid, f, photoCard, "photos");
    }
    bar.querySelectorAll("select").forEach(function (s) { s.addEventListener("change", draw); });
    draw();
  };

  function selectFilter(label, set, fmt) {
    var s = el("select"); s.dataset.f = label;
    s.appendChild(new Option(label + ": all", ""));
    Object.keys(set).sort().forEach(function (k) { s.appendChild(new Option(fmt ? fmt(k) : k, k)); });
    return s;
  }

  PAGES.people = function () {
    var rows = window.PEOPLE || [];
    var a = app();
    a.appendChild(heading("People", num(rows.length) + " face clusters."));
    rows.forEach(function (r) {
      var p = el("div", "person");
      var faces = el("div", "faces");
      (r.thumbs || []).forEach(function (t) { if (t) { var im = el("img"); im.src = t; im.loading = "lazy"; faces.appendChild(im); } });
      if (!faces.children.length) faces.appendChild(el("div", "ph", "no preview"));
      p.appendChild(faces);
      var body = el("div", "body");
      body.innerHTML = "<h3>" + esc(r.name) + " " + stars(r.significance) + "</h3>" +
        '<span class="badge">' + num(r.photo_count) + " photos" +
        (r.video_count ? " · " + num(r.video_count) + " videos" : "") + "</span>" +
        (r.summary ? "<p>" + esc(r.summary) + "</p>" : "");
      p.appendChild(body);
      a.appendChild(p);
    });
    if (!rows.length) a.appendChild(el("p", "notice", "No people were clustered for this case."));
  };

  PAGES.places = function () {
    var d = window.PLACES || { points: [], trips: [] };
    var a = app();
    a.appendChild(heading("Places", d.points.length + " geotagged photos across " + d.trips.length + " locations."));
    var mapEl = el("div"); mapEl.id = "map"; a.appendChild(mapEl);
    if (!d.points.length) { mapEl.outerHTML = '<p class="notice">No GPS data was found in this case.</p>'; return; }
    try {
      var map = L.map("map");
      if (typeof addBasemap === "function") addBasemap(map);
      var markers = [];
      d.points.forEach(function (p) {
        var m = L.circleMarker([p.lat, p.lon], { radius: 5, color: "#1B3A6B", fillColor: "#C9A964", fillOpacity: .8, weight: 1 });
        m.bindTooltip(esc(prettyPlace(p.trip || p.place || p.name)));
        m.addTo(map); markers.push(m);
      });
      var grp = L.featureGroup(markers);
      map.fitBounds(grp.getBounds(), { padding: [30, 30] });
    } catch (e) {
      mapEl.innerHTML = "Map failed to load: " + esc(e.message);
    }
    if (d.trips.length) {
      a.appendChild(el("h2", null, "Locations"));
      var tbl = el("table");
      tbl.innerHTML = "<tr><th>Location</th><th>Photos</th></tr>";
      d.trips.forEach(function (t) {
        tbl.appendChild(el("tr", null, "<td>" + esc(prettyPlace(t.name)) + "</td><td>" + num(t.count) + "</td>"));
      });
      a.appendChild(tbl);
    }
  };

  PAGES.documents = function () {
    var rows = window.DOCUMENTS || [];
    var a = app();
    a.appendChild(heading("Documents", num(rows.length) + " documents, sorted by significance."));
    var bar = el("div", "toolbar");
    var cats = {}; rows.forEach(function (r) { if (r.category) cats[r.category] = 1; });
    bar.appendChild(selectFilter("Category", cats));
    a.appendChild(bar);
    var holder = el("div"); a.appendChild(holder);
    function draw() {
      holder.innerHTML = "";
      var cat = bar.querySelector('[data-f="Category"]').value;
      var f = rows.filter(function (r) { return !cat || r.category === cat; });
      var tbl = el("table");
      tbl.innerHTML = "<tr><th>Document</th><th>Category</th><th>Sig.</th><th>Summary</th></tr>";
      holder.appendChild(tbl);
      paginate(holder, f, function (r) {
        var tr = el("tr");
        tr.innerHTML = "<td>" + esc(r.name) + "</td><td>" + esc(r.category.replace(/_/g, " ")) + "</td>" +
          "<td>" + stars(r.significance) + "</td>" +
          "<td>" + esc(r.summary || "") + (r.preview ? '<div class="preview">' + esc(r.preview) + "</div>" : "") + "</td>";
        tbl.appendChild(tr);
        return document.createComment("");
      }, "documents");
    }
    bar.querySelector("select").addEventListener("change", draw);
    draw();
  };

  PAGES.emails = function () {
    var d = window.EMAILS || {};
    var a = app();
    a.appendChild(heading("Emails", "Correspondence recovered from the estate."));
    if (d.available) {
      var s = d.stats || {};
      var p = el("p");
      p.innerHTML = "<a href=\"" + esc(d.href) + "\">Open the conversation browser &rarr;</a>";
      a.appendChild(p);
      var t = el("div", "tiles");
      [["emails", "Messages"], ["threads_multi", "Conversations"], ["duplicates", "Duplicates"]].forEach(function (k) {
        if (s[k[0]] != null) {
          var tile = el("div", "tile");
          tile.innerHTML = '<span class="n">' + num(s[k[0]]) + "</span><span class=\"l\">" + k[1] + "</span>";
          t.appendChild(tile);
        }
      });
      a.appendChild(t);
    } else {
      a.appendChild(el("p", "notice", "No email conversations were generated for this case."));
    }
  };

  PAGES.audio = function () {
    var rows = window.AUDIO || [];
    var a = app();
    a.appendChild(heading("Audio", num(rows.length) + " recordings with transcripts."));
    if (!rows.length) { a.appendChild(el("p", "notice", "No audio was delivered for this build.")); return; }
    var tbl = el("table");
    tbl.innerHTML = "<tr><th>Recording</th><th>Category</th><th>Sig.</th><th>Transcript</th></tr>";
    a.appendChild(tbl);
    rows.forEach(function (r) {
      var tr = el("tr");
      tr.innerHTML = "<td>" + esc(r.name) + "<div class=\"preview\">" + esc(r.language || "") +
        (r.duration ? " · " + Math.round(r.duration) + "s" : "") + "</div></td>" +
        "<td>" + esc((r.category || "").replace(/_/g, " ")) + "</td><td>" + stars(r.significance) + "</td>" +
        "<td>" + esc(r.summary || "") + (r.preview ? '<div class="preview">' + esc(r.preview) + "</div>" : "") + "</td>";
      tbl.appendChild(tr);
    });
  };

  PAGES.timeline = function () {
    var rows = window.TIMELINE || [];
    var a = app();
    a.appendChild(heading("Timeline", num(rows.length) + " dated items, oldest first."));
    var holder = el("div"); holder.dataset.kind = "grid"; a.appendChild(holder);
    paginate(holder, rows, function (r) {
      var c = photoCard({ name: r.label, thumb: r.thumb, scene: r.type, place: r.place, ts: r.ts });
      return c;
    }, "items");
  };

  PAGES.search = function () {
    var d = window.SEARCH || { records: [], index: {} };
    var a = app();
    a.appendChild(heading("Search", "Offline full-text search across photos, documents, people, and audio."));
    var box = el("input", "search-box");
    box.placeholder = "Search " + num(d.records.length) + " items…";
    a.appendChild(box);
    var out = el("div", "results"); a.appendChild(out);

    function run() {
      var q = box.value.toLowerCase().trim();
      out.innerHTML = "";
      if (q.length < 2) return;
      var toks = q.split(/[^a-z0-9]+/).filter(function (t) { return t.length > 2; });
      var scores = {};
      toks.forEach(function (tok) {
        // prefix-match across indexed terms
        Object.keys(d.index).forEach(function (term) {
          if (term.indexOf(tok) === 0) {
            d.index[term].forEach(function (id) { scores[id] = (scores[id] || 0) + 1; });
          }
        });
      });
      var hits = Object.keys(scores).map(function (id) { return [id, scores[id]]; })
        .sort(function (x, y) { return y[1] - x[1]; }).slice(0, 200);
      if (!hits.length) { out.appendChild(el("p", "count-note", "No matches.")); return; }
      out.appendChild(el("p", "count-note", hits.length + " result" + (hits.length === 1 ? "" : "s")));
      hits.forEach(function (h) {
        var r = d.records[h[0]];
        var div = el("div", "result");
        div.innerHTML = "<a href=\"" + esc(r.p) + ".html\">" + esc(r.t) + "</a>" +
          '<span class="k">' + esc(r.k) + "</span>" + (r.s ? '<div class="s">' + esc(r.s) + "</div>" : "");
        out.appendChild(div);
      });
    }
    box.addEventListener("input", run);
    box.focus();
  };

  PAGES.review = function () {
    var d = window.REVIEW || {};
    var a = app();
    a.appendChild(heading("Review queue", "Examiner-only. Items needing human attention before handoff."));
    var warn = el("div", "banner warn");
    warn.innerHTML = "<h3>On-workstation only</h3><p>This page exists only in the examiner build and is never written into a family bundle.</p>";
    a.appendChild(warn);

    var c = d.reconciliation || {};
    var tiles = el("div", "tiles");
    [["Quarantine", d.quarantine_total], ["Sensitivity flags", d.sensitive_total],
     ["Human review", d.human_review_count], ["In suspense", d.suspense_total],
     ["Credentials", (d.credentials || {}).critical_count]].forEach(function (p) {
      var t = el("div", "tile");
      t.innerHTML = '<span class="n">' + num(p[1]) + "</span><span class=\"l\">" + p[0] + "</span>";
      tiles.appendChild(t);
    });
    a.appendChild(tiles);

    if ((d.quarantine || []).length) {
      a.appendChild(el("h2", null, "Quarantine"));
      var qt = el("table"); qt.innerHTML = "<tr><th>File</th><th>Filter</th><th>When</th></tr>";
      d.quarantine.slice(0, 500).forEach(function (q) {
        qt.appendChild(el("tr", null, "<td>" + esc(q.file) + "</td><td>" + esc(q.filter) + "</td><td>" + esc((q.timestamp || "").slice(0, 19)) + "</td>"));
      });
      a.appendChild(qt);
    }
    if ((d.sensitive || []).length) {
      a.appendChild(el("h2", null, "Sensitivity flags"));
      var st = el("table"); st.innerHTML = "<tr><th>File</th><th>Human review</th><th>Filters</th></tr>";
      d.sensitive.slice(0, 500).forEach(function (s) {
        st.appendChild(el("tr", null, "<td>" + esc(s.file) + "</td><td>" + (s.human_review ? "yes" : "") +
          "</td><td>" + esc((s.filters || []).join(", ")) + "</td>"));
      });
      a.appendChild(st);
    }
    if (c.review_items && c.review_items.length) {
      a.appendChild(el("h2", null, "Reconciliation notes"));
      var ul = el("ul");
      c.review_items.forEach(function (it) { ul.appendChild(el("li", null, esc(it))); });
      a.appendChild(ul);
    }
  };

  // ── boot ───────────────────────────────────────────────────────────────
  document.addEventListener("DOMContentLoaded", function () {
    var page = document.body.dataset.page;
    // Hide the examiner-only nav link in family builds (defensive; the link is
    // also omitted server-side).
    if (document.body.dataset.role === "family") {
      var rev = document.querySelector('a[data-review]');
      if (rev) rev.remove();
    }
    var fn = PAGES[page];
    if (fn) {
      try { fn(); }
      catch (e) { app().appendChild(el("p", "notice", "Render error: " + esc(e.message))); }
    }
  });
})();
