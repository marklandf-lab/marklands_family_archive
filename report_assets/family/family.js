/* Wyeast Family Archive — front-end engine (served over HTTP by family_archive.py).
   Renders the page named in the #ctx JSON block, fetches /api/<page>, and drives
   the write-back verbs (Confirm/Banish/Rename/Release/Export/Undo) with toasts. */
(function () {
  "use strict";
  // Read the bootstrap context from a <script type="application/json" id="ctx">
  // block rather than an inline-executable global, so the page can run under a
  // strict Content-Security-Policy (script-src 'self', no 'unsafe-inline').
  var CTX = (function () {
    try {
      var node = document.getElementById("ctx");
      return node ? JSON.parse(node.textContent) : null;
    } catch (e) { return null; }
  })() || { page: "overview", role: "examiner", nav: [] };
  var EXAMINER = CTX.role === "examiner";
  var Q = (function () {
    var o = {}, s = location.search.replace(/^\?/, "");
    s.split("&").forEach(function (kv) {
      if (!kv) return;
      var p = kv.split("="); o[decodeURIComponent(p[0])] = decodeURIComponent(p[1] || "");
    });
    return o;
  })();

  // ── helpers ──
  function el(t, c, h) { var e = document.createElement(t); if (c) e.className = c; if (h != null) e.innerHTML = h; return e; }
  function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;"); }
  function num(n) { return (n == null ? 0 : n).toLocaleString(); }
  // Human byte size for attachment chips (G-4). Coerces junk to 0 → "0 B".
  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(1) + " MB";
  }
  function basename(s) { return String(s || "").split("/").pop(); }
  // #26: strip a leading "- " artifact from a blank-caller Google Voice export
  // filename (" - Voicemail - 2010-01-29T17_40_25Z.mp3") — mirrors
  // _clean_recording_name in _archive_data.py (the recordings LIST goes
  // through that; this covers the detail page, which titles itself from the
  // raw file basename instead).
  function cleanRecordingName(s) { return s.replace(/^\s*-\s*/, "") || s; }
  function pretty(s) { return String(s || "").replace(/_/g, " "); }
  // Whole-seconds offset → "m:ss" for the poster-strip frame labels (G-11).
  function fmtSecs(s) { s = Math.max(0, Math.round(Number(s) || 0)); var m = Math.floor(s / 60), ss = s % 60; return m + ":" + (ss < 10 ? "0" : "") + ss; }
  // Geo place names are "City_..._Region" (last token = state / 2-letter country),
  // e.g. "Portland_Oregon" → "Portland, Oregon", "Depoe_Bay_Oregon" → "Depoe Bay,
  // Oregon". Display-only; the raw name stays the deep-link / export / filter key.
  function prettyPlace(s) {
    s = String(s || "");
    if (!s || s === "Unknown_Location") return s ? "Unknown location" : s;
    var parts = s.split("_");
    if (parts.length < 2) return s;
    return parts.slice(0, -1).join(" ") + ", " + parts[parts.length - 1];
  }
  // Significance as an unobtrusive hover tooltip (was always-on ★ stars). role=img +
  // aria-label so the level isn't conveyed by colour/opacity alone (F-12).
  function sig(s) { var n = parseInt(s, 10) || 0; return n ? '<span class="sig" data-n="' + n + '" role="img" aria-label="Significance ' + n + ' of 5" title="Significance ' + n + '/5">●</span>' : ""; }
  function thumbURL(id) { return "/thumb?src=" + encodeURIComponent(id); }
  function mediaURL(id) { return "/media?src=" + encodeURIComponent(id); }

  // The review-queue item kinds that carry a thumbnail and are batch-discardable
  // media (as opposed to name/album guesses). One constant, three call sites (F-13).
  var MEDIA_KINDS = ["scene_guess", "unidentified_face", "face_merge"];
  function isMediaKind(k) { return MEDIA_KINDS.indexOf(k) !== -1; }

  // Make a click-only element keyboard-operable (F-12): focusable + Enter/Space fires
  // its existing click handler, so keyboard and pointer take the identical path. Call
  // AFTER the element's onclick/click listener is wired. `role` defaults to "button";
  // pass null to keep native semantics (e.g. a <tr> stays a table row, just focusable).
  // CSP-safe: addEventListener only, no inline handlers.
  function keyable(node, role, label) {
    node.setAttribute("tabindex", "0");
    if (role !== null) node.setAttribute("role", role || "button");
    if (label) node.setAttribute("aria-label", label);
    node.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
        node.click();
      }
    });
    return node;
  }

  // ── lazy thumbnails (F-5) ──
  // A grid can hold thousands of cards; assigning every <img>.src up front fires a
  // thumb-request storm at the single-threaded server (which self-DOSes). Instead,
  // an IntersectionObserver assigns src only as a card nears the viewport, and a
  // small concurrency cap keeps the number of outstanding requests bounded (the
  // rest queue). Native loading="lazy" is set too as a second line of defence.
  // One helper (lazyThumb) is reused by every grid.
  var THUMB_MAX = 6;                 // max concurrent outstanding thumb fetches
  var _thumbActive = 0, _thumbQueue = [];
  function _thumbPump() {
    while (_thumbActive < THUMB_MAX && _thumbQueue.length) {
      var img = _thumbQueue.shift();
      if (!img || img._lt_loaded || !img.dataset.src) continue;
      img._lt_loaded = true;
      img._lt_pending = true;      // still counted against THUMB_MAX; see reclaimThumbSlot
      _thumbActive++;
      var done = function () {
        img.classList.remove("thumb-loading");   // #18: clear the skeleton either way
        if (!img._lt_pending) return;   // already reclaimed on eviction — don't double-free
        img._lt_pending = false;
        _thumbActive--; _thumbPump();
      };
      img.addEventListener("load", done, { once: true });
      img.addEventListener("error", done, { once: true });
      img.src = img.dataset.src;
    }
  }
  // The windowed grid can evict a tile whose thumb is still mid-flight (its
  // first-paint row-height correction re-runs renderWindow() synchronously,
  // right after the initial mount, and can un-mount tiles it just mounted).
  // Removing an <img> from the document does not reliably fire its load/error
  // listener in every case, which would leak this slot in _thumbActive
  // forever — once enough evicted tiles do this, THUMB_MAX is permanently
  // "full" and every later thumbnail on the page silently never loads (seen:
  // a whole default Photos & Videos landing with every visible tile blank,
  // unaffected by scrolling since nothing re-queues already-mounted images).
  // Reclaim the slot proactively instead of waiting for an event that may
  // never come.
  function reclaimThumbSlot(img) {
    if (!img._lt_pending) return;
    img._lt_pending = false;
    _thumbActive--; _thumbPump();
  }
  var _thumbObserver = ("IntersectionObserver" in window)
    ? new IntersectionObserver(function (entries) {
        entries.forEach(function (en) {
          if (!en.isIntersecting) return;
          var img = en.target;
          _thumbObserver.unobserve(img);
          if (!img._lt_loaded && img.dataset.src) { _thumbQueue.push(img); _thumbPump(); }
        });
      }, { rootMargin: "400px" })
    : null;
  // Defer a thumbnail's src until it nears the viewport. `id` null → left blank
  // (e.g. a video with no poster). Returns the img for chaining.
  function lazyThumb(img, id) {
    img.loading = "lazy";
    if (id == null || id === "") return img;
    // #18: a subtle skeleton while the thumbnail is in flight (fetch/decode is
    // real network time, not a bug) instead of a blank card with no affordance.
    img.classList.add("thumb-loading");
    img.addEventListener("load", function () { img.classList.remove("thumb-loading"); }, { once: true });
    img.addEventListener("error", function () { img.classList.remove("thumb-loading"); }, { once: true });
    img.dataset.src = thumbURL(id);
    if (_thumbObserver) _thumbObserver.observe(img);
    else img.src = img.dataset.src;     // no IO support → load immediately
    return img;
  }
  // Reject (with the server's error message when the body is JSON) on any non-2xx
  // so a 500 {"error":…} or a 403 examiner-only JSON is surfaced as an error state
  // instead of being handed to a page renderer as data (blank/crashed page). #F-2
  function getJSON(p) {
    return fetch(p).then(function (r) {
      if (r.ok) return r.json();
      return r.text().then(function (body) {
        var msg = "";
        try { msg = (JSON.parse(body) || {}).error || ""; } catch (e) { msg = ""; }
        throw new Error(msg || ("Request failed (" + r.status + ")"));
      });
    });
  }
  function postJSON(p, body) {
    return fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); });
  }

  // Clamp a long recipient/participant list to `max` + "+N more recipients" (#11).
  // Accepts a list or a comma/semicolon-separated string. Returns escaped HTML.
  function recipients(value, max) {
    max = max || 3;
    var arr = Array.isArray(value) ? value.slice()
      : String(value || "").split(/[;,]/).map(function (s) { return s.trim(); }).filter(Boolean);
    if (!arr.length) return "";
    var shown = arr.slice(0, max).map(esc).join(", ");
    return arr.length > max ? shown + ' <span class="more">+' + (arr.length - max) + " more recipients</span>" : shown;
  }

  function mediaKind(f) {  // lightbox kind by extension (#16)
    var ext = String(f || "").split(".").pop().toLowerCase();
    if (["jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic"].indexOf(ext) >= 0) return "image";
    if (["mp4", "mov", "m4v", "webm", "ogv"].indexOf(ext) >= 0) return "video";
    if (ext === "pdf") return "pdf";   // F-11: previews inline in the browser PDF viewer
    // office/svg/html/txt/unknown → the "doc" stage, which renders the extracted
    // text layer inline when the pipeline produced one and otherwise shows a
    // "Download to open" card. The raw BYTES stay attachment+octet-stream+sandbox
    // (an inline frame over them only ever rendered blank or triggered a download
    // inside the overlay) — the inline view is separate, sanitised text.
    return "doc";
  }

  // ── breadcrumbs: the navigation trail (N-1) ──────────────────────────────
  // Every drill-down carries a `from` param holding the crumbs BEHIND it, as a
  // flat JSON list [{l: label, u: "/path?query"}, ...], oldest first.
  //
  // Flat, not nested. The obvious design embeds each parent URL inside its
  // child's `from`, which re-percent-encodes the entire chain at every hop
  // (`%` -> `%25` -> `%2525`); four levels deep ran past 2 KB of URL for a
  // three-word trail. A flat list costs exactly one encode no matter how deep.
  //
  // A crumb's `u` deliberately OMITS its own `from`. Clicking crumb i rebuilds
  // the trail as trail.slice(0, i), so going back up re-enters that view with
  // precisely the history that led to it and never with a stale tail behind it.
  //
  // The trail is the *path you walked*, not a fixed tree: arriving at Emails
  // from Correspondents and arriving from Search are different trails, and both
  // are truthful. Landing cold on a URL with no `from` yields no crumbs at all,
  // which is why backLink() still carries its old single-link fallback.
  var TRAIL_MAX = 6;   // deepest trail kept; older crumbs fall off the front

  // The label for the CURRENT view. Pages that render a *filtered* view (Emails
  // narrowed to one correspondent, Photos narrowed to one album) overwrite this
  // so the crumb they leave behind says "Alex Rendon" and not just "Emails".
  // render() resets it, so a page that forgets falls back to its nav label.
  var CRUMB = { label: null, node: null };
  // Pages call this while rendering — often after an async fetch resolves, by which
  // time the trail is already on screen — so it updates the live tail node too.
  function setCrumb(label) {
    if (!label) return;
    CRUMB.label = String(label);
    if (CRUMB.node) CRUMB.node.textContent = CRUMB.label;
  }

  function navLabel(page) {
    var hit = (CTX.nav || []).filter(function (n) { return n.key === page; })[0];
    return hit ? hit.label : pretty(String(page || "overview"));
  }
  // Three sources, most-specific first:
  //   CRUMB.label  the page named itself while rendering (it knows the most)
  //   Q.crumb      the link that sent us here named us — go(t, {label: ...}).
  //                The referrer usually holds the only human name available:
  //                /emails?participant=a.rendon@example.com can render a
  //                truthful crumb from the address, but only the correspondent
  //                card it was clicked from knows that address is "Alex Rendon".
  //   navLabel     the section name from the rail ("Emails")
  function crumbLabel() { return CRUMB.label || Q.crumb || navLabel(CTX.page); }

  // This view's own URL, without its `from` — what a child stores as a crumb.
  function bareURL() {
    var saved = Q.from;
    delete Q.from;
    var u = encodeURL();
    if (saved != null) Q.from = saved;
    return u;
  }

  // Same-view test. urlFor() and encodeURL() emit their params in different
  // orders (each has its own hand-maintained list), so a raw string compare
  // reported two spellings of one view as different places — and the cycle guard
  // that depends on it silently stopped guarding. Sort the params, drop `from`
  // (the trail is the thing being compared, not part of the identity).
  function canonURL(u) {
    u = String(u || "");
    var i = u.indexOf("?");
    if (i < 0) return u;
    var path = u.slice(0, i);
    var parts = u.slice(i + 1).split("&").filter(function (kv) {
      return kv && kv.split("=")[0] !== "from";
    });
    parts.sort();
    return path + (parts.length ? "?" + parts.join("&") : "");
  }

  function parseTrail(raw) {
    if (!raw) return [];
    var t;
    try { t = JSON.parse(raw); } catch (e) { return []; }   // tampered/truncated -> no crumbs
    if (!Array.isArray(t)) return [];
    return t.filter(function (c) {
      // Only same-origin absolute paths. A crumb is rendered as an href, so a
      // "//evil.example" or "javascript:" u would be an open redirect straight
      // out of the estate viewer.
      return c && typeof c.l === "string" && typeof c.u === "string" &&
             c.u.charAt(0) === "/" && c.u.charAt(1) !== "/";
    }).slice(-TRAIL_MAX);
  }
  function trailParam(trail) {
    return trail && trail.length ? "from=" + encodeURIComponent(JSON.stringify(trail)) : "";
  }

  // The trail a child of the current view should carry. Appends this view as a
  // crumb — unless this view is ALREADY in the trail, in which case the trail is
  // truncated back to it. Without that, Correspondents -> Emails -> a
  // correspondent -> Emails walks a cycle that grows the trail forever.
  function nextTrail(replace) {
    var trail = parseTrail(Q.from);
    var here = { l: crumbLabel(), u: bareURL() };
    var hereKey = canonURL(here.u);
    for (var i = 0; i < trail.length; i++) {
      if (canonURL(trail[i].u) === hereKey) return trail.slice(0, i + 1);
    }
    if (replace) return trail;          // sibling move: stand in for the current crumb
    trail = trail.concat([here]);
    return trail.slice(-TRAIL_MAX);
  }

  // Navigate to a click-through target {page, person_id?, scene?, place?, thread_id?,
  // conversation?, open?, file?}.
  // `opts.replace` marks a SIBLING move (clearing a filter, switching tabs): the
  // current view stands in for itself rather than becoming another crumb.
  // `opts.trail: false` resets the trail — for a jump that is genuinely a fresh
  // start rather than a step deeper.
  function go(target, opts) {
    if (!target) return;
    if (target.open && target.file) { lightbox(target.file, mediaKind(target.file)); return; }  // inline, not a new tab (#16)
    location.href = urlFor(target, opts);
  }

  // The URL a click-through target resolves to, trail included. Split out of go()
  // so a real <a href> carries the same crumbs a scripted navigation does —
  // otherwise middle-clicking "Open album" opened a tab with no way back, and the
  // two paths to the same view disagreed about where the visitor had been.
  function urlFor(target, opts) {
    opts = opts || {};
    var url = "/" + (target.page || "overview"), q = [];
    ["person", "scene", "place", "event", "thread", "conversation", "venue", "tab",
     "media", "vperson", "vscene", "favorite_curation", "collection_curation", "people",
     "participant", "cat", "subcat", "album", "favorite", "date_from", "date_to",
     "q", "otd", "group", "target", "list", "rec",
     // Emails index drill-down: significance band, year, and the break-down tab
     // the reader last had open, so a link into a group is fully addressable.
     "band", "year", "by", "sort",
     // Recordings: which audio kind is being shown. Reports: which report.
     "kind", "r", "view", "estate", "rescued", "rescued"].forEach(function (k) {
      var v = target[k] != null ? target[k] : target[k + "_id"];
      if (v != null && v !== "") q.push(k + "=" + encodeURIComponent(v));
    });
    // The destination's own crumb name, supplied by whoever linked to it.
    if (opts.label) q.push("crumb=" + encodeURIComponent(String(opts.label)));
    if (opts.trail !== false) {
      // If the target is somewhere we have already been, this click is a step
      // BACK up the trail, not a step deeper — drop everything past it.
      var bare = canonURL(url + (q.length ? "?" + q.join("&") : ""));
      var trail = nextTrail(opts.replace);
      for (var i = 0; i < trail.length; i++) {
        if (canonURL(trail[i].u) === bare) { trail = trail.slice(0, i); break; }
      }
      var tp = trailParam(trail);
      if (tp) q.push(tp);
    }
    if (q.length) url += "?" + q.join("&");
    return url;
  }

  // G-9: turn a /api/search record ({p:page, k:type, h:href}) into a go() target so
  // a search hit OPENS the item (photo/document → lightbox, email/message → the
  // thread/conversation) instead of only landing on the section page. A record with
  // no href (or an unknown type) falls back to its section page. Used by the FTS
  // search UI (family-archive-full-text-search.md).
  function searchTarget(rec) {
    if (!rec) return null;
    var h = rec.h;
    if (h) {
      if (rec.k === "photo" || rec.k === "document") return { open: true, file: h };
      if (rec.k === "email") return { page: "emails", thread: h };
      if (rec.k === "conversation") return { page: "messages", conversation: h };
      if (rec.k === "person") return { page: "people", person: h };
      // Recordings reads ?rec=<file> to open one item's detail (recordingDetail),
      // same as its own row click — audio search hits used to fall through to the
      // bare section page below, dropping the file ref they already carried.
      if (rec.k === "audio") return { page: "recordings", rec: h };
    }
    return { page: rec.p || "overview" };   // hrefless hits → section page
  }

  // Persist the active view sub-state in the URL (and the in-memory Q) so an action
  // that calls render() — which rebuilds the page and re-seeds its controls from Q —
  // keeps the current filter/selection instead of snapping back to default. Also
  // makes filtered views shareable/reload-stable. setQ writes one param + rewrites
  // the URL via replaceState; the page fn reads it back from Q on the next render.
  function encodeURL() {
    var q = [];
    // `from` (the breadcrumb trail) rides along here for a reason: setQ() rewrites
    // the whole URL from this list on every filter change, so a param missing from
    // it is DELETED the first time the user touches a dropdown. Leaving `from` out
    // made the trail vanish the moment you sorted the list you had drilled into.
    // It is last so the human-meaningful params stay readable in the address bar.
    ["person", "scene", "place", "event", "thread", "media", "cat", "subcat", "modality", "qcat",
     "album", "favorite", "hidden", "participant", "venue", "tab", "date_from", "date_to",
     "vperson", "vscene", "favorite_curation", "collection_curation", "people", "q",
     // The Emails index drill-down. `sort` was missing here before `band`/`year`
     // existed, with the consequence this comment describes: choosing a sort and
     // then touching any other control dropped it from the URL, so a reload or a
     // shared link lost it. `by` is the open break-down tab.
     "band", "year", "by", "sort", "kind", "r", "view", "estate",
     "group", "target", "list", "crumb", "from"]
      .forEach(function (k) { if (Q[k]) q.push(k + "=" + encodeURIComponent(Q[k])); });
    return location.pathname + (q.length ? "?" + q.join("&") : "");
  }
  function setQ(updates) {
    Object.keys(updates).forEach(function (k) {
      if (updates[k]) Q[k] = updates[k]; else delete Q[k];
    });
    history.replaceState(null, "", encodeURL());
  }

  // Persistent polite live region so screen readers announce every verb result
  // (F-12). Toasts are appended here rather than to <body>; each keeps its own
  // position:fixed placement, so the region is layout-neutral (no children in flow).
  function toastRegion() {
    var r = document.getElementById("toasts");
    if (!r) {
      r = el("div"); r.id = "toasts";
      r.setAttribute("role", "status");
      r.setAttribute("aria-live", "polite");
      r.setAttribute("aria-atomic", "false");
      document.body.appendChild(r);
    }
    return r;
  }

  function toast(msg, undoToken) {
    var t = el("div", "toast", esc(msg));
    if (undoToken && EXAMINER) {
      var u = el("span", "undo", "Undo");
      u.onclick = function () {
        postJSON("/api/undo", { undo_token: undoToken }).then(function (res) {
          if (res.ok && res.j.ok !== false) { location.reload(); return; }
          toast("Couldn't undo: " + (res.j.error || "error"));
        }).catch(function (e) { toast("Couldn't undo: " + (e && e.message ? e.message : "server error")); });
      };
      t.appendChild(u);
    }
    toastRegion().appendChild(t);
    setTimeout(function () { t.remove(); }, 6000);
  }

  // Always resolves — never rejects. On an HTTP error, a non-JSON 500, or a network
  // drop it toasts the reason and returns null, so every call site's `if (!x)` /
  // `else` branch runs and restores its button/disabled state (was: a rejected verb
  // showed no toast AND left "Discarding…" stuck disabled forever). #F-2
  function doVerb(path, body, okMsg) {
    return postJSON(path, body).then(function (res) {
      if (res.ok && res.j.ok !== false) { toast(okMsg, res.j.undo_token); return res.j; }
      toast("Couldn't do that: " + (res.j.error || "error")); return null;
    }).catch(function (e) {
      toast("Couldn't do that: " + (e && e.message ? e.message : "server error"));
      return null;
    });
  }

  // #11: debounce a live search-box handler (waits for a pause in typing before
  // firing) so an in-list ?q= filter doesn't re-fetch on every keystroke.
  function debounce(fn, ms) {
    var t = null;
    return function () {
      var args = arguments;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(null, args); }, ms);
    };
  }

  // G-15: pick a target person cluster from a small modal, then cb(person_id).
  // `excludeId` is dropped from the list (can't merge/assign into the same person).
  // CSP-safe: no inline handlers (addEventListener/.onclick only); the <option> text
  // is set via new Option(text,…) which assigns textContent, so names are never
  // interpreted as HTML; person_ids ride the option VALUE and go into a JSON body.
  // Shared modal shell (#14): every pick*() below built this same back/box/
  // Confirm/Cancel/Escape/click-outside scaffold by hand. Centralized here so
  // new callers (textPrompt below; person/collection rename, add-to-collection)
  // get the same in-app dialog instead of falling back to a raw prompt()/confirm().
  // `opts.onConfirm(close)` reads the body's current value and decides whether
  // to close (a validation failure can toast and leave the dialog open).
  function pickmodal(title, bodyEl, opts) {
    opts = opts || {};
    var back = el("div", "pickmodal-back");
    var box = el("div", "pickmodal");
    box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", title);
    box.appendChild(el("h3", null, esc(title)));
    box.appendChild(bodyEl);
    var actions = el("div", "pickmodal-actions");
    var ok = el("button", "btn primary", opts.confirmLabel || "Confirm");
    var cancel = el("button", "btn", "Cancel");
    function onKey(e) { if (e.key === "Escape") close(); }
    function close() { document.removeEventListener("keydown", onKey); back.remove(); }
    function confirm() { if (opts.onConfirm) opts.onConfirm(close); }
    ok.onclick = confirm;
    cancel.onclick = close;
    back.addEventListener("click", function (e) { if (e.target === back) close(); });
    document.addEventListener("keydown", onKey);
    actions.appendChild(ok); actions.appendChild(cancel);
    box.appendChild(actions);
    back.appendChild(box); document.body.appendChild(back);
    return { box: box, close: close, confirm: confirm };
  }

  // A styled, in-app replacement for prompt() (#14): same callback shape as the
  // pick*() functions — `cb` only fires on Confirm, never on Cancel/Escape (no
  // `if (name == null) return;` needed at call sites, unlike native prompt()).
  function textPrompt(title, defaultValue, cb) {
    var input = el("input", "pickmodal-sel");
    input.type = "text"; input.value = defaultValue || "";
    var m = pickmodal(title, input, {
      onConfirm: function (close) { var v = input.value; close(); cb(v); },
    });
    input.addEventListener("keydown", function (e) { if (e.key === "Enter") m.confirm(); });
    try { input.focus(); input.select(); } catch (e) { /* no focus */ }
  }

  function pickPerson(title, excludeId, cb) {
    getJSON("/api/people").then(function (rows) {
      var choices = (rows || []).filter(function (r) { return r.person_id !== excludeId; });
      if (!choices.length) { toast("No other people to choose from."); return; }
      var sel = el("select", "pickmodal-sel");
      choices.forEach(function (r) {
        sel.appendChild(new Option((r.name || r.person_id) + " · " + num(r.photo_count) + " photos",
                                    r.person_id));   // text is textContent-safe
      });
      var m = pickmodal(title, sel, {
        onConfirm: function (close) { var pid = sel.value; close(); if (pid) cb(pid); },
      });
      try { sel.focus(); } catch (e) { /* no focus */ }
    }).catch(function (e) { toast("Couldn't load people: " + (e && e.message ? e.message : "error")); });
  }

  // Scene picker (Phase-1.5 Move): choose a target gallery scene CATEGORY. The
  // choices are the distinct scene categories present across the whole photo set
  // (loaded over every page), minus the item's current scene. The server validates
  // the target again (a scanned-document / unknown category is refused), so this is
  // just the affordance. Text is textContent-safe (pretty()); ids never inlined.
  function pickScene(title, excludeScene, cb) {
    loadAllRows("/api/photos", function (rows) {
      var seen = {}, choices = [];
      (rows || []).forEach(function (r) {
        var s = r.scene;
        if (s && s !== excludeScene && !seen[s]) { seen[s] = 1; choices.push(s); }
      });
      choices.sort();
      if (!choices.length) { toast("No other scenes to choose from."); return; }
      var sel = el("select", "pickmodal-sel");
      choices.forEach(function (s) {
        sel.appendChild(new Option(pretty(s), s));   // Option text is textContent-safe
      });
      var m = pickmodal(title, sel, {
        onConfirm: function (close) { var s = sel.value; close(); if (s) cb(s); },
      });
      try { sel.focus(); } catch (e) { /* no focus */ }
    });
  }

  // Album picker (Move Phase 2): choose a target event album. The choices are the
  // configured event albums (from /api/events), minus the item's current album.
  // The server validates the target again (an unknown album_id → 409), so this is
  // just the affordance. Option text is textContent-safe (esc not needed for
  // Option); album_ids ride the option VALUE and go into a JSON body.
  function pickAlbum(title, excludeAlbum, cb) {
    getJSON("/api/events").then(function (albs) {
      var choices = (albs || []).filter(function (a) {
        return String(a.album_id) !== String(excludeAlbum);
      });
      if (!choices.length) { toast("No other albums to choose from."); return; }
      var sel = el("select", "pickmodal-sel");
      choices.forEach(function (a) {
        sel.appendChild(new Option((a.title || a.album_id) + " · " + num(a.count) + " photos",
                                    String(a.album_id)));   // text is textContent-safe
      });
      var m = pickmodal(title, sel, {
        onConfirm: function (close) { var aid = sel.value; close(); if (aid) cb(aid); },
      });
      try { sel.focus(); } catch (e) { /* no focus */ }
    }).catch(function (e) { toast("Couldn't load albums: " + (e && e.message ? e.message : "error")); });
  }

  // Category picker (Move Phase 2.5): choose a target DOCUMENT category. The choices
  // are served from /api/doc-categories (the case-config document_categories, or the
  // stdlib fallback, ALREADY MINUS account_credentials — the sealed category is never
  // a movable target, §13.3). `excludeCat` drops the item's current category. The
  // server re-validates (unknown/no-op/account_credentials refused), so this is just
  // the affordance. Option text is textContent-safe (pretty()); category strings ride
  // the option VALUE and go into a JSON body.
  function pickCategory(title, excludeCat, cb) {
    getJSON("/api/doc-categories").then(function (resp) {
      var choices = ((resp && resp.categories) || []).filter(function (c) {
        return c !== excludeCat;
      });
      if (!choices.length) { toast("No other categories to choose from."); return; }
      var sel = el("select", "pickmodal-sel");
      choices.forEach(function (c) {
        sel.appendChild(new Option(pretty(c), c));   // Option text is textContent-safe
      });
      var m = pickmodal(title, sel, {
        onConfirm: function (close) { var c = sel.value; close(); if (c) cb(c); },
      });
      try { sel.focus(); } catch (e) { /* no focus */ }
    }).catch(function (e) { toast("Couldn't load categories: " + (e && e.message ? e.message : "error")); });
  }

  // Vital-document target picker (reassign): choose a DIFFERENT vital-document
  // target for a checklist item. `choices` is vd.all_targets ([{target,label}] — the
  // canonical 13 types + labels served by the documents payload); `excludeTarget`
  // (the item's current display target) is dropped. Labels are textContent-safe via
  // new Option(); the target id rides the option VALUE and goes into a JSON body —
  // never inlined into HTML. Mirrors pickCategory (CSP-safe: addEventListener/.onclick).
  function pickVitalTarget(title, choices, excludeTarget, cb) {
    var opts = (choices || []).filter(function (c) { return c.target !== excludeTarget; });
    if (!opts.length) { toast("No other document types to choose from."); return; }
    var sel = el("select", "pickmodal-sel");
    opts.forEach(function (c) {
      sel.appendChild(new Option(c.label || c.target, c.target));   // Option text is textContent-safe
    });
    var m = pickmodal(title, sel, {
      onConfirm: function (close) { var v = sel.value; close(); if (v) cb(v); },
    });
    try { sel.focus(); } catch (e) { /* no focus */ }
  }

  // The distinct vital categories a given document PATH currently matches (used to
  // decide whether a reassign needs a scope prompt). A doc confirmed under >1 target
  // shows up once per target in vd.targets[].items.
  // Display label for a vital target slug, from whichever list carries it.
  function vitalTargetLabel(vd, target) {
    var all = (vd.all_targets || []).filter(function (x) {
      return (x.target || x.key) === target; })[0];
    if (all && all.label) return all.label;
    var t = (vd.targets || []).filter(function (x) { return x.target === target; })[0];
    return (t && t.label) || pretty(target);
  }

  // A plain confirm dialog for a verb whose reach is wider than the row it sits
  // on. Same shape as pickScope; the cautious option is the one that is focused.
  function confirmModal(title, body, goLabel, cb) {
    var back = el("div", "pickmodal-back");
    var box = el("div", "pickmodal");
    box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", title);
    box.appendChild(el("h3", null, esc(title)));
    box.appendChild(el("p", "pickmodal-note", esc(body)));
    var actions = el("div", "pickmodal-actions");
    var go = el("button", "btn", esc(goLabel));
    var cancel = el("button", "btn primary", "Cancel");
    function onKey(e) { if (e.key === "Escape") close(); }
    function close() { document.removeEventListener("keydown", onKey); back.remove(); }
    go.onclick = function () { close(); cb(); };
    cancel.onclick = close;
    back.addEventListener("click", function (e) { if (e.target === back) close(); });
    document.addEventListener("keydown", onKey);
    actions.appendChild(cancel); actions.appendChild(go);
    box.appendChild(actions);
    back.appendChild(box); document.body.appendChild(back);
    try { cancel.focus(); } catch (e) { /* no focus */ }
  }

  function vitalPathTargets(vd, path) {
    var seen = {};
    (vd.targets || []).forEach(function (t) {
      (t.items || []).forEach(function (it) { if (it.path === path) seen[t.target] = 1; });
    });
    return Object.keys(seen);
  }

  // Reassign SCOPE chooser: when a document matched >1 vital category, ask whether
  // the reassign applies to EVERY category (global) or only the clicked one (single).
  // CSP-safe (addEventListener/.onclick, no inline handlers); text via esc()+innerHTML.
  function pickScope(nCats, cb) {
    var back = el("div", "pickmodal-back");
    var box = el("div", "pickmodal");
    box.setAttribute("role", "dialog"); box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "Reassign scope");
    box.appendChild(el("h3", null, "Reassign in how many categories?"));
    box.appendChild(el("p", "pickmodal-note",
      esc("This document is a vital match in " + nCats + " categories.")));
    var actions = el("div", "pickmodal-actions");
    var all = el("button", "btn primary", esc("All " + nCats + " categories"));
    var one = el("button", "btn", "Just this one");
    var cancel = el("button", "btn", "Cancel");
    function onKey(e) { if (e.key === "Escape") close(); }
    function close() { document.removeEventListener("keydown", onKey); back.remove(); }
    all.onclick = function () { close(); cb("global"); };
    one.onclick = function () { close(); cb("single"); };
    cancel.onclick = close;
    back.addEventListener("click", function (e) { if (e.target === back) close(); });
    document.addEventListener("keydown", onKey);
    actions.appendChild(all); actions.appendChild(one); actions.appendChild(cancel);
    box.appendChild(actions);
    back.appendChild(box); document.body.appendChild(back);
    try { all.focus(); } catch (e) { /* no focus */ }
  }

  // Financial SUB-category picker (§14.5) — mirrors pickCategory but lists the
  // movable financial subcategories served additively at /api/doc-categories
  // `financial_subcategories` (case-config, or the stdlib fallback; an EMPTY list
  // means the second pass is disabled → no targets). `excludeSub` drops the row's
  // current subcategory. Only offered on rows already category=="financial"
  // (client-side gate); the server re-validates. textContent-safe (pretty()).
  function pickSubcategory(title, excludeSub, cb) {
    getJSON("/api/doc-categories").then(function (resp) {
      var choices = ((resp && resp.financial_subcategories) || []).filter(function (s) {
        return s !== excludeSub;
      });
      if (!choices.length) { toast("No other sub-categories to choose from."); return; }
      var sel = el("select", "pickmodal-sel");
      choices.forEach(function (s) {
        sel.appendChild(new Option(pretty(s), s));   // Option text is textContent-safe
      });
      var m = pickmodal(title, sel, {
        onConfirm: function (close) { var s = sel.value; close(); if (s) cb(s); },
      });
      try { sel.focus(); } catch (e) { /* no focus */ }
    }).catch(function (e) { toast("Couldn't load sub-categories: " + (e && e.message ? e.message : "error")); });
  }

  // ── shell ──
  function shell() {
    var app = document.getElementById("app");
    app.innerHTML = "";
    var sh = el("div", "shell");
    var rail = el("nav", "rail");
    rail.setAttribute("role", "navigation");
    rail.setAttribute("aria-label", "Archive sections");
    rail.innerHTML = '<div class="brand"><span class="mark">Wyeast</span><span class="sub">Family Archive</span></div>';
    // Full-text search box — present on every page (family-archive-full-text-search.md).
    // CSP-safe: no inline handlers, submit navigates to the /search results page.
    var railForm = el("form", "railsearch");
    var railInput = el("input", "railsearch-input");
    railInput.type = "search";
    railInput.name = "q";
    railInput.placeholder = "Search the archive…";
    railInput.setAttribute("aria-label", "Search the archive");
    railInput.autocomplete = "off";
    if (CTX.page === "search" && Q.q) railInput.value = Q.q;
    railForm.appendChild(railInput);
    railForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var v = railInput.value.trim();
      location.href = v ? "/search?q=" + encodeURIComponent(v) : "/search";
    });
    rail.appendChild(railForm);
    (CTX.nav || []).forEach(function (item) {
      var active = item.key === CTX.page;
      var a = el("a", "navlink" + (active ? " active" : ""), esc(item.label));
      a.href = "/" + item.key;
      if (active) a.setAttribute("aria-current", "page");
      rail.appendChild(a);
    });
    rail.appendChild(el("div", "spacer"));
    rail.appendChild(el("div", "casemeta", esc(CTX.case_id) + '<br><span class="role-pill">' + esc(CTX.role) + '</span>'));
    var main = el("main", "main"); main.id = "main"; main.setAttribute("role", "main");
    sh.appendChild(rail); sh.appendChild(main); app.appendChild(sh);
    return main;
  }

  // Sticky section header. Title group on the left; a right-aligned .controls slot
  // (#7) for filter dropdowns that must stay reachable while the body scrolls.
  // Returns the .controls element so callers can append their controls into it.
  function head(main, eyebrow, title, lead) {
    var ph = el("div", "pagehead");
    var titleWrap = el("div", "pagehead-title");
    if (eyebrow) titleWrap.appendChild(el("div", "eyebrow", esc(eyebrow)));
    titleWrap.appendChild(el("h1", null, esc(title)));
    if (lead) titleWrap.appendChild(el("p", "lead", esc(lead)));
    var controls = el("div", "controls");
    ph.appendChild(titleWrap); ph.appendChild(controls);
    main.appendChild(ph);
    return controls;
  }

  // The way back. When the visitor walked here, render the whole trail so one
  // click reaches ANY level above — that is the point of a breadcrumb over a
  // back link, and it is what the single hardcoded "All emails" link could never
  // do: it always jumped to the top of the section, discarding the correspondent
  // (or album, or search) the visitor had drilled through to get here.
  //
  // With no trail (a bookmarked URL, a fresh tab, a reload of a deep link) there
  // is no honest history to show, so the caller's original single link stands in.
  // `label`/`target` are therefore still required at every call site.
  function backLink(main, label, target) {
    // Walked here → render() already drew the full trail above the page head;
    // a second "← All emails" underneath it would be redundant and, worse, would
    // disagree with the crumbs about where "back" goes.
    if (parseTrail(Q.from).length) return;
    var a = el("a", "backlink", "← " + esc(label));
    a.href = "#"; a.onclick = function (e) { e.preventDefault(); go(target, { trail: false }); };
    main.appendChild(a);
  }

  // Render a crumb trail plus the current view as the (unlinked) tail. Crumb i
  // links to its own URL carrying trail.slice(0, i) — the history that led to it.
  // Real <a href>s, so middle-click/open-in-new-tab work and the destination
  // shows in the status bar on hover.
  function breadcrumb(main, trail) {
    trail = trail || parseTrail(Q.from);
    var nav = el("nav", "crumbs");
    nav.setAttribute("aria-label", "Breadcrumb");
    var ol = el("ol", "crumblist");
    trail.forEach(function (c, i) {
      var li = el("li", "crumb");
      var a = el("a", null, esc(c.l));
      var tp = trailParam(trail.slice(0, i));
      a.href = c.u + (tp ? (c.u.indexOf("?") >= 0 ? "&" : "?") + tp : "");
      li.appendChild(a);
      ol.appendChild(li);
    });
    var here = el("li", "crumb current", esc(crumbLabel()));
    here.setAttribute("aria-current", "page");
    CRUMB.node = here;
    ol.appendChild(here);
    nav.appendChild(ol);
    main.appendChild(nav);
    return nav;
  }

  // ── targeted view updates ──
  // A page can register removeItems(ids) so a verb success updates the CURRENT
  // view in place instead of re-fetching the whole dataset and rebuilding up to
  // 2,000 cards (the "re-render-the-world" pattern). render() clears the hook;
  // pages without one fall back to a full render() unchanged.
  var VIEW = { removeItems: null };
  function dropItems(ids) {
    if (!VIEW.removeItems) return false;
    VIEW.removeItems(ids);
    return true;
  }

  // ── Load-more pagination (F-3) ──
  // Drives a paginated section endpoint ({rows,total,offset,limit}). Seed from the
  // page-1 payload the router already fetched, then Load-more fetches the next
  // ?offset=&limit= slice and APPENDS to the accumulated set. `opts.getParams()`
  // adds server params (sort/date/category); change them and call load(true) to
  // refetch from offset 0. `opts.render(allRows, total, reset)` redraws the body.
  // The returned `.footer` carries the "Showing 1–N of M" note + Load-more button
  // — append it below the body. CSP-safe: no inline handlers (addEventListener/
  // .onclick only), getJSON rejects on non-2xx.
  function pager(apiPath, opts) {
    opts = opts || {};
    // Extract the {rows,total,...} envelope from a response — top-level by default,
    // but a nested key for endpoints that paginate a sub-list (correspondence's
    // `scanned`). Applied to BOTH the Load-more fetch and the seed payload.
    var unwrap = opts.unwrap || function (d) { return d || {}; };
    var st = { rows: [], total: 0, offset: 0, limit: 2000, loading: false,
               footer: el("div", "pager") };
    function draw() {
      st.footer.innerHTML = "";
      var shown = st.rows.length;
      st.footer.appendChild(el("p", "count-note",
        st.total ? ("Showing 1–" + num(shown) + " of " + num(st.total)) : "Nothing here."));
      if (shown < st.total) {
        var b = el("button", "btn", "Load more (" + num(st.total - shown) + " remaining)");
        b.onclick = function () { b.disabled = true; st.load(false); };
        st.footer.appendChild(b);
      }
    }
    st.draw = draw;
    st.load = function (reset) {
      if (st.loading) return Promise.resolve();
      st.loading = true;
      if (reset) { st.rows = []; st.offset = 0; }
      var params = (opts.getParams && opts.getParams()) || {};
      var q = ["offset=" + st.offset, "limit=" + st.limit];
      Object.keys(params).forEach(function (k) {
        if (params[k] != null && params[k] !== "") q.push(k + "=" + encodeURIComponent(params[k]));
      });
      return getJSON(apiPath + "?" + q.join("&")).then(function (d) {
        st.loading = false;
        var u = unwrap(d), rows = (u && u.rows) || [];
        st.rows = st.rows.concat(rows);
        st.total = (u && u.total != null) ? u.total : st.rows.length;
        st.offset = st.rows.length;
        if (opts.onData) opts.onData(d);   // raw payload (e.g. photo facets)
        opts.render(st.rows, st.total, reset);
        draw();
      }).catch(function (e) {
        st.loading = false;
        toast("Couldn't load more: " + (e && e.message ? e.message : "error"));
        draw();
      });
    };
    // Seed from the router's page-1 payload (avoids a duplicate first fetch).
    st.seed = function (d) {
      var u = unwrap(d);
      st.rows = (u && u.rows) || [];
      st.total = (u && u.total != null) ? u.total : st.rows.length;
      st.offset = st.rows.length;
      if (opts.onData) opts.onData(d);   // raw payload (e.g. photo facets)
      opts.render(st.rows, st.total, true);
      draw();
    };
    return st;
  }

  // Fetch EVERY page of a paginated endpoint (for internal consumers that need the
  // whole set — e.g. the Places drill-in filtering photos to one location, or the
  // Photos page's video pool). Pages in 2,000-row chunks so no single response is
  // oversized. Calls done(allRows) once; toasts + returns what it has on error.
  function loadAllRows(apiPath, done, onFirst) {
    var acc = [], first = true;
    function step(offset) {
      var sep = apiPath.indexOf("?") >= 0 ? "&" : "?";
      getJSON(apiPath + sep + "offset=" + offset + "&limit=2000").then(function (d) {
        if (first) { first = false; if (onFirst) onFirst(d); }   // raw payload (e.g. video facets)
        var rows = (d && d.rows) || [];
        acc = acc.concat(rows);
        var total = (d && d.total != null) ? d.total : acc.length;
        if (rows.length && acc.length < total) step(acc.length);
        else done(acc);
      }).catch(function (e) {
        toast("Couldn't load all: " + (e && e.message ? e.message : "error"));
        done(acc);
      });
    }
    step(0);
  }

  // Repopulate a <select> from `values`, preserving the current selection if it
  // still exists (filter dropdowns rebuilt as more pages load).
  function fillSelect(sel, allLabel, values, labelFn) {
    var cur = sel.value;
    sel.innerHTML = "";
    sel.appendChild(new Option(allLabel, ""));
    values.forEach(function (v) { sel.appendChild(new Option(labelFn ? labelFn(v) : v, v)); });
    if (cur && values.indexOf(cur) >= 0) sel.value = cur; else sel.value = "";
  }

  // ── selection bar ──
  var SEL = {};
  // file id → rendered category, populated by fileTable — lets a batch sub-move
  // restrict the selection to financial rows (§14.6, client-side gate).
  var DOC_CAT = {};
  function selbar() {
    var bar = document.getElementById("selbar");
    if (!bar) { bar = el("div", "selbar"); bar.id = "selbar"; document.body.appendChild(bar); }
    var ids = Object.keys(SEL);
    bar.innerHTML = '<span class="n">' + ids.length + ' selected</span><span class="sep"></span>';
    var exp = el("button", "act primary", "Export");
    exp.onclick = function () { doVerb("/api/export", { items: ids }, "Exported " + ids.length + " item(s)").then(clearSel); };
    bar.appendChild(exp);
    if (EXAMINER) {
      // "Discard" is the user-facing label; the verb/route/dir stay "banish" (#10).
      var ban = el("button", "act danger", "Discard");
      ban.onclick = function () {
        // Group discard confirms; a single item is immediate (both reversible
        // from History).
        if (ids.length > 1 && !confirm("Discard " + ids.length + " items? They are hidden from the archive "
          + "(removed from view); this is reversible from the History view.")) return;
        // One batched request (server reloads the case once) + immediate disabled
        // feedback, so a multi-select Discard updates the grid promptly instead of
        // lagging seconds behind per-item reloads and looking like it failed.
        ban.disabled = true; ban.textContent = "Discarding…";
        doVerb("/api/banish", { srcs: ids }, "Discarded " + ids.length + " item(s)")
          .then(function (x) {
            if (!x) { ban.disabled = false; ban.textContent = "Discard"; return; }
            clearSel();
            if (!dropItems(ids)) render();   // in-place removal when the page supports it
          });
      };
      bar.appendChild(ban);
      // Batch Move (Phase 1.5) — reuses the selection + toast/undo wiring. On a
      // person_detail grid it re-files the selection under another person; on the
      // Photos gallery it relabels the selection's scene facet. One batched request
      // (server writes once, reloads once); skip-not-fail on any non-movable member.
      if (CTX.page === "people" && Q.person) {
        var mvp = el("button", "act", "Move to person…");
        mvp.onclick = function () {
          pickPerson("Move " + ids.length + " item(s) to which person?", Q.person, function (pid) {
            mvp.disabled = true; mvp.textContent = "Moving…";
            doVerb("/api/move", { view: "person", srcs: ids, to: pid },
                   "Moved " + ids.length + " item(s)").then(function (x) {
              if (!x) { mvp.disabled = false; mvp.textContent = "Move to person…"; return; }
              clearSel();
              if (!dropItems(ids)) render();
            });
          });
        };
        bar.appendChild(mvp);
      }
      if (CTX.page === "photos") {
        var mvs = el("button", "act", "Move to scene…");
        mvs.onclick = function () {
          pickScene("Move " + ids.length + " item(s) to which scene?", null, function (cat) {
            mvs.disabled = true; mvs.textContent = "Moving…";
            doVerb("/api/move", { view: "scene", srcs: ids, to: cat },
                   "Moved " + ids.length + " item(s)").then(function (x) {
              if (!x) { mvs.disabled = false; mvs.textContent = "Move to scene…"; return; }
              clearSel(); render();
            });
          });
        };
        bar.appendChild(mvs);
        // Batch event-move (Phase 2): re-file the selection into one event album.
        var mva = el("button", "act", "Move to album…");
        mva.onclick = function () {
          pickAlbum("Move " + ids.length + " item(s) to which album?", null, function (aid) {
            mva.disabled = true; mva.textContent = "Moving…";
            doVerb("/api/move", { view: "event", srcs: ids, to: aid },
                   "Moved " + ids.length + " item(s)").then(function (x) {
              if (!x) { mva.disabled = false; mva.textContent = "Move to album…"; return; }
              clearSel(); render();
            });
          });
        };
        bar.appendChild(mva);
      }
      // Batch document-category move (Phase 2.5): re-file the selected documents into
      // one category. On the Documents and Correspondence pages the selection is doc
      // `file` ids (fileTable). Skip-not-fail (a credential/email/no-op member is
      // skipped server-side, §13.3/§13.4); a full re-render reflects the new buckets.
      if (CTX.page === "documents" || CTX.page === "correspondence") {
        var mvc = el("button", "act", "Move to category…");
        mvc.onclick = function () {
          pickCategory("Move " + ids.length + " document(s) to which category?", null, function (cat) {
            mvc.disabled = true; mvc.textContent = "Moving…";
            doVerb("/api/move", { view: "document", srcs: ids, to: cat },
                   "Moved " + ids.length + " document(s)").then(function (x) {
              if (!x) { mvc.disabled = false; mvc.textContent = "Move to category…"; return; }
              clearSel(); render();
            });
          });
        };
        bar.appendChild(mvc);
        // Batch financial SUB-category move (Phase 2.6): restrict the selection to
        // financial rows (§14.6 — a mixed batch would MOVE non-financial members
        // into financial, so the UI gates to financial-only). Shown only when the
        // selection includes at least one financial row.
        var finIds = ids.filter(function (id) { return DOC_CAT[id] === "financial"; });
        if (finIds.length) {
          var mvsub = el("button", "act", "Move to sub-category…");
          mvsub.onclick = function () {
            pickSubcategory("Move " + finIds.length + " financial document(s) to which sub-category?", null, function (sub) {
              mvsub.disabled = true; mvsub.textContent = "Moving…";
              doVerb("/api/move", { view: "document", srcs: finIds, to: "financial", subcategory: sub },
                     "Moved " + finIds.length + " document(s)").then(function (x) {
                if (!x) { mvsub.disabled = false; mvsub.textContent = "Move to sub-category…"; return; }
                clearSel(); render();
              });
            });
          };
          bar.appendChild(mvsub);
        }
        // #19: a scanned-document/handwritten-letter image the scene classifier
        // mis-tagged has no Move (scanned isn't a document_classifications
        // category — the Move machinery excludes it) and Discard would hide it
        // entirely when the family may still want the photo, just not filed as
        // correspondence. Server-side skip-not-fail (like the sub-category gate
        // above) means a mixed selection (typed/handwritten doc rows alongside
        // scanned images) just quietly skips the non-scanned members.
        if (CTX.page === "correspondence") {
          var relsc = el("button", "act", "Not a document");
          relsc.title = "Release back to Photos — it isn't a scanned document/letter";
          relsc.onclick = function () {
            relsc.disabled = true; relsc.textContent = "Releasing…";
            doVerb("/api/release/scanned", { ids: ids }, "Released " + ids.length + " item(s) to Photos")
              .then(function (x) {
                if (!x) { relsc.disabled = false; relsc.textContent = "Not a document"; return; }
                clearSel();
                if (!dropItems(ids)) render();
              });
          };
          bar.appendChild(relsc);
        }
      }
    }
    var clr = el("button", "act", "Deselect all");
    clr.onclick = clearSel;
    bar.appendChild(clr);
    bar.classList.toggle("show", ids.length > 0);
  }
  function clearSel() {
    SEL = {}; selbar();
    document.querySelectorAll(".sel").forEach(function (c) { c.classList.remove("sel"); });
    document.querySelectorAll("input.rowpick:checked").forEach(function (i) { i.checked = false; });
  }
  function toggleSel(id, on) { if (on) SEL[id] = 1; else delete SEL[id]; selbar(); }
  function addPick(card, id) {
    card.dataset.id = id;
    var pick = el("div", "pick");
    pick.onclick = function (e) {
      e.stopPropagation();
      var on = !SEL[id];
      card.classList.toggle("sel", on); toggleSel(id, on);
      pick.setAttribute("aria-pressed", on ? "true" : "false");
    };
    keyable(pick, "button", "Select item");   // F-12: focusable + Enter/Space toggles
    pick.setAttribute("aria-pressed", SEL[id] ? "true" : "false");
    card.appendChild(pick);
    if (SEL[id]) card.classList.add("sel");
  }
  // SEL-based pick for marquee/click selection of grid cards (data-id carries the id).
  function selPick(card, on) {
    var id = card.dataset.id; if (!id) return;
    card.classList.toggle("sel", on); toggleSel(id, on);
  }
  // Rubber-band (drag) selection over a card grid, gated by a "Select" mode toggle
  // placed in `controls`. While the mode is on the page cursor is a crosshair, a
  // plain card click toggles its selection (not the lightbox), and dragging a box
  // selects every card it touches. `pick(cardEl, on)` records the selection.
  function marqueeSelect(controls, root, cardSel, pick, onClear) {
    var on = false, box = null;
    var btn = el("button", "btn", "Select");
    btn.onclick = function () {
      on = !on; document.body.classList.toggle("selecting", on);
      btn.textContent = on ? "Done" : "Select"; btn.classList.toggle("primary", on);
    };
    controls.appendChild(btn);
    var clr = el("button", "btn", "Deselect all");
    clr.onclick = onClear || clearSel;
    controls.appendChild(clr);
    root.addEventListener("mousedown", function (e) {
      if (!on || e.button) return;
      e.preventDefault();
      var sx = e.clientX, sy = e.clientY;
      // Cache each currently-mounted card's viewport rect ONCE (F-7): the grid doesn't
      // move during a drag, so re-reading getBoundingClientRect on every mousemove
      // (a forced layout per card) is pure waste. `pre` records the card's selection
      // state at drag start; `dragOn` tracks cards THIS drag toggled on — so shrinking
      // the box deselects its own overshoot while leaving prior selections intact.
      // With F-4 virtualization only mounted cards have rects; marquee operates over
      // the visible window (acceptable — the rest aren't on screen).
      var rects = Array.prototype.map.call(root.querySelectorAll(cardSel), function (c) {
        var b = c.getBoundingClientRect();
        return { card: c, l: b.left, t: b.top, r: b.right, bo: b.bottom,
                 pre: c.classList.contains("sel"), dragOn: false };
      });
      box = el("div", "marquee"); document.body.appendChild(box);
      function mv(ev) {
        var x = Math.min(sx, ev.clientX), y = Math.min(sy, ev.clientY),
            w = Math.abs(ev.clientX - sx), h = Math.abs(ev.clientY - sy),
            x2 = x + w, y2 = y + h;
        box.style.cssText = "left:" + x + "px;top:" + y + "px;width:" + w + "px;height:" + h + "px";
        rects.forEach(function (rc) {
          var hit = !(rc.r < x || rc.l > x2 || rc.bo < y || rc.t > y2);
          if (hit) { if (!rc.pre && !rc.dragOn) { pick(rc.card, true); rc.dragOn = true; } }
          else if (rc.dragOn) { pick(rc.card, false); rc.dragOn = false; }
        });
      }
      function up() {
        document.removeEventListener("mousemove", mv); document.removeEventListener("mouseup", up);
        if (box) { box.remove(); box = null; }
      }
      document.addEventListener("mousemove", mv); document.addEventListener("mouseup", up);
    });
    root.addEventListener("click", function (e) {   // capture: beat the card's own onclick
      if (!on) return;
      var c = e.target.closest(cardSel);
      if (!c || !root.contains(c)) return;
      e.preventDefault(); e.stopPropagation();
      pick(c, !c.classList.contains("sel"));
    }, true);
  }

  // ── lightbox (F-10) ──
  // A real viewer: keyboard (Escape closes, ←/→ page through the caller's set),
  // click/scroll zoom + drag pan on images, an EXIF/metadata sidebar, and full
  // dialog a11y (role=dialog, aria-modal, focus moves in on open + is trapped +
  // restored to the trigger on close). `openLightbox({list,index,resolve})` drives
  // a gallery; `lightbox(id,kind,actions,meta)` is the single-item wrapper (arrows
  // are no-ops). `resolve(row)` → {id, kind, actions, meta}; `meta` is the photo row
  // (or a {caption,source} legacy object) — whatever fields it carries populate the
  // panel. `kind`: true/"image", "video", "pdf" (inline browser PDF viewer — F-11),
  // "doc" (Download-to-open card — F-11), false/"iframe" (legacy sandboxed frame).
  var LBX = { list: null, index: 0, mode: null, trigger: null, resolve: null,
              go: null, zoomCleanup: null, keyed: false };

  function ensureLB() {
    var lb = document.getElementById("lb");
    if (!lb) {
      lb = el("div", "lightbox"); lb.id = "lb";
      lb.setAttribute("role", "dialog"); lb.setAttribute("aria-modal", "true");
      lb.setAttribute("aria-label", "Media viewer"); lb.tabIndex = -1;
      document.body.appendChild(lb);
      lb.addEventListener("click", function (e) {
        // Backdrop (or the × button) closes; the media/panel/nav controls don't.
        if (e.target === lb || (e.target.classList && e.target.classList.contains("x"))) closeLB();
      });
    }
    if (!LBX.keyed) {   // one document-level key handler for the whole session
      LBX.keyed = true;
      document.addEventListener("keydown", function (e) {
        var box = document.getElementById("lb");
        if (!box || !box.classList.contains("show")) return;
        if (e.key === "Escape") { e.preventDefault(); closeLB(); return; }
        if (e.key === "Tab") { trapFocus(e, box); return; }
        if (LBX.mode === "gallery" && LBX.go) {
          if (e.key === "ArrowRight") { e.preventDefault(); LBX.go(1); }
          else if (e.key === "ArrowLeft") { e.preventDefault(); LBX.go(-1); }
        }
      });
    }
    return lb;
  }

  function closeLB() {
    if (LBX.zoomCleanup) { LBX.zoomCleanup(); LBX.zoomCleanup = null; }
    var lb = document.getElementById("lb");
    if (lb) lb.classList.remove("show");
    document.body.classList.remove("lb-open");
    var t = LBX.trigger;
    LBX.trigger = null; LBX.mode = null; LBX.list = null; LBX.go = null;
    if (t && t.focus) { try { t.focus(); } catch (e) { /* trigger gone */ } }
  }

  // Keep Tab focus inside the open dialog (F-10 / F-12).
  function trapFocus(e, lb) {
    var nodes = lb.querySelectorAll('button, a[href], iframe, video, [tabindex]:not([tabindex="-1"])');
    var f = Array.prototype.filter.call(nodes, function (n) {
      return !n.disabled && (n.offsetWidth || n.offsetHeight || n === document.activeElement);
    });
    if (!f.length) { e.preventDefault(); lb.focus(); return; }
    var first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    else if (f.indexOf(document.activeElement) < 0) { e.preventDefault(); first.focus(); }
  }

  // Find the on-screen grid card for an id (so a verb invoked from the lightbox can
  // still update the card in place while paging a gallery). String-equality on the
  // dataset id — no attribute-selector escaping hazard.
  function findCard(grid, id) {
    if (!grid) return null;
    var cs = grid.querySelectorAll(".card, .qcard");
    for (var i = 0; i < cs.length; i++) if (cs[i].dataset.id === id) return cs[i];
    return null;
  }

  // ── metadata panel ──
  function _coord(v) { var n = Number(v); return isFinite(n) ? n.toFixed(5) : String(v); }
  function placeText(meta) {
    var pd = meta.place_detail;
    if (pd && typeof pd === "object") {
      var parts = [pd.name, pd.admin1, pd.cc].filter(Boolean);
      if (parts.length) return parts.join(", ");
    }
    return meta.place ? prettyPlace(meta.place) : "";
  }
  // [label, escaped-HTML] pairs for whatever the row carries. Everything is
  // estate-derived → esc() at the sink.
  function metaRows(meta) {
    if (!meta) return [];
    var out = [];
    if (meta.ts) out.push(["Date", esc(String(meta.ts).replace("T", " "))]);
    var place = placeText(meta);
    if (place) out.push(["Place", esc(place)]);
    var cam = [meta.camera_make, meta.camera_model].filter(Boolean).join(" ");
    if (cam) out.push(["Camera", esc(cam)]);
    if (meta.width_px && meta.height_px)
      out.push(["Dimensions", esc(num(meta.width_px) + " × " + num(meta.height_px) + " px")]);
    if (meta.gps && meta.gps.lat != null && meta.gps.lon != null) {
      var g = esc(_coord(meta.gps.lat) + ", " + _coord(meta.gps.lon));
      if (meta.gps_altitude_m != null) g += esc(" · " + Math.round(meta.gps_altitude_m) + " m");
      out.push(["GPS", g]);
    }
    if (meta.scene) out.push(["Scene", esc(pretty(meta.scene))]);
    if (meta.caption) out.push(["Caption", esc(meta.caption)]);
    if (meta.albums && meta.albums.length) out.push(["Albums", esc(meta.albums.join(", "))]);
    if (meta.source) out.push(["Source", esc(pretty(meta.source))]);
    // Curation overlay (examiner): starred flag, collection membership, note. All
    // estate/operator-derived → esc() at the sink.
    if (meta.favorite_curation) out.push(["Starred", "★"]);
    if (meta.collections && meta.collections.length)
      out.push(["Collections", esc(meta.collections.join(", "))]);
    if (meta.note) out.push(["Note", esc(meta.note)]);
    return out;
  }
  function metaPanel(meta) {
    var rows = metaRows(meta);
    if (!rows.length) return null;
    var panel = el("aside", "lb-panel"); panel.setAttribute("aria-label", "Details");
    if (meta && meta.name) panel.appendChild(el("h2", "lb-panel-h", esc(meta.name)));
    else panel.appendChild(el("h2", "lb-panel-h", "Details"));
    var dl = el("dl", "lb-meta");
    rows.forEach(function (r) {
      dl.appendChild(el("dt", null, esc(r[0])));
      dl.appendChild(el("dd", null, r[1]));   // r[1] is already escaped HTML
    });
    panel.appendChild(dl);
    var btn = el("button", "lb-info", "Details");
    btn.setAttribute("aria-label", "Toggle details"); btn.setAttribute("aria-expanded", "true");
    btn.onclick = function (e) {
      e.stopPropagation();
      var hidden = panel.classList.toggle("hidden");
      btn.setAttribute("aria-expanded", hidden ? "false" : "true");
    };
    return { el: panel, btn: btn };
  }

  // ── image zoom / pan ── dependency-free. Scroll or click to zoom, drag to pan.
  function attachZoom(img, stage) {
    var scale = 1, tx = 0, ty = 0, dragging = false, sx = 0, sy = 0, moved = false;
    function apply() {
      img.style.transform = "translate(" + tx + "px," + ty + "px) scale(" + scale + ")";
      img.style.cursor = scale > 1 ? "grab" : "zoom-in";
    }
    function onWheel(e) {
      e.preventDefault();
      scale = Math.min(6, Math.max(1, scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)));
      if (scale === 1) { tx = 0; ty = 0; }
      apply();
    }
    function onDown(e) {
      if (e.button) return;
      e.preventDefault(); dragging = true; moved = false; sx = e.clientX; sy = e.clientY;
      if (scale > 1) img.style.cursor = "grabbing";
    }
    function onMove(e) {
      if (!dragging) return;
      var dx = e.clientX - sx, dy = e.clientY - sy;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) moved = true;
      if (scale > 1) { tx += dx; ty += dy; sx = e.clientX; sy = e.clientY; apply(); }
    }
    function onUp() {
      if (!dragging) return;
      dragging = false;
      if (!moved) {   // a plain click toggles zoom (in to ~2.4×, or back to fit)
        if (scale > 1) { scale = 1; tx = 0; ty = 0; } else { scale = 2.4; }
      }
      apply();
    }
    img.addEventListener("wheel", onWheel, { passive: false });
    img.addEventListener("mousedown", onDown);
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    apply();
    return function () {   // cleanup on nav/close
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
  }

  // Inline document icon for the Download-to-open card (Zone-B safe, no CDN).
  function docIcon() {
    return '<svg viewBox="0 0 24 24" width="46" height="46" aria-hidden="true">' +
      '<path d="M6 2h8l4 4v16H6z" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
      '<path d="M14 2v4h4" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>';
  }
  // F-11: office/svg/html/unknown → a clean "Download to open" card (never a broken
  // frame / silent attachment download inside the overlay). Filename + optional size
  // + the OCR preview text the archive already has (when the caller passes it).
  function downloadCard(id, meta) {
    var name = (meta && meta.name) || basename(id);
    var wrap = el("div", "lb-download");
    wrap.appendChild(el("div", "lb-dl-icon", docIcon()));
    wrap.appendChild(el("h2", "lb-dl-name", esc(name)));
    // Reached for two different reasons — a format with no in-browser renderer
    // (.svg/.html/unknown) and a renderable document whose view sidecar is
    // absent — so the wording has to be true of both.
    var sub = "A preview isn't available for this file.";
    if (meta && meta.size) sub += " · " + esc(String(meta.size));
    wrap.appendChild(el("p", "lb-dl-note", sub));
    if (meta && meta.preview) wrap.appendChild(el("div", "lb-dl-preview", esc(meta.preview)));
    var a = el("a", "btn primary", "Download to open");
    a.href = mediaURL(id); a.setAttribute("download", name);
    a.setAttribute("rel", "noopener noreferrer");
    a.onclick = function (e) { e.stopPropagation(); };
    wrap.appendChild(a);
    return wrap;
  }

  // ── inline office-document view (F-11 follow-up) ──
  // The server hands back typed BLOCKS of plain text (never document markup —
  // see wyeast/stages/_doctext.py), and every string lands via textContent, so a
  // hostile estate .docx cannot inject anything into this page. Do NOT switch
  // these to el(tag, cls, html): that third argument is innerHTML.
  function txt(tag, cls, s) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    e.textContent = String(s == null ? "" : s);
    return e;
  }

  function docBlockNode(b) {
    var t = b && b.t;
    if (t === "h") return txt("h" + Math.min(6, Math.max(1, b.level || 2)), "dv-h", b.text);
    if (t === "section") return txt("div", "dv-section", b.text);
    if (t === "pre") return txt("pre", "dv-pre", b.text);
    if (t === "table") {
      var rows = b.rows || [];
      if (!rows.length) return null;
      // Wide sheets scroll inside the table, never widening the lightbox.
      var wrap = el("div", "dv-tablewrap");
      var tbl = document.createElement("table");
      tbl.className = "dv-table";
      rows.forEach(function (r, i) {
        var head = b.header && i === 0;
        var tr = document.createElement("tr");
        (r || []).forEach(function (c) { tr.appendChild(txt(head ? "th" : "td", null, c)); });
        tbl.appendChild(tr);
      });
      wrap.appendChild(tbl);
      return wrap;
    }
    return txt("p", "dv-p", (b && b.text) || "");
  }

  // A "doc" lightbox stage: try the inline view, fall back to the download card.
  // Absence is routine (legacy .doc, unrenderable file, case OCR'd before inline
  // views existed), so a failed fetch is a normal outcome, not an error state.
  function docStage(id, meta) {
    var wrap = el("div", "lb-doc");
    wrap.appendChild(el("p", "notice", "Loading preview…"));
    getJSON("/api/doctext?src=" + encodeURIComponent(id)).then(function (r) {
      var blocks = (r && r.blocks) || [];
      if (!blocks.length) throw new Error("empty");
      wrap.innerHTML = "";
      var bar = el("div", "dv-bar");
      bar.appendChild(txt("span", "dv-name", (meta && meta.name) || r.name || basename(id)));
      var dl = el("a", "btn", "Download");
      dl.href = mediaURL(id);
      dl.setAttribute("download", (meta && meta.name) || r.name || basename(id));
      dl.setAttribute("rel", "noopener noreferrer");
      dl.onclick = function (e) { e.stopPropagation(); };
      bar.appendChild(dl);
      wrap.appendChild(bar);
      var body = el("div", "dv-body");
      blocks.forEach(function (b) {
        var n = docBlockNode(b);
        if (n) body.appendChild(n);
      });
      wrap.appendChild(body);
    }).catch(function () {
      wrap.innerHTML = "";
      wrap.appendChild(downloadCard(id, meta));
    });
    return wrap;
  }

  // Render the current item into #lb. `focusSel` picks what to focus afterwards.
  function showLBItem(focusSel) {
    var lb = document.getElementById("lb"); if (!lb) return;
    if (LBX.zoomCleanup) { LBX.zoomCleanup(); LBX.zoomCleanup = null; }
    var d = (LBX.resolve ? LBX.resolve(LBX.list[LBX.index]) : null) || {};
    var id = d.id, actions = d.actions, meta = d.meta;
    var k = d.kind === true ? "image" : d.kind === false ? "iframe" : (d.kind || "image");
    lb.innerHTML = "";
    var close = el("button", "x", "×"); close.setAttribute("aria-label", "Close"); lb.appendChild(close);
    if (LBX.mode === "gallery") {
      var prev = el("button", "lb-nav lb-prev", "‹"); prev.setAttribute("aria-label", "Previous");
      prev.onclick = function (e) { e.stopPropagation(); LBX.go(-1); };
      var next = el("button", "lb-nav lb-next", "›"); next.setAttribute("aria-label", "Next");
      next.onclick = function (e) { e.stopPropagation(); LBX.go(1); };
      lb.appendChild(prev); lb.appendChild(next);
    }
    var stage = el("div", "lb-stage"); lb.appendChild(stage);
    var node;
    if (k === "video") {
      node = el("video"); node.controls = true; node.src = mediaURL(id);
      node.onerror = function () {   // many estate videos are .wmv/.avi → no in-browser codec
        // G-11 poster-strip fallback: show the video's keyframes so the family can
        // still SEE the moments even when the browser can't decode the stream.
        var fb = el("div", "lb-fallback");
        fb.appendChild(el("p", "notice", "This video can't be played in the browser — "
          + "here are its moments. Use Export to view the full video."));
        var strip = el("div", "stackstrip posterstrip"); fb.appendChild(strip);
        node.replaceWith(fb);
        getJSON("/api/video-frames?id=" + encodeURIComponent(id)).then(function (r) {
          // The endpoint is retention-aware: it returns ONLY frames that still
          // exist on disk (the poster itself is one of them when it survives), so
          // an empty list means nothing remains → show a notice, never a broken thumb.
          var frames = (r && r.frames) || [];
          if (!frames.length) { strip.appendChild(el("p", "notice", "No preview frames remain for this video — use Export to view it.")); return; }
          frames.forEach(function (fr) {
            var cell = el("div", "sthumb");
            var im = el("img"); lazyThumb(im, fr.id); im.alt = "";
            im.addEventListener("click", function () { lightbox(fr.id, "image"); });   // just view the frame
            cell.appendChild(im);
            if (fr.offset != null) cell.appendChild(el("div", "slabel", esc(fmtSecs(fr.offset))));
            strip.appendChild(cell);
          });
        }).catch(function () {
          strip.appendChild(el("p", "notice", "Couldn't load preview frames."));
        });
      };
      stage.appendChild(node);
    } else if (k === "image") {
      node = el("img"); node.src = mediaURL(id);
      node.alt = (meta && meta.caption) || (meta && meta.name) || "";   // G-7 alt-text
      stage.appendChild(node);
      LBX.zoomCleanup = attachZoom(node, stage);   // click/scroll zoom + drag pan
    } else if (k === "pdf") {
      // F-11: the browser's built-in (itself sandboxed) PDF viewer — NOT sandbox=""
      // (which blanks it). Same-origin /media; the page CSP allows frame-src 'self'.
      node = document.createElement("iframe"); node.className = "lb-frame"; node.src = mediaURL(id);
      node.setAttribute("title", (meta && meta.name) || "PDF preview");
      stage.appendChild(node);
    } else if (k === "doc") {
      // Office formats render inline from the text layer the pipeline already
      // extracted; anything with no view falls back to the download card.
      stage.appendChild(docStage(id, meta));
    } else {
      // Legacy fallback (kind false/"iframe"): fully sandboxed frame for anything a
      // caller still routes here. Defence-in-depth alongside the /media sandbox CSP.
      node = document.createElement("iframe"); node.className = "lb-frame";
      node.setAttribute("sandbox", ""); node.src = mediaURL(id);
      stage.appendChild(node);
    }
    if (LBX.mode === "gallery")
      stage.appendChild(el("div", "lb-counter", (LBX.index + 1) + " / " + LBX.list.length));
    var panel = metaPanel(meta);
    if (panel) { lb.appendChild(panel.btn); lb.appendChild(panel.el); }
    if (actions && actions.length) {
      var barEl = el("div", "lbactions");
      actions.forEach(function (a) {
        var b = el("button", "btn" + (a.cls ? " " + a.cls : ""), esc(a.label));
        b.onclick = function (ev) { ev.stopPropagation(); a.onclick(lb); };
        barEl.appendChild(b);
      });
      lb.appendChild(barEl);
    }
    var focusEl = (focusSel && lb.querySelector(focusSel)) || close;
    setTimeout(function () { if (focusEl && focusEl.focus) focusEl.focus(); }, 0);
  }

  // Gallery engine. `list` is the caller's set (opaque rows); `resolve(row)` builds
  // the descriptor for one. Single-item callers pass a 1-list → arrows are no-ops.
  function openLightbox(opts) {
    ensureLB();
    LBX.trigger = document.activeElement;
    LBX.list = (opts.list && opts.list.length) ? opts.list : [opts.item != null ? opts.item : null];
    LBX.resolve = opts.resolve || function () { return opts.item || {}; };
    LBX.index = Math.max(0, Math.min(opts.index || 0, LBX.list.length - 1));
    LBX.mode = LBX.list.length > 1 ? "gallery" : "single";
    LBX.go = function (delta) {
      var n = LBX.list.length;
      LBX.index = ((LBX.index + delta) % n + n) % n;
      showLBItem(delta > 0 ? ".lb-next" : ".lb-prev");
    };
    document.body.classList.add("lb-open");
    document.getElementById("lb").classList.add("show");
    showLBItem();
  }

  // Single-item wrapper — preserves the old lightbox(id, kind, actions, meta) API.
  function lightbox(id, kind, actions, meta) {
    openLightbox({ list: [null], index: 0,
      resolve: function () { return { id: id, kind: kind, actions: actions, meta: meta }; } });
  }

  // ── photo stacks (perceptual dup groups): badge + review strip ──
  // A keeper that heads a surfaced stack gets a "⧉ N" badge; opening it shows
  // every scan-cleared variant ordered by capture time, with full-size view and
  // download for any member. The pipeline's pick is advisory — bursts have no
  // machine-decidable winner, so the stack is the review surface, and members
  // are view/download objects only (they skipped enrichment; no verbs).
  function stackBadge(c, r) {
    var b = el("div", "stackbadge", "⧉ " + r.stack.n);
    b.title = r.stack.n + " similar shot(s) — click to review";
    b.onclick = function (e) { e.stopPropagation(); openStack(r); };
    c.appendChild(b);
  }
  function openStack(r) {
    var st = r.stack; if (!st) return;
    var frames = [{ src: r.id, name: r.name, capture_time: r.ts, keeper: true }]
      .concat((st.members || []).map(function (m) {
        return { src: m.src, name: m.name, capture_time: m.capture_time };
      }));
    frames.sort(function (a, b) {   // capture-time order, undated last, keeper wins ties
      if (a.capture_time == null) return b.capture_time == null ? 0 : 1;
      if (b.capture_time == null) return -1;
      return a.capture_time < b.capture_time ? -1 : a.capture_time > b.capture_time ? 1 : (a.keeper ? -1 : 1);
    });
    var lb = ensureLB();
    // Single-item modal (no ←/→ paging over the gallery); Escape still closes it and
    // focus is restored to the trigger via closeLB.
    LBX.trigger = document.activeElement; LBX.mode = "single"; LBX.list = null; LBX.go = null;
    if (LBX.zoomCleanup) { LBX.zoomCleanup(); LBX.zoomCleanup = null; }
    document.body.classList.add("lb-open");
    lb.innerHTML = '<button class="x" aria-label="Close">×</button>';
    var wrap = el("div", "stackwrap");
    wrap.appendChild(el("div", "stackhead",
      esc(st.n + " similar shot" + (st.n === 1 ? "" : "s") + " · " + pretty(st.kind))));
    var mainImg = el("img", "stackmain");
    wrap.appendChild(mainImg);
    var ctl = el("div", "stackctl");
    var meta = el("div", "stackmeta");
    var dl = el("a", "btn", "Download");
    ctl.appendChild(meta); ctl.appendChild(dl); wrap.appendChild(ctl);
    var strip = el("div", "stackstrip");
    function show(f) {
      mainImg.src = mediaURL(f.src);
      meta.innerHTML = "<strong>" + esc(f.name) + "</strong>" +
        (f.capture_time ? ' <span class="when">' + esc(String(f.capture_time).replace("T", " ")) + "</span>" : "") +
        (f.keeper ? ' <span class="badge">Delivered pick</span>' : "") +
        (st.suggested && f.name === st.suggested && !f.keeper ? ' <span class="badge sug">Suggested</span>' : "");
      dl.href = mediaURL(f.src); dl.setAttribute("download", f.name || "photo");
      strip.querySelectorAll(".sthumb").forEach(function (t, i) {
        t.classList.toggle("active", frames[i] === f);
      });
    }
    frames.forEach(function (f) {
      var t = el("div", "sthumb");
      var im = el("img"); lazyThumb(im, f.src); im.alt = f.name || "";
      t.appendChild(im);
      t.appendChild(el("div", "slabel",
        esc(String(f.capture_time || "").slice(11, 19) || basename(f.name)) +
        (f.keeper ? " ●" : "")));
      t.title = f.name + (f.keeper ? " (delivered pick)" : "");
      t.onclick = function (e) { e.stopPropagation(); show(f); };
      strip.appendChild(t);
    });
    wrap.appendChild(strip);
    lb.appendChild(wrap);
    show(frames.filter(function (f) { return f.keeper; })[0] || frames[0]);
    lb.classList.add("show");
  }

  // Open the lightbox on one grid item, threading the WHOLE virtual set so ←/→ page
  // through it (F-10) even across tiles that aren't mounted. `ctx` = {win, grid};
  // win.getRows() is the live full set (a virtualized grid mutates it via setRows).
  function openCardLightbox(r, ctx) {
    var win = ctx && ctx.win;
    var list = win ? win.getRows() : (ctx && ctx.list) ? ctx.list : [r];
    var grid = ctx && ctx.grid;
    var index = list.indexOf(r); if (index < 0) index = 0;
    openLightbox({ list: list, index: index,
      resolve: function (row) { return galleryDescriptor(row, grid, win); } });
  }
  // Descriptor for one gallery row: the full row is the metadata panel source. `win`
  // lets a discard-from-lightbox drop the row from the virtual set even when its tile
  // isn't currently mounted (findCard would return null in that case).
  function galleryDescriptor(row, grid, win) {
    if (!row) return {};
    var card = findCard(grid, row.id);
    if (row.kind === "video") return { id: row.id, kind: "video", actions: videoActions(row, card, win), meta: row };
    return { id: row.id, kind: "image", actions: photoActions(row, card, win), meta: row };
  }

  // A photo card, shared by photoGrid + mediaGrid: caption is used for alt-text
  // (G-7) and the lightbox metadata; a favorite gets a ★, a source shows a chip.
  function photoCard(r, ctx) {
    var c = el("div", "card");
    var img = el("img"); lazyThumb(img, r.id);             // F-5 lazy thumbnail
    img.alt = r.caption || r.name || "";                 // G-7 alt-text
    img.onclick = function () { openCardLightbox(r, ctx); };
    c.appendChild(img);
    addPick(c, r.id);
    if (r.stack) stackBadge(c, r);
    if (r.favorite) c.appendChild(el("div", "favbadge", "★"));   // G-1 owner favorite
    if (r.favorite_curation) c.appendChild(el("div", "starbadge", "★"));  // curation star (examiner)
    if (r.note) {                                                // curation note indicator
      var nb = el("div", "notebadge", "✎"); nb.title = r.note; c.appendChild(nb);
    }
    var metaBits = [r.scene, prettyPlace(r.place)].filter(Boolean).join(" · ");
    // What the vision model saw, where it looked. It ran on a minority of the
    // document photos and the sentence has never been rendered — so a card that
    // could say "a handwritten note listing…" has been showing IMG_1159.jpeg.
    // Only the images that actually have one get it; the rest are unchanged.
    var desc = r.description
      ? '<span class="carddesc">' + esc(r.description) + "</span>" : "";
    c.appendChild(el("div", "cap", '<span class="nm">' + esc(r.name) + '</span>' + desc +
      '<span class="meta">' + esc(metaBits) + "</span>" +
      (r.source ? '<span class="srcchip">' + esc(pretty(r.source)) + "</span>" : "")));
    return c;
  }

  // ── windowed grid (virtual scroller, F-4) ──
  // Mounts only the tiles near the viewport so DOM node count stays bounded no matter
  // how large the set is. Tiles are absolutely positioned inside a spacer sized to the
  // full set's height; on (rAF-throttled) window scroll/resize the visible row range is
  // recomputed and only that slice (+overscan) is mounted. `makeTile(row,index)` builds
  // one card (with lazyThumb etc.); mounted tiles are keyed by index and evicted (their
  // thumbs unobserved) when they leave the window. Interop:
  //   • pager Load-more → setRows(concatenatedRows) extends the virtual set in place.
  //   • lazyThumb/IntersectionObserver → a mounted tile's <img> registers as usual; an
  //     evicted tile is unobserved so the observer's target set stays bounded.
  //   • lightbox ←/→ → callers thread win.getRows() (the FULL virtual set), never the DOM.
  // Degrades to a plain CSS grid below `plainMax` rows (tiny sets skip the math). Every
  // live controller is registered in GRIDS so render() can destroy stale ones (their
  // window scroll/resize listeners) before rebuilding the page.
  var GRIDS = [];
  function destroyGrids() {
    GRIDS.forEach(function (g) { try { g.destroy(); } catch (e) { /* detached */ } });
    GRIDS = [];
  }
  function windowGrid(container, rows, makeTile, opts) {
    opts = opts || {};
    var TILE_MIN = opts.tileMin || 150, GAP = opts.gap || 12, OVERSCAN = 2;
    var plainMax = opts.plainMax || 60;
    var wrap = el("div", "vgrid");
    container.appendChild(wrap);
    var st = { rows: rows || [], cols: 1, tileW: TILE_MIN, rowH: opts.rowH || 188,
               mounted: {}, measured: false, plain: false, raf: 0, dead: false };

    function unlazy(tile) {
      var imgs = tile.querySelectorAll("img[data-src]");
      for (var i = 0; i < imgs.length; i++) {
        if (_thumbObserver && !imgs[i]._lt_loaded) _thumbObserver.unobserve(imgs[i]);
        reclaimThumbSlot(imgs[i]);
      }
    }
    function clearMounted() {
      Object.keys(st.mounted).forEach(function (k) { unlazy(st.mounted[k]); st.mounted[k].remove(); });
      st.mounted = {};
    }
    function layout() {
      var w = wrap.clientWidth || container.clientWidth || 0;
      st.cols = Math.max(1, Math.floor((w + GAP) / (TILE_MIN + GAP)));  // matches CSS auto-fill
      st.tileW = st.cols > 0 ? (w - (st.cols - 1) * GAP) / st.cols : w;
    }
    // Plain render for tiny sets: reuse the responsive .grid CSS, no windowing math.
    function renderPlain() {
      clearMounted();
      wrap.className = "grid"; wrap.style.height = ""; wrap.innerHTML = "";
      st.rows.forEach(function (r, i) { wrap.appendChild(makeTile(r, i)); });
    }
    function renderWindow() {
      if (st.dead) return;
      layout();
      var n = st.rows.length, stepY = st.rowH + GAP;
      var totalRows = Math.ceil(n / st.cols);
      wrap.style.height = (totalRows > 0 ? totalRows * st.rowH + (totalRows - 1) * GAP : 0) + "px";
      var top = wrap.getBoundingClientRect().top + window.pageYOffset;
      var vh = window.innerHeight || document.documentElement.clientHeight || 0;
      var y0 = window.pageYOffset - top, y1 = y0 + vh;
      var firstRow = Math.max(0, Math.floor(y0 / stepY) - OVERSCAN);
      var lastRow = Math.min(totalRows - 1, Math.floor(y1 / stepY) + OVERSCAN);
      var need = {};
      for (var r = firstRow; r <= lastRow; r++) {
        for (var c = 0; c < st.cols; c++) {
          var idx = r * st.cols + c;
          if (idx >= n) break;
          need[idx] = 1;
          var tile = st.mounted[idx];
          if (!tile) {
            tile = makeTile(st.rows[idx], idx); tile.classList.add("vtile");
            wrap.appendChild(tile); st.mounted[idx] = tile;
          }
          tile.style.left = (c * (st.tileW + GAP)) + "px";
          tile.style.top = (r * stepY) + "px";
          tile.style.width = st.tileW + "px";
        }
      }
      Object.keys(st.mounted).forEach(function (k) {
        if (!need[k]) { unlazy(st.mounted[k]); st.mounted[k].remove(); delete st.mounted[k]; }
      });
      // Measure the real tile height once (captions are clipped → height is constant
      // enough) and re-layout if it differs materially from the estimate.
      if (!st.measured) {
        var keys = Object.keys(st.mounted);
        if (keys.length) {
          var h = st.mounted[keys[0]].offsetHeight;
          st.measured = true;
          if (h && Math.abs(h - st.rowH) > 2) { st.rowH = h; renderWindow(); }
        }
      }
    }
    function schedule() {
      if (st.plain || st.dead || st.raf) return;
      st.raf = window.requestAnimationFrame(function () { st.raf = 0; renderWindow(); });
    }
    function apply() {
      if (st.dead) return;
      st.plain = st.rows.length <= plainMax && !opts.forceVirtual;
      clearMounted();
      if (st.plain) { renderPlain(); }
      else {
        // Full reset: drop any leftover DOM (e.g. untracked plain-mode tiles from a
        // previous, smaller set) before the windowed renderer takes over.
        wrap.className = "vgrid"; wrap.style.height = ""; wrap.innerHTML = ""; st.mounted = {};
        renderWindow();
      }
    }
    function onResize() { st.measured = false; apply(); }
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("resize", onResize);

    var ctrl = {
      el: wrap,
      getRows: function () { return st.rows; },
      setRows: function (newRows) { st.rows = newRows || []; st.measured = false; apply(); },
      removeIds: function (ids) {
        var set = {}; (ids || []).forEach(function (i) { set[i] = 1; });
        st.rows = st.rows.filter(function (r) { return !set[r.id]; });
        st.measured = false; apply();
      },
      refresh: function () { st.measured = false; apply(); },   // re-measure after unhide
      destroy: function () {
        st.dead = true;
        window.removeEventListener("scroll", schedule);
        window.removeEventListener("resize", onResize);
        if (st.raf) window.cancelAnimationFrame(st.raf);
        clearMounted();
      },
    };
    GRIDS.push(ctrl);
    apply();
    return ctrl;
  }

  // ── reusable photo grid (Photos, person detail, place detail) ──
  // Virtualized (F-4): returns a window controller ({el,getRows,setRows,removeIds,…}).
  // `ctx` threads the controller so a card's click reads the CURRENT full row set for
  // the lightbox (win.getRows(), not a captured array) and verbs can removeIds() it.
  function photoGrid(container, rows) {
    var ctx = {};
    var ctrl = windowGrid(container, rows, function (r) { return photoCard(r, ctx); });
    ctx.grid = ctrl.el; ctx.win = ctrl;
    return ctrl;
  }

  // Person/scene facet chips for a video card (G-11). person → filter to that
  // person; scene → filter to that scene. All estate text esc()'d; the chip label
  // is a metadata affordance (both roles).
  function videoChips(v) {
    var persons = v.persons || [], scenes = v.scenes || [];
    if (!persons.length && !scenes.length) return null;
    var wrap = el("div", "vchips");
    persons.forEach(function (p) {
      var chip = el("span", "vchip who", esc(p.name || p.person_id));
      chip.title = "Show videos with " + (p.name || p.person_id);
      chip.addEventListener("click", function (e) {
        e.stopPropagation();
        go({ page: "photos", media: "videos", vperson: p.person_id },
           { label: "Videos of " + (p.display_name || p.name || "this person") });
      });
      wrap.appendChild(chip);
    });
    scenes.forEach(function (s) {
      var chip = el("span", "vchip scene", esc(pretty(s)));
      chip.title = "Show " + pretty(s) + " videos";
      chip.addEventListener("click", function (e) {
        e.stopPropagation();
        go({ page: "photos", media: "videos", vscene: s }, { label: "Videos · " + pretty(s) });
      });
      wrap.appendChild(chip);
    });
    return wrap;
  }

  // A video tile: keyframe poster + ▶, click plays the source video (#3).
  function videoCard(v, ctx) {
    var c = el("div", "card vidcard");
    var img = el("img"); lazyThumb(img, v.poster); img.alt = v.name || "";   // F-5 lazy poster
    img.onclick = function () { openCardLightbox(v, ctx); };
    c.appendChild(img);
    c.appendChild(el("div", "playbadge", "▶"));
    addPick(c, v.id);
    c.appendChild(el("div", "cap", '<span class="nm">' + esc(v.name) + '</span><span class="meta">video</span>'));
    var chips = videoChips(v);       // G-11 person/scene chips
    if (chips) c.appendChild(chips);
    return c;
  }

  // Mixed grid of photo + video items (kind:"video" → video tile, else photo).
  // Virtualized (F-4) — returns the same window controller as photoGrid.
  function mediaGrid(container, items) {
    var ctx = {};
    var ctrl = windowGrid(container, items, function (r) {
      return r.kind === "video" ? videoCard(r, ctx) : photoCard(r, ctx);
    });
    ctx.grid = ctrl.el; ctx.win = ctrl;
    return ctrl;
  }

  function exportCollection(kind, key, label) {
    doVerb("/api/export/collection", { kind: kind, key: key },
      "Exported all of " + (label || key));
  }

  // Drop a discarded row from whatever view owns it: the page's in-place hook
  // (Photos) first, else the virtual grid's set (person/places — works even if the
  // tile isn't mounted), else the bare card element.
  function dropDiscarded(ids, card, win) {
    if (dropItems(ids)) return;
    if (win) { win.removeIds(ids); return; }
    if (card) card.remove();
  }

  // Curation-layer actions (examiner-first): Star / Note / Add to collection. Shared
  // by the photo + video lightbox. All operator text is esc()'d at the metadata sink
  // (metaRows) and ids go through encodeURIComponent in the verb URLs. `row` is the
  // live row so we can flip its overlay keys optimistically after a verb.
  function curationActions(row) {
    if (!EXAMINER) return [];
    var id = row.id;
    var acts = [];
    acts.push({ label: row.favorite_curation ? "★ Unstar" : "☆ Star",
      onclick: function () {
        var on = !row.favorite_curation;
        doVerb("/api/favorite", { id: id, on: on }, on ? "Starred" : "Unstarred")
          .then(function (x) { if (x) row.favorite_curation = on; });
      } });
    acts.push({ label: row.note ? "Edit note" : "Add note",
      onclick: function () {
        var text = prompt("Note for this item:", row.note || "");
        if (text == null) return;
        if (!text.trim()) {
          doVerb("/api/note/clear", { id: id }, "Note cleared")
            .then(function (x) { if (x) row.note = null; });
        } else {
          doVerb("/api/note/set", { id: id, text: text }, "Note saved")
            .then(function (x) { if (x) row.note = text; });
        }
      } });
    acts.push({ label: "Add to collection", onclick: function () { addToCollection([id]); } });
    return acts;
  }

  // Let the examiner pick an existing collection or create a new one, then add
  // `ids` to it. A labeled dropdown + "or type a new name" field in the shared
  // pickmodal (#14) — was a number-to-pick-from-a-menu prompt() dialog, easy to
  // mistype and inconsistent with "Merge into…"'s in-app modal.
  function addToCollection(ids) {
    getJSON("/api/collections").then(function (d) {
      var cols = (d && d.collections) || [];
      var body = el("div");
      body.appendChild(el("p", "pickmodal-note",
        esc("Add " + ids.length + " item(s) to a collection.")));
      var sel = el("select", "pickmodal-sel");
      sel.appendChild(new Option("— New collection —", ""));
      cols.forEach(function (c) { sel.appendChild(new Option(c.title, c.slug)); });   // textContent-safe
      var input = el("input", "pickmodal-sel");
      input.type = "text"; input.placeholder = "New collection name";
      function sync() { input.style.display = sel.value ? "none" : ""; }
      sel.onchange = sync; sync();
      body.appendChild(sel); body.appendChild(input);
      pickmodal("Add to collection", body, {
        onConfirm: function (close) {
          var slug = sel.value, name = input.value.trim();
          if (slug) {
            var picked = cols.filter(function (c) { return c.slug === slug; })[0];
            close();
            doVerb("/api/collection/add", { slug: slug, ids: ids }, "Added to " + (picked ? picked.title : slug));
          } else if (name) {
            close();
            postJSON("/api/collection/create", { title: name }).then(function (res) {
              if (!res.ok) { toast((res.j && res.j.error) || "Couldn't create collection"); return; }
              doVerb("/api/collection/add", { slug: res.j.slug, ids: ids }, "Added to " + name);
            });
          } else {
            toast("Pick a collection or type a new name.");
          }
        },
      });
      try { sel.focus(); } catch (e) { /* no focus */ }
    }).catch(function (e) { toast("Couldn't load collections: " + (e && e.message)); });
  }

  // Per-photo actions shown in the zoom/lightbox (#13).
  function photoActions(r, card, win) {
    var acts = [
      { label: "Export", onclick: function () { doVerb("/api/export", { items: [r.id] }, "Exported " + r.name); } },
      { label: "Discard", cls: "danger", onclick: function (lb) {
        // Single-item discard is immediate (no confirm) — it is reversible from
        // History. Only multi-item/group discards prompt.
        doVerb("/api/banish", { src: r.id }, "Discarded").then(function (x) {
          if (x) { dropDiscarded([r.id], card, win); if (lb) closeLB(); }
        });
      } },
    ];
    // #19: the Correspondence "Scanned letters & documents" grid surfaces images
    // the scene classifier CLIP-tagged as a scanned document/letter — sometimes
    // wrongly (an ordinary photo). Discard would hide it entirely; this instead
    // clears the tag (a decisions overlay, not a scene_index mutation) so it
    // rejoins Photos. Only meaningful on that grid.
    if (EXAMINER && CTX.page === "correspondence") {
      acts.push({ label: "Not a document", onclick: function (lb) {
        doVerb("/api/release/scanned", { id: r.id }, "Released to Photos").then(function (x) {
          if (x) { dropDiscarded([r.id], card, win); if (lb) closeLB(); }
        });
      } });
    }
    // Move verb (Phase 1, person view only): on a person_detail member, re-file this
    // photo under a different person. The item leaves THIS person's grid on success.
    if (EXAMINER && CTX.page === "people" && Q.person) {
      acts.push({ label: "Move to person…", onclick: function (lb) {
        pickPerson("Move this photo to which person?", Q.person, function (pid) {
          doVerb("/api/move", { view: "person", src: r.id, to: pid, from: Q.person }, "Moved to person")
            .then(function (x) {
              if (x) { dropDiscarded([r.id], card, win); if (lb) closeLB(); }
            });
        }); } });
    }
    // Scene-move (Phase 1.5): relabel this gallery photo's scene facet (a relabel
    // WITHIN the gallery, never a cross-section re-file). Photos gallery only.
    if (EXAMINER && CTX.page === "photos") {
      acts.push({ label: "Move to scene…", onclick: function (lb) {
        pickScene("Move this photo to which scene?", r.scene, function (cat) {
          doVerb("/api/move", { view: "scene", src: r.id, to: cat }, "Moved to scene")
            .then(function (x) {
              if (x) { if (lb) closeLB(); render(); }
            });
        }); } });
      // Event-move (Phase 2): re-file this gallery photo into a different event
      // album; the events view's live counts shift on success.
      acts.push({ label: "Move to album…", onclick: function (lb) {
        pickAlbum("Move this photo to which album?", r.event_id, function (aid) {
          doVerb("/api/move", { view: "event", src: r.id, to: aid }, "Moved to album")
            .then(function (x) {
              if (x) { if (lb) closeLB(); render(); }
            });
        }); } });
    }
    curationActions(r).forEach(function (a) { acts.push(a); });
    if (r.stack) acts.push({ label: "Similar shots (" + r.stack.n + ")", onclick: function () { openStack(r); } });
    return acts;
  }

  // Per-video actions (Export, + examiner Discard) shared by the tile and the
  // lightbox so paging through a mixed gallery keeps the verbs on video frames.
  function videoActions(v, card, win) {
    var acts = [{ label: "Export", onclick: function () { doVerb("/api/export", { items: [v.id] }, "Exported " + v.name); } }];
    if (EXAMINER) acts.push({ label: "Discard", cls: "danger", onclick: function (lb) {
      doVerb("/api/banish", { src: v.id }, "Discarded").then(function (x) {
        if (x) { dropDiscarded([v.id], card, win); if (lb) closeLB(); }
      });
    } });
    curationActions(v).forEach(function (a) { acts.push(a); });
    return acts;
  }

  // ── pages ──
  var P = {};

  // ── overview ──────────────────────────────────────────────────────────────
  // The front door is a TOOL, not a poster: search first, then everything the
  // archive holds visible at once, with nothing that matters below the fold.
  //
  // What this replaced, and why: six count tiles rendered at 28px display serif
  // — the loudest thing on the page — each linking to a section that is already
  // in the left rail, permanently, four inches away. The screen spent its most
  // valuable region restating the navigation, and the only section that exercises
  // editorial judgment sat three screens down. Counts are inventory; inventory is
  // what you check on visit forty, not visit one.
  //
  // Every number here comes off /api/overview, /api/places or /api/transparency.
  // Nothing on this screen is derived by guesswork — if a figure is not in a
  // payload it does not get printed.

  // "imessage: Alex Rendon (+15035550178), Brian Okafor (+15035550179), owner"
  //   → "Alex Rendon & Brian Okafor"
  // The ranked-item label is the raw corpus handle: platform prefix, every
  // participant, and their phone numbers. It is the least readable text in the
  // archive and it was being shown, truncated mid-number, in the section that
  // claims to say what matters most. Strip to the people; the platform and the
  // digits tell a grieving family nothing they want.
  function conversationLabel(raw) {
    var s = String(raw || "");
    var colon = s.indexOf(":");
    if (colon > -1 && colon < 24) s = s.slice(colon + 1);          // drop "imessage:" / "whatsapp:"
    var names = s.split(",").map(function (part) {
      return part.replace(/\([^)]*\)/g, "").trim();                 // drop "(+15035551234)"
    }).filter(function (n) { return n && n !== "owner"; });         // "owner" is the deceased
    if (!names.length) return "Conversation";
    if (names.length === 1) return names[0];
    if (names.length === 2) return names[0] + " & " + names[1];
    // Name all three rather than "& 1 more" — eliding one person is longer than
    // saying it and tells the reader strictly less.
    if (names.length === 3) return names[0] + ", " + names[1] + " & " + names[2];
    return names.slice(0, 2).join(", ") + " & " + (names.length - 2) + " more";
  }

  // Human words for the message-category slugs in case_config.json.
  var CONV_CATEGORY = {
    close_personal: "Family and close friends",
    romantic: "Someone they loved",
    logistics: "Day-to-day arrangements",
    work: "Work",
    transactional: "Services and appointments",
    miscellaneous: "Messages"
  };

  // One row in Most significant. `signals` carries the only real counts the
  // ranker exposes (photo_count for scenes and people, chunk_count for
  // conversations) — so a row prints a count when there is one and stays silent
  // when there is not, rather than inventing a plausible number.
  function sigRow(r) {
    var li = el("li", "sigrow");
    var thumb = (r.thumbs && r.thumbs[0]) || r.thumb;
    if (thumb) {
      var im = el("img"); im.loading = "lazy"; im.src = thumbURL(thumb); im.alt = "";
      li.appendChild(im);
    } else {
      li.appendChild(el("div", "sigph", typeIcon(r.type)));
    }
    var sg = r.signals || {}, label, sub;
    if (r.type === "conversation") {
      label = conversationLabel(r.label);
      sub = CONV_CATEGORY[r.category] || "Messages";
    } else if (r.type === "photo_cluster") {
      label = r.label || r.person_id || "Person";
      sub = sg.photo_count ? num(sg.photo_count) + " photographs" : "A face that recurs";
      if (sg.cross_scene) sub += " · often at a " + pretty(sg.cross_scene);   // reads "· often at a wedding"
    } else {
      label = sentenceCase(r.label);
      sub = sg.photo_count ? num(sg.photo_count) + " photographs" : "";
    }
    var body = el("div", "siglab", esc(label));
    if (sub) body.appendChild(el("i", null, esc(sub)));
    li.appendChild(body);

    // Conversations carry no `target` from the ranker, so they cannot deep-link
    // to their own transcript. Rather than render a card that looks clickable and
    // does nothing, send them to Messages by name — an honest "it is in here".
    var target = r.target || (r.type === "conversation" ? { page: "messages" } : null);
    if (target) {
      li.classList.add("clickable");
      li.onclick = function (e) {
        if (e.target.tagName !== "BUTTON") go(target, { label: label });
      };
      keyable(li, "button", label);
    }
    if (EXAMINER && r.key) {
      // Was position:absolute over the label and printed straight through it on
      // any card whose text reached the top-right corner. It is now a real
      // flex child with its own column, revealed on hover or keyboard focus.
      var dm = el("button", "sigdrop", "Remove");
      dm.title = "Remove from Most Significant (keeps the item; ranking only)";
      dm.onclick = function (e) {
        e.stopPropagation();
        doVerb("/api/demote", { key: r.key, label: r.label }, "Removed from Most Significant")
          .then(function (x) { if (x) li.remove(); });
      };
      li.appendChild(dm);
    }
    return li;
  }

  function ovCard(parent, title, moreLabel, moreTarget) {
    var card = el("section", "ovcard");
    var h = el("div", "ovcard-h");
    h.appendChild(el("h2", null, esc(title)));
    if (moreLabel) {
      var a = el("a", null, esc(moreLabel) + " →");
      a.href = urlFor(moreTarget, { label: title });
      h.appendChild(a);
    }
    card.appendChild(h);
    parent.appendChild(card);
    return card;
  }

  P.overview = function (main, d) {
    var c = d.counts || {};
    var g = d.export_gate || {};
    if (g.delivery_blocked) main.appendChild(el("div", "banner crit",
      "<strong>Delivery blocked.</strong> " + esc((g.reasons || []).join("; ")) + " — examiner review only."));

    // ── search, first and largest ──
    var find = el("form", "ovfind");
    var fi = el("input", "ovfind-in");
    fi.type = "search"; fi.name = "q"; fi.autocomplete = "off";
    fi.placeholder = "Search every photo, email, document and recording…";
    fi.setAttribute("aria-label", "Search the archive");
    var fb = el("button", "ovfind-go", "Search"); fb.type = "submit";
    find.appendChild(fi); find.appendChild(fb);
    find.addEventListener("submit", function (e) {
      e.preventDefault();
      var v = fi.value.trim();
      if (v) go({ page: "search", q: v });
    });
    main.appendChild(find);
    main.appendChild(el("p", "ovfind-hint",
      "Search a name, a place, an account number, or a phrase you remember from a letter."));

    var cols = el("div", "ovcols");
    var left = el("div", "ovcol"), right = el("div", "ovcol");
    cols.appendChild(left); cols.appendChild(right);
    main.appendChild(cols);

    // ── what is in the archive ──
    var inv = ovCard(left, "What is in the archive");
    var tbl = el("table", "ovtable");
    var rows = [
      ["Photos & videos", c.photos, c.videos ? num(c.videos) + " of them videos" : "", { page: "photos" }],
      ["Emails", c.emails, "", { page: "emails" }],
      ["Documents", c.documents,
        (d.vital_docs && d.vital_docs.available)
          ? num(d.vital_docs.found_count) + " of " + num(d.vital_docs.total_count) + " vital types found" : "",
        { page: "documents" }],
      ["Recordings", c.audio, "", { page: "recordings" }],
      ["Messages", c.messages, "conversations", { page: "messages" }],
      ["People", c.people, "recognised by face", { page: "people" }],
      ["Places", c.places, "trips", { page: "places" }]
    ];
    rows.forEach(function (r) {
      if (r[1] == null) return;
      var tr = el("tr");
      var td = el("td");
      var a = el("a", "ovsec", esc(r[0]));
      a.href = urlFor(r[3], { label: r[0] });
      td.appendChild(a); tr.appendChild(td);
      tr.appendChild(el("td", "ovwhat", esc(r[2] || "")));
      tr.appendChild(el("td", "ovnum", num(r[1])));
      tbl.appendChild(tr);
    });
    inv.appendChild(tbl);

    // ── most significant ──
    if ((d.ranked_top || []).length) {
      var sig = ovCard(left, "Most significant");
      var ul = el("ul", "siglist");
      d.ranked_top.slice(0, 8).forEach(function (r) { ul.appendChild(sigRow(r)); });
      sig.appendChild(ul);
    }

    // ── vital documents ──
    var vd = d.vital_docs;
    if (vd && vd.available) {
      var vc = ovCard(right, "Vital documents", "Open Documents", { page: "documents" });
      var pct = vd.total_count ? Math.round(vd.found_count / vd.total_count * 100) : 0;
      var tal = el("p", "ovtally");
      tal.innerHTML = "<b>" + num(vd.found_count) + " of " + num(vd.total_count) +
        "</b> key document types found";
      vc.appendChild(tal);
      var meter = el("div", "ovmeter");
      meter.setAttribute("role", "img");
      meter.setAttribute("aria-label", pct + "% of key document types found");
      var fill = el("i"); fill.style.width = pct + "%";     // CSSOM, not style="" (CSP)
      meter.appendChild(fill); vc.appendChild(meter);
      // "14 of 27 found", with a meter filled halfway, reads as "you are halfway
      // done". It is not a measure of done — it says only that SOMETHING turned up
      // for 14 types, not that anyone checked the something was right. Mostly
      // nobody has. One line fixes the impression; the four-number breakdown stays
      // on Documents, where working the queue actually happens. A summary should
      // point at its detail, not restate it.
      //
      // The count comes off /api/guided (4 KB) rather than /api/documents (1.4 MB
      // of document rows) — and it is the same figure the guided-review step and
      // the release gate read, so this card cannot drift away from them.
      if (EXAMINER) {
        var unchecked = el("p", "ovunchecked");
        vc.appendChild(unchecked);
        getJSON("/api/guided").then(function (g) {
          var step = ((g && g.steps) || []).filter(function (x) { return x.key === "vital_docs"; })[0];
          var x = (step && step.extra) || {};
          if (!x.unconfirmed) { unchecked.remove(); return; }   // nothing outstanding → say nothing
          var a = el("a", null, esc("Nobody has checked " + num(x.unconfirmed) +
                                    " of the documents behind them") + " →");
          a.href = urlFor({ page: "review", group: "vital" }, { label: "Vital review" });
          unchecked.appendChild(a);
        }).catch(function () { unchecked.remove(); });
      }
      // NOT "Still missing". These are the types with no CONFIRMED document, and
      // on this case every one of them still has weaker matches nobody has read —
      // so calling them missing states an absence the archive cannot support, and
      // contradicts the estate report, which puts the same types under "not yet
      // established". A reader who takes "missing" at face value concludes there
      // is no death certificate, when the truth is that nobody has finished
      // looking. The count is examiner-only upstream, so a family session gets
      // the honest heading without the review vocabulary under it.
      var missing = (vd.types || []).filter(function (t) { return !t.found; });
      if (missing.length) {
        vc.appendChild(el("p", "ovtally ovtally-sub", "Not yet found"));
        if (EXAMINER && vd.unfound_near_misses) {
          vc.appendChild(el("p", "ovnote",
            "Nothing matched well enough to be a candidate. "
            + num(vd.unfound_near_misses)
            + " weaker matches for these types have not been reviewed, so none of "
            + "them can be called absent yet."));
        }
        var chips = el("div", "ovchips");
        missing.forEach(function (t) { chips.appendChild(el("span", null, esc(t.label))); });
        vc.appendChild(chips);
      }
    }

    // ── on this day ──
    var od = d.on_this_day;
    if (od && (od.years || []).length) {
      var oc = ovCard(right, "On this day",
        "All " + num(od.total_count || 0), { page: "photos", media: "" });
      var oul = el("ul", "siglist");
      var shown = 0;
      (od.years || []).forEach(function (y) {
        (y.photos || []).forEach(function (ph) {
          if (shown >= 3) return;
          shown++;
          var li = el("li", "sigrow clickable");
          var im = el("img"); im.loading = "lazy"; im.src = thumbURL(ph.id); im.alt = "";
          li.appendChild(im);
          // Name it by where it was taken, then by what it is. "IMG_0999" is a
          // camera's filing reference, not a thing a person recognises — it was
          // the fallback and it read as broken next to "Beverly Hills, California".
          var place = prettyPlace(ph.place || "");
          var scene = sentenceCase(ph.scene || "");
          var body = el("div", "siglab", esc(place || scene || "Photograph"));
          body.appendChild(el("i", null,
            esc([fmtDay(ph.ts), (place && scene) ? scene : ""].filter(Boolean).join(" · "))));
          li.appendChild(body);
          li.onclick = function () { go({ open: true, file: ph.id }); };
          keyable(li, "button", "Open photograph");
          oul.appendChild(li);
        });
      });
      oc.appendChild(oul);
    }

    // ── what was set aside ──
    getJSON("/api/transparency").then(function (t) {
      if (!t) return;
      // Was: "11,022 group(s) of near-duplicate photos and 23,816 exact
      // duplicate(s) were set aside". Parenthetical plurals are the tell that
      // nobody read it aloud, and this is the one sentence on the page whose
      // whole job is to be believed. Say why, not only what.
      right.appendChild(el("p", "ovkept",
        esc(num(t.exact_duplicates_removed || 0) + " exact copies and " +
            num(t.near_duplicate_groups || 0) + " sets of near-identical photographs were set " +
            "aside, so you would not scroll past the same picture nine times. Nothing was " +
            "deleted — they are all still here.")));
    }).catch(function () { });
  };

  // "2025-08-26T21:09:53" → "26 August 2025". The archive is read by families,
  // not by machines; ISO timestamps on a keepsake page read as a database dump.
  var MONTHS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"];
  // Scene and category labels arrive lowercase from the classifier
  // ("birthday party"). Fine as a filter value, wrong as a heading.
  function sentenceCase(v) {
    v = pretty(String(v || ""));
    return v ? v.charAt(0).toUpperCase() + v.slice(1) : "";
  }
  function fmtDay(ts) {
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(ts || ""));
    if (!m) return "";
    return String(Number(m[3])) + " " + MONTHS[Number(m[2]) - 1] + " " + m[1];
  }

  // Inline SVG type icons (no CDN — Zone-B safe) for ranked items without a thumb.
  function typeIcon(t) {
    var p = {
      document: '<path d="M6 2h8l4 4v16H6z" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M14 2v4h4" fill="none" stroke="currentColor" stroke-width="1.6"/>',
      email: '<rect x="3" y="5" width="18" height="14" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M4 6l8 6 8-6" fill="none" stroke="currentColor" stroke-width="1.6"/>',
      audio: '<path d="M9 18V7l9-2v11" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="7" cy="18" r="2.2" fill="currentColor"/><circle cx="16" cy="16" r="2.2" fill="currentColor"/>',
      // A conversation with no thumbnail used to fall through to the default
      // circle, which reads as a missing image rather than as "this is a chat".
      conversation: '<path d="M4 5h16v11H9l-5 4z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
      scene: '<rect x="3" y="4" width="18" height="16" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="8.5" cy="9.5" r="1.8" fill="currentColor"/><path d="M5 18l5-5 4 3 3-2 4 4" fill="none" stroke="currentColor" stroke-width="1.6"/>'
    };
    var body = p[t] || '<circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.6"/>';
    return '<svg viewBox="0 0 24 24" width="26" height="26">' + body + "</svg>";
  }

  // ── the Photos & Videos opening ───────────────────────────────────────────
  // The Overview is a tool; this page is a keepsake, and it opens like one — one
  // photograph taken on today's date, full width, with the rest of that day's
  // pictures across the years beneath it. The grid and its filters are unchanged
  // and sit directly below.
  //
  // Only on the UNFILTERED page. Once a visitor has narrowed to an album, a
  // person or a scene they have asked a question, and a large unrelated
  // photograph on top of the answer is an obstacle. `heroSuppressed()` names
  // every param that counts as a question.
  //
  // The hero degrades in one step: today's photographs if the archive has any,
  // otherwise the single most recent photograph in it. Both come out of a real
  // payload; if neither is available the page renders exactly as it did before.
  function heroSuppressed() {
    return !!(Q.event || Q.scene || Q.place || Q.album || Q.media || Q.cat ||
              Q.vperson || Q.vscene || Q.favorite || Q.hidden || Q.date_from ||
              Q.date_to || Q.favorite_curation || Q.collection_curation);
  }

  function photosHero(main, newestRow) {
    if (heroSuppressed()) return;
    var host = el("section", "phero");
    main.appendChild(host);

    function paint(photo, headline, stand, strip) {
      var fig = el("div", "phero-fig");
      var im = el("img"); im.src = mediaURL(photo.id); im.alt = ""; im.loading = "eager";
      fig.appendChild(im);
      fig.appendChild(el("div", "phero-veil"));
      var say = el("div", "phero-say");
      say.appendChild(el("div", "phero-when",
        esc([fmtDay(photo.ts), prettyPlace(photo.place || "")].filter(Boolean).join(" · "))));
      say.appendChild(el("h2", "phero-h", esc(headline)));
      if (stand) say.appendChild(el("p", null, esc(stand)));
      fig.appendChild(say);
      fig.onclick = function () { go({ open: true, file: photo.id }); };
      keyable(fig, "button", "Open this photograph");
      host.appendChild(fig);

      if (strip && strip.length) {
        var row = el("div", "phero-strip");
        strip.forEach(function (ph) {
          var f = el("figure");
          var i2 = el("img"); i2.loading = "lazy"; i2.src = thumbURL(ph.id); i2.alt = "";
          f.appendChild(i2);
          // Just the year here — the day and month are the same for every photo
          // in this strip, which is the entire premise of "on this day".
          f.appendChild(el("figcaption", null,
            esc(fmtDay(ph.ts).replace(/^\d+\s\w+\s/, "") +
                (ph.scene ? " · " + sentenceCase(ph.scene) : ""))));
          f.onclick = function () { go({ open: true, file: ph.id }); };
          keyable(f, "button", "Open photograph");
          row.appendChild(f);
        });
        host.appendChild(row);
      }
    }

    getJSON("/api/overview").then(function (d) {
      var od = (d && d.on_this_day) || {}, years = od.years || [];
      var all = [];
      years.forEach(function (y) { (y.photos || []).forEach(function (ph) { all.push(ph); }); });
      if (all.length) {
        var span = years.length > 1
          ? ("Across " + years.length + " years — " + years[years.length - 1].year + " to " + years[0].year + ".")
          : "";
        paint(all[0],
          all.length === 1
            ? "One photograph was taken on this day."
            : num(all.length) + " photographs were taken on this day.",
          span, all.slice(1, 9));
        return;
      }
      if (newestRow) fallback();
    }).catch(function () { if (newestRow) fallback(); });

    function fallback() {
      paint(newestRow, "The most recent photograph in the archive.", "", []);
    }
  }

  P.photos = function (main, data) {
    // Before head(), deliberately. The filter controls render into the sticky
    // .pagehead, so a hero appended afterwards sits BELOW them — which puts the
    // working controls above the photograph and loses the whole point of the
    // opening. Default sort is "newest", so row 0 of the seed payload is the
    // archive's most recent photograph: the hero's fallback for a date with
    // nothing on it. photosHero() appends its container synchronously and fills
    // it when the fetch resolves, so document order holds.
    photosHero(main, (data && data.rows && data.rows[0]) || null);
    var controls = head(main, "Photos & Videos", "Photos & Videos",
      "Filter, then select to export or discard.");
    // Curation: when a collection filter is active (?collection_curation=<slug>),
    // show a context bar with a back link + Export-this-collection button. The slug
    // is a safe lookup key; the human title is fetched (esc()'d) for the label.
    if (Q.collection_curation) {
      var cbar = el("div", "collbar");
      backLink(cbar, "All collections", { page: "collections" });
      var clabel = el("span", "collname", esc(Q.collection_curation));
      cbar.appendChild(clabel);
      var cexp = el("button", "btn primary", "Export this collection");
      cexp.onclick = function () {
        exportCollection("curation_collection", Q.collection_curation, Q.collection_curation);
      };
      cbar.appendChild(cexp);
      main.appendChild(cbar);
      getJSON("/api/collections").then(function (d) {
        (d.collections || []).forEach(function (c) {
          if (c.slug === Q.collection_curation) clabel.textContent = c.title;
        });
      }).catch(function () {});
    }
    // Pagination model (docs/specs/family-archive-pagination.md, F-3):
    //  • Sort (newest/oldest) + date range are SERVER params — so the oldest tail
    //    and a given date window are reachable without paging through everything.
    //  • Scene / Place / Event / media (photo/video) filter the LOADED pages
    //    client-side (Load-more loads more if a filter's tail isn't loaded yet).
    //  • The 2,000-photo cliff (shown.slice(0,2000)) is gone.
    var loaded = [], total = (data && data.total) || 0, videos = null, shown = [];
    var videoFacets = null;   // G-11: distinct persons/scenes for the whole video set

    var selMedia = el("select");
    [["", "All"], ["photos", "Photos"], ["videos", "Videos"]].forEach(function (o) { selMedia.appendChild(new Option(o[1], o[0])); });
    // G-11 video facet filters (person / scene). Server-side params on /api/videos —
    // hidden until the Videos view is active AND the whole-set facets carry data.
    var vpLabel = el("span", "flabel", "Who"); var selVPerson = el("select");
    var vsLabel = el("span", "flabel", "Video scene"); var selVScene = el("select");
    vpLabel.style.display = selVPerson.style.display = "none";
    vsLabel.style.display = selVScene.style.display = "none";
    var selEvent = el("select"); var evLabel = el("span", "flabel", "Event");
    var selScene = el("select"); var selPlace = el("select");
    var selSort = el("select");
    [["newest", "Newest first"], ["oldest", "Oldest first"]].forEach(function (o) { selSort.appendChild(new Option(o[1], o[0])); });
    var dFrom = el("input"); dFrom.type = "date"; dFrom.title = "From date";
    var dTo = el("input"); dTo.type = "date"; dTo.title = "To date";
    // G-1 owner-gallery controls. Album is a SERVER filter (narrows the full set
    // before paging); Favorites/Hidden are SERVER params too. All three are hidden
    // until the whole-set facets say they have data (source-conditional).
    var albLabel = el("span", "flabel", "Album"); var selAlbum = el("select");
    var favBtn = el("button", "btn chip", "★ Favorites");
    var hidBtn = el("button", "btn chip", "Hidden");   // examiner toggle only
    // Curation-layer Starred chip (examiner): a virtual filter over ?favorite_curation=1.
    // Distinct from the owner-gallery Favorites chip above.
    var starBtn = el("button", "btn chip", "★ Starred");
    albLabel.style.display = selAlbum.style.display = favBtn.style.display
      = hidBtn.style.display = starBtn.style.display = "none";
    if (Q.favorite === "1") favBtn.classList.add("active");
    if (Q.hidden === "1") hidBtn.classList.add("active");
    if (Q.favorite_curation === "1") starBtn.classList.add("active");
    if (Q.media) selMedia.value = Q.media;
    if (Q.sort) selSort.value = Q.sort;
    if (Q.date_from) dFrom.value = Q.date_from;
    if (Q.date_to) dTo.value = Q.date_to;
    var expBtn = el("button", "btn", "Export filtered");
    controls.appendChild(el("span", "flabel", "Show")); controls.appendChild(selMedia);
    controls.appendChild(vpLabel); controls.appendChild(selVPerson);   // G-11 video person filter
    controls.appendChild(vsLabel); controls.appendChild(selVScene);    // G-11 video scene filter
    controls.appendChild(evLabel); controls.appendChild(selEvent);
    controls.appendChild(el("span", "flabel", "Scene")); controls.appendChild(selScene);
    controls.appendChild(el("span", "flabel", "Place")); controls.appendChild(selPlace);
    controls.appendChild(albLabel); controls.appendChild(selAlbum);
    controls.appendChild(el("span", "flabel", "Sort")); controls.appendChild(selSort);
    controls.appendChild(el("span", "flabel", "From")); controls.appendChild(dFrom);
    controls.appendChild(el("span", "flabel", "To")); controls.appendChild(dTo);
    controls.appendChild(favBtn);
    if (EXAMINER) { controls.appendChild(hidBtn); controls.appendChild(starBtn); }
    controls.appendChild(expBtn);

    // Whole-set facets from the /api/photos payload drive the owner-gallery
    // controls' visibility + album options (favorites/albums may live in the
    // un-loaded tail, so this uses the server's full-set summary, not loaded rows).
    function applyFacets(f) {
      f = f || {};
      var favN = f.favorites || 0, hidN = f.hidden || 0, albums = f.albums || [];
      favBtn.style.display = favN ? "" : "none";
      favBtn.textContent = "★ Favorites (" + num(favN) + ")";
      if (albums.length) {
        fillSelect(selAlbum, "All albums", albums);
        if (Q.album) selAlbum.value = Q.album;
        albLabel.style.display = selAlbum.style.display = "";
      } else {
        albLabel.style.display = selAlbum.style.display = "none";
      }
      hidBtn.style.display = (EXAMINER && hidN) ? "" : "none";
      hidBtn.textContent = "Hidden (" + num(hidN) + ")";
      // Curation Starred chip: shown to the examiner when the star filter is active
      // (so it can be turned off even from an empty result) or any starred items exist.
      var starN = f.starred || 0;
      starBtn.style.display = (EXAMINER && (starN || Q.favorite_curation === "1")) ? "" : "none";
      starBtn.textContent = "★ Starred" + (starN ? " (" + num(starN) + ")" : "");
    }

    // G-11: populate the video person/scene selects from the whole-set video facets
    // (the /api/videos payload, same for every filter), then sync their visibility.
    function applyVideoFacets(f) {
      f = f || {};
      var persons = f.persons || [], scenes = f.scenes || [];
      selVPerson.innerHTML = "";
      selVPerson.appendChild(new Option("Anyone", ""));            // text node → safe
      persons.forEach(function (p) { selVPerson.appendChild(new Option(p.name || p.person_id, p.person_id)); });
      if (Q.vperson) selVPerson.value = Q.vperson;
      fillSelect(selVScene, "All scenes", scenes, pretty);
      if (Q.vscene) selVScene.value = Q.vscene;
      syncVideoControls();
    }
    // Video facet controls belong to the Videos view only (persons/scenes don't
    // apply to photos); hidden when the facets carry no data (pre-video_index case).
    function syncVideoControls() {
      var isVid = selMedia.value === "videos", f = videoFacets || {};
      var hasP = (f.persons || []).length, hasS = (f.scenes || []).length;
      vpLabel.style.display = selVPerson.style.display = (isVid && hasP) ? "" : "none";
      vsLabel.style.display = selVScene.style.display = (isVid && hasS) ? "" : "none";
    }
    // Server-side video load: ?person=/?scene= narrow the FULL set before paging,
    // so a filtered tail is reachable. Facets are captured from the first page.
    function loadVideos() {
      var qp = [];
      if (Q.vperson) qp.push("person=" + encodeURIComponent(Q.vperson));
      if (Q.vscene) qp.push("scene=" + encodeURIComponent(Q.vscene));
      loadAllRows("/api/videos" + (qp.length ? "?" + qp.join("&") : ""),
        function (v) { videos = v; rebuildFilters(); draw(); },
        function (first) {   // whole-set facets (stable across filters)
          if (!videoFacets) { videoFacets = (first && first.facets) || {}; applyVideoFacets(videoFacets); }
        });
    }

    var holder = el("div"); main.appendChild(holder);
    // Persistent count-note + empty-notice + a single virtualized grid: filter changes
    // re-window in place (mgrid.setRows) instead of tearing down and rebuilding, so
    // scroll and a late /api/videos merge don't yank the grid (F-8).
    var noteEl = el("p", "count-note"); holder.appendChild(noteEl);
    var emptyEl = el("p", "notice", "No photographs match these filters.");
    emptyEl.style.display = "none"; holder.appendChild(emptyEl);
    var mgrid = mediaGrid(holder, []);
    marqueeSelect(controls, holder, ".card", selPick);  // drag-select photos/videos

    // Move Phase 2: the event dropdown is a SERVER filter keyed on album_id, so its
    // options come from the whole-set /api/events list (not the filtered loaded
    // rows) and its title labels survive when ?event= narrows the gallery.
    var eventTitles = {};
    function loadEventAlbums() {
      getJSON("/api/events").then(function (albs) {
        albs = albs || [];
        selEvent.innerHTML = "";
        selEvent.appendChild(new Option("All events", ""));   // text node → safe
        albs.forEach(function (a) {
          var aid = String(a.album_id);
          eventTitles[aid] = a.title;
          selEvent.appendChild(new Option((a.title || aid) + " · " + num(a.count) + " photos", aid));
        });
        if (Q.event) selEvent.value = Q.event;
        var has = albs.length > 0;
        evLabel.style.display = selEvent.style.display = has ? "" : "none";
      }).catch(function () { /* no albums → control stays hidden */ });
    }

    function rebuildFilters() {
      var scenes = {}, places = {};
      loaded.forEach(function (r) {
        if (r.scene) scenes[r.scene] = 1; if (r.place) places[r.place] = 1;
      });
      (videos || []).forEach(function (v) { if (v.place) places[v.place] = 1; });
      fillSelect(selScene, "All scenes", Object.keys(scenes).sort(), pretty);
      fillSelect(selPlace, "All places", Object.keys(places).sort(), prettyPlace);
      if (Q.scene) selScene.value = Q.scene; if (Q.place) selPlace.value = Q.place;
    }

    function draw() {
      var m = selMedia.value, sc = selScene.value, pl = selPlace.value;
      var pool = loaded.slice();
      if (m !== "photos" && videos) pool = pool.concat(videos);   // include videos for All/Videos
      var oldest = selSort.value === "oldest";
      // TOTAL comparator (F-8): tiebreak equal timestamps on id so ordering is stable
      // (the old comparator returned 0 → unstable, jittered order across re-windows).
      pool.sort(function (a, b) {
        var x = a.ts || "", y = b.ts || "", c = x < y ? -1 : x > y ? 1 : 0;
        if (c === 0) { var ai = a.id || "", bi = b.id || ""; c = ai < bi ? -1 : ai > bi ? 1 : 0; }
        return c * (oldest ? 1 : -1);
      });
      shown = pool.filter(function (r) {
        if (m === "photos" && r.kind === "video") return false;
        if (m === "videos" && r.kind !== "video") return false;
        // The narrowing PHOTO-only server filters (owner album/favorite, event
        // album, curation star/collection) narrow the photo set server-side, but the
        // video pool (/api/videos) honors NONE of them — so without re-applying them
        // here the full unfiltered video set leaks into the grid whenever one is
        // active (the reported "Album shows unexpected results"). Re-apply each
        // client-side over the merged pool: a no-op for the already-narrowed photos,
        // decisive for the videos, which carry none of these fields and so drop out
        // (a video belongs to no owner-album/event-album; future-proof if videos ever
        // become curation-aware — a matching one would then be kept, not blanket-dropped).
        if (Q.album && (r.albums || []).indexOf(Q.album) < 0) return false;
        if (Q.event && r.event_id !== Q.event) return false;
        if (Q.favorite === "1" && !r.favorite) return false;
        if (Q.favorite_curation === "1" && !r.favorite_curation) return false;
        if (Q.collection_curation && (r.collections || []).indexOf(Q.collection_curation) < 0) return false;
        if (sc && r.scene !== sc) return false;                   // videos have no scene → excluded when a scene is chosen
        if (pl && r.place !== pl) return false;
        var day = (r.ts || "").slice(0, 10);                      // client date filter (also covers videos)
        if (dFrom.value && day && day < dFrom.value) return false;
        if (dTo.value && day && day > dTo.value) return false;
        return true;
      });
      // The corpus size for this view: photos-only uses the server's photo total;
      // All/Videos also counts videos (always loaded in full, unlike paginated
      // photos) — without this, "shown" summed loaded-photos + all-videos and
      // called it the total, which never matched any total the APIs report (#16).
      var mediaTotal = m === "videos" ? (videos || []).length
        : m === "photos" ? total
        : total + (videos || []).length;
      var note = num(shown.length) + " of " + num(mediaTotal) + " shown";
      if (loaded.length < total && m !== "videos") {
        note += " · " + num(total - loaded.length) + " photo(s) not yet loaded (Load more below)";
      }
      noteEl.textContent = note;
      emptyEl.style.display = shown.length ? "none" : "";
      mgrid.setRows(shown);   // re-window in place — no teardown, scroll preserved
    }

    var pg = pager("/api/photos", {
      getParams: function () {
        var p = { sort: selSort.value, date_from: dFrom.value, date_to: dTo.value };
        // G-1 server-side filters — narrow the FULL set before the page slice.
        if (Q.favorite === "1") p.favorite = "1";
        if (Q.album) p.album = Q.album;
        // Move Phase 2: event-album filter (album_id) narrows server-side too.
        if (Q.event) p.event = Q.event;
        if (Q.hidden === "1") p.hidden = "1";
        // Curation virtual filters (server-side, narrow the full set before paging).
        if (Q.favorite_curation === "1") p.favorite_curation = "1";
        if (Q.collection_curation) p.collection_curation = Q.collection_curation;
        return p;
      },
      onData: function (d) { applyFacets(d && d.facets); },
      render: function (all, tot) { loaded = all; total = tot; rebuildFilters(); draw(); },
    });
    main.appendChild(pg.footer);

    // Discards update this view in place: drop from the cached sets (so filter
    // changes don't resurrect them), then re-window via draw() (no full teardown,
    // scroll preserved) — no manual DOM surgery, the virtual grid handles it.
    VIEW.removeItems = function (ids) {
      var set = {}; ids.forEach(function (i) { set[i] = 1; });
      loaded = loaded.filter(function (r) { return !set[r.id]; });
      if (videos) videos = videos.filter(function (v) { return !set[v.id]; });
      total = Math.max(0, total - ids.length);
      draw();
      pg.draw();
    };

    // Scene / Place are independent ways to slice the LOADED set client-side —
    // picking one resets the other so the view always shows that slice.
    function pickOnly(active, key) {
      [selScene, selPlace].forEach(function (s) { if (s !== active) s.value = ""; });
      var upd = { scene: "", place: "" };
      upd[key] = active.value;   // "" (All) clears it
      setQ(upd);
      draw();
    }
    selScene.onchange = function () { pickOnly(selScene, "scene"); };
    selPlace.onchange = function () { pickOnly(selPlace, "place"); };
    // Event is a SERVER filter (album_id) → refetch page 1 narrowed, and reset the
    // client scene/place slices so the view shows just that album.
    selEvent.onchange = function () {
      selScene.value = ""; selPlace.value = "";
      setQ({ event: selEvent.value, scene: "", place: "" });
      pg.load(true);
    };
    selMedia.onchange = function () { setQ({ media: selMedia.value }); syncVideoControls(); draw(); };
    // G-11: video person/scene are SERVER filters → reload the video pool narrowed.
    selVPerson.onchange = function () { setQ({ vperson: selVPerson.value }); loadVideos(); };
    selVScene.onchange = function () { setQ({ vscene: selVScene.value }); loadVideos(); };
    // Sort + date + owner-gallery filters are SERVER params → refetch page 1.
    selSort.onchange = function () { setQ({ sort: selSort.value }); pg.load(true); };
    function onDate() { setQ({ date_from: dFrom.value, date_to: dTo.value }); pg.load(true); }
    dFrom.onchange = onDate; dTo.onchange = onDate;
    selAlbum.onchange = function () { setQ({ album: selAlbum.value }); pg.load(true); };
    favBtn.onclick = function () {
      var on = Q.favorite === "1";
      setQ({ favorite: on ? "" : "1" }); favBtn.classList.toggle("active", !on); pg.load(true);
    };
    hidBtn.onclick = function () {   // examiner: reveal the owner's hidden photos
      var on = Q.hidden === "1";
      setQ({ hidden: on ? "" : "1" }); hidBtn.classList.toggle("active", !on); pg.load(true);
    };
    starBtn.onclick = function () {   // curation: filter to starred items
      var on = Q.favorite_curation === "1";
      setQ({ favorite_curation: on ? "" : "1" }); starBtn.classList.toggle("active", !on); pg.load(true);
    };
    expBtn.onclick = function () {
      var ev = selEvent.value, sc = selScene.value, pl = selPlace.value;
      if (ev && !sc && !pl) return exportCollection("event", ev, eventTitles[ev] || ev);
      if (sc && !pl) return exportCollection("scene", sc, pretty(sc));
      if (pl && !sc) return exportCollection("place", pl, pretty(pl));
      doVerb("/api/export", { items: shown.map(function (r) { return r.id; }) },
        "Exported " + shown.length + " item(s)");
    };

    // Seed from the router's page-1 payload; if a non-default server filter is
    // active (deep link: sort/date/favorite/album/hidden), refetch page 1 with
    // those server params instead (the router's seed was fetched unfiltered).
    if (selSort.value === "oldest" || dFrom.value || dTo.value
        || Q.favorite === "1" || Q.album || Q.hidden === "1" || Q.event
        || Q.favorite_curation === "1" || Q.collection_curation) pg.load(true);
    else pg.seed(data);
    loadVideos();   // lazy video pool (+ G-11 facets, honoring any ?vperson/?vscene deep link)
    loadEventAlbums();   // Move Phase 2: whole-set album list for the Event server filter
  };

  // Events (Move Phase 2): the event-album view — one card per configured event
  // album with its LIVE (placement-aware) member count, a few thumbs, and place /
  // date range. A card opens the photos gallery filtered ?event=<album_id>. Both
  // roles (album grouping is non-sensitive). Titles/places are estate text → esc()
  // at every sink; album_ids ride go()'s query (encodeURIComponent) and JSON.
  P.events = function (main, rows) {
    head(main, "Events", "Events",
      "Trips and occasions, grouped into albums. Open one to see its photos.");
    rows = rows || [];
    if (!rows.length) {
      main.appendChild(el("p", "notice", "No event albums for this case."));
      return;
    }
    var grid = el("div", "collgrid"); main.appendChild(grid);
    rows.forEach(function (a) {
      var card = el("div", "collcard");
      // Clickable title: a real anchor to the same album gallery the "Open album"
      // button reaches (/photos?event=<album_id>), so it right-clicks / opens in a
      // new tab and is keyboard-focusable natively. Title is estate text → esc();
      // album_id rides the query via encodeURIComponent.
      var h = el("h2", "collcard-h");
      var titleLink = el("a", "collcard-hlink", esc(a.title || "Untitled album"));
      titleLink.href = urlFor({ page: "photos", event: a.album_id },
                              { label: a.title || "Untitled album" });
      h.appendChild(titleLink);
      card.appendChild(h);
      var meta = [a.place, a.date_range].filter(Boolean).join(" · ");
      if (meta) card.appendChild(el("p", "collcard-sub", esc(meta)));
      var thumbs = el("div", "faces");   // reuse the People card's small face strip
      (a.sample_ids || []).slice(0, 4).forEach(function (id) {
        var im = el("img"); lazyThumb(im, id); im.alt = ""; thumbs.appendChild(im);
      });
      if ((a.sample_ids || []).length) card.appendChild(thumbs);
      card.appendChild(el("p", "collcard-n", num(a.count) + " photo(s)"));
      var open = el("button", "btn", "Open album");
      open.onclick = function () {
        go({ page: "photos", event: String(a.album_id) }, { label: a.title || "Untitled album" });
      };
      card.appendChild(open);
      grid.appendChild(card);
    });
  };

  // Curation: the examiner's named collections index (examiner-only route). Each
  // collection opens as a photos grid filtered ?collection_curation=<slug>; a
  // Favorites virtual collection sits at the top. All titles are operator free text →
  // esc() at every sink; slugs go through encodeURIComponent via go()/exportCollection.
  P.collections = function (main, data) {
    head(main, "Collections", "Collections",
      "Hand-picked groups you gathered. Deleting a collection keeps its items.");
    var favN = (data && data.favorites_count) || 0;
    var grid = el("div", "collgrid"); main.appendChild(grid);
    // Favorites pseudo-collection card.
    var favCard = el("div", "collcard fav");
    favCard.appendChild(el("h2", "collcard-h", "★ Favorites"));
    favCard.appendChild(el("p", "collcard-n", num(favN) + " starred item(s)"));
    var favTools = el("div", "collcard-tools");
    var favOpen = el("button", "btn", "Open");
    favOpen.onclick = function () { go({ page: "photos", favorite_curation: "1" }, { label: "Favorites" }); };
    var favExp = el("button", "btn", "Export Favorites");
    favExp.onclick = function () { exportCollection("favorites", "", "Favorites"); };
    favTools.appendChild(favOpen); favTools.appendChild(favExp);
    favCard.appendChild(favTools);
    grid.appendChild(favCard);

    var cols = (data && data.collections) || [];
    if (!cols.length) {
      main.appendChild(el("p", "notice",
        "No collections yet. Open a photo, then “Add to collection”."));
      return;
    }
    cols.forEach(function (c) {
      var card = el("div", "collcard");
      card.appendChild(el("h2", "collcard-h", esc(c.title)));
      card.appendChild(el("p", "collcard-n", num(c.count) + " item(s)"));
      var tools = el("div", "collcard-tools");
      var open = el("button", "btn", "Open");
      open.onclick = function () {
        go({ page: "photos", collection_curation: c.slug }, { label: c.title || c.slug });
      };
      tools.appendChild(open);
      var exp = el("button", "btn", "Export");
      exp.onclick = function () { exportCollection("curation_collection", c.slug, c.title); };
      tools.appendChild(exp);
      var ren = el("button", "btn", "Rename");
      ren.onclick = function () {
        textPrompt("New name for this collection:", c.title, function (t) {
          if (!t.trim()) return;
          doVerb("/api/collection/rename", { slug: c.slug, title: t.trim() }, "Renamed")
            .then(function (x) { if (x) render(); });
        });
      };
      tools.appendChild(ren);
      var del = el("button", "btn danger", "Delete");
      del.onclick = function () {
        if (!confirm("Delete “" + c.title + "”? The items are kept. Reversible from History.")) return;
        doVerb("/api/collection/delete", { slug: c.slug }, "Collection deleted")
          .then(function (x) { if (x) render(); });
      };
      tools.appendChild(del);
      card.appendChild(tools);
      grid.appendChild(card);
    });
  };

  P.people = function (main, rows) {
    // Drill into one person when ?person=ID is present (#1). Render the person's
    // ACTUAL cluster members directly (not via the frame/doc-excluded gallery, which
    // empties out video-only people). Video appearances play their source video.
    if (Q.person) {
      // Carry the roster filter (?people=) back so a drill-in → merge/discard/back
      // returns to the SAME context (e.g. still "Review needed"), not the full list.
      backLink(main, "All people", { page: "people", people: Q.people });
      var holder = el("div"); main.appendChild(holder);
      getJSON("/api/person?id=" + encodeURIComponent(Q.person)).then(function (d) {
        var ctrls = head(holder, "People", d.name || Q.person,
          d.photo_n + " photograph(s)" + (d.video_n ? " · " + d.video_n + " video appearance(s)" : "") + ".");
        var exp = el("button", "btn", "Export all of this person");
        exp.onclick = function () { exportCollection("person", Q.person, d.name || Q.person); };
        holder.appendChild(exp);
        if (EXAMINER) {
          var nm = el("button", "btn", d.named ? "Rename person" : "Name this person");
          nm.onclick = function () {
            textPrompt("Name for this person:", d.named ? d.name : "", function (name) {
              doVerb("/api/rename/person", { person_id: Q.person, new_name: name },
                "Renamed to " + (name || Q.person)).then(function (x) { if (x) location.reload(); });
            });
          };
          holder.appendChild(nm);
          var rm = el("button", "btn danger", "Remove person");  // #6
          rm.onclick = function () {
            if (!confirm("Remove this person grouping? The photographs are kept. Reversible from History.")) return;
            doVerb("/api/remove/person", { person_id: Q.person }, "Removed " + (d.name || Q.person))
              .then(function (x) { if (x) go({ page: "people", people: Q.people }); });
          };
          holder.appendChild(rm);
          // G-15: fold THIS person into another (this becomes the loser). The target
          // person absorbs these photos; this cluster disappears from People.
          var mg = el("button", "btn", "Merge into…");
          mg.title = "Merge this person into another (the photographs join that person)";
          mg.onclick = function () {
            pickPerson("Merge " + (d.name || Q.person) + " into which person?", Q.person, function (winner) {
              doVerb("/api/merge/persons", { winner_pid: winner, loser_pid: Q.person },
                "Merged " + (d.name || Q.person)).then(function (x) {
                  // Return to the (filtered) People LIST to keep reviewing — not into
                  // the winner's detail/album, which drops the review context.
                  if (x) go({ page: "people", people: Q.people });
                });
            });
          };
          holder.appendChild(mg);
        }
        var photos = (d.members || []).filter(function (m) { return m.kind === "photo"; });
        var videos = (d.members || []).filter(function (m) { return m.kind === "video"; });
        if (photos.length) {
          holder.appendChild(el("h2", null, "Photos (" + photos.length + ")"));
          var pgrid = el("div"); holder.appendChild(pgrid);
          photoGrid(pgrid, photos);
          if (EXAMINER) marqueeSelect(ctrls, pgrid, ".card", selPick);  // drag-select
        }
        if (videos.length) {
          holder.appendChild(el("h2", null, "Video appearances (" + videos.length + ")"));
          var vg = el("div", "grid");
          videos.forEach(function (m) {
            var c = el("div", "card vidcard");
            var img = el("img"); lazyThumb(img, m.id); img.alt = m.video_name || "";
            img.onclick = function () {
              lightbox(m.video_src, "video", [{ label: "Export video", onclick: function () {
                doVerb("/api/export", { items: [m.video_src] }, "Exported " + m.video_name); } }]);
            };
            c.appendChild(img);
            c.appendChild(el("div", "playbadge", "▶"));
            c.appendChild(el("div", "cap", '<span class="nm">' + esc(m.video_name || "video") + "</span>"));
            vg.appendChild(c);
          });
          holder.appendChild(vg);
        }
        if (!photos.length && !videos.length)
          holder.appendChild(el("p", "notice", "No delivered media for this person."));
      }).catch(function (e) { holder.appendChild(el("p", "notice", "Couldn't load: " + esc(e.message))); });
      return;
    }
    var pplControls = head(main, "People", "The family",
      rows.length + " people recognized across the photographs.");
    // Cleanup item 1: filter the roster — All / Renamed (a real name set) / Review
    // needed (still an unnamed Person_NN cluster an examiner should name). Persisted
    // in ?people= for reload/share.
    var pplNote = el("span", "count-note");
    var selPeople = el("select");
    [["", "All"], ["named", "Renamed"], ["review", "Review needed"]].forEach(
      function (o) { selPeople.appendChild(new Option(o[1], o[0])); });
    if (Q.people) selPeople.value = Q.people;
    pplControls.appendChild(el("span", "flabel", "Show"));
    pplControls.appendChild(selPeople);
    pplControls.appendChild(pplNote);
    var pplGrid = el("div"); main.appendChild(pplGrid);
    // #22: a per-card Remove/Merge already drops the card from the grid in
    // place (no full re-fetch/rebuild) — but neither the page header ("N
    // people recognized…") nor the "N shown" note read from `rows` again
    // until the next full navigation, so both kept reporting a stale total.
    // Splice the row out of the same `rows` array drawPeople() filters and
    // recompute both texts from it — no grid rebuild, just the two counts.
    function removeRow(r) {
      var ix = rows.indexOf(r);
      if (ix >= 0) rows.splice(ix, 1);
      var leadEl = main.querySelector(".pagehead-title .lead");
      if (leadEl) leadEl.textContent = rows.length + " people recognized across the photographs.";
      var mode = selPeople.value;
      var shown = rows.filter(function (x) {
        if (mode === "named") return x.named;
        if (mode === "review") return !x.named;
        return true;
      }).length;
      pplNote.textContent = num(shown) + " shown";
    }
    function personCard(r) {
      var p = el("div", "person clickable");
      var faces = el("div", "faces");
      (r.sample_ids || []).slice(0, 4).forEach(function (id) { var im = el("img"); lazyThumb(im, id); im.alt = r.name ? ("Photo of " + r.name) : ""; faces.appendChild(im); });
      if (!faces.children.length) faces.appendChild(el("div", "ph", ""));
      p.appendChild(faces);
      var body = el("div", "body");
      body.innerHTML = "<h3>" + esc(r.name) + "</h3><span class='badge'>" +
        num(r.photo_count) + " photos" + (r.video_count ? " · " + num(r.video_count) + " videos" : "") +
        "</span>" + (r.summary ? "<p>" + esc(r.summary) + "</p>" : "");
      p.appendChild(body);
      p.onclick = function (e) {
        if (e.target.tagName !== "BUTTON")
          go({ page: "people", person: r.person_id, people: selPeople.value },
             { label: r.display_name || r.name || "Person" });
      };
      keyable(p, "button", "View " + (r.name || "person"));
      if (EXAMINER) {
        var btn = el("button", "btn", r.named ? "Rename" : "Name this person");
        btn.onclick = function (e) {
          e.stopPropagation();
          textPrompt("Name for this person:", r.named ? r.name : "", function (name) {
            doVerb("/api/rename/person", { person_id: r.person_id, new_name: name }, "Renamed to " + (name || r.person_id)).then(function (x) {
              if (!x) return;
              // Update the card in place — no full people re-fetch/rebuild.
              r.named = !!name; r.name = name || r.person_id;
              var h = body.querySelector("h3"); if (h) h.textContent = r.name;
              btn.textContent = r.named ? "Rename" : "Name this person";
              if (selPeople.value) drawPeople();   // it changed buckets under an active filter
            });
          });
        };
        p.appendChild(btn);
        var rm = el("button", "btn danger", "Remove person");  // #6 dissolve grouping
        rm.title = "Remove this grouping from People (photos are kept)";
        rm.onclick = function (e) {
          e.stopPropagation();
          if (!confirm("Remove this person grouping? The photographs are kept (only the person folder is removed). Reversible from History.")) return;
          doVerb("/api/remove/person", { person_id: r.person_id }, "Removed " + r.name).then(function (x) { if (x) { p.remove(); removeRow(r); } });
        };
        p.appendChild(rm);
        // G-15: merge this card's person INTO another (this becomes the loser).
        var mg = el("button", "btn", "Merge into…");
        mg.title = "Merge this person into another (the photographs join that person)";
        mg.onclick = function (e) {
          e.stopPropagation();
          pickPerson("Merge " + r.name + " into which person?", r.person_id, function (winner) {
            doVerb("/api/merge/persons", { winner_pid: winner, loser_pid: r.person_id },
              "Merged " + r.name).then(function (x) { if (x) { p.remove(); removeRow(r); } });
          });
        };
        p.appendChild(mg);
      }
      return p;
    }
    function drawPeople() {
      var mode = selPeople.value;
      var items = rows.filter(function (r) {
        if (mode === "named") return r.named;
        if (mode === "review") return !r.named;
        return true;
      });
      pplGrid.innerHTML = "";
      pplNote.textContent = num(items.length) + " shown";
      if (!items.length) {
        pplGrid.appendChild(el("p", "notice",
          mode === "review" ? "No people need review — everyone is named."
          : mode === "named" ? "No one has been renamed yet." : "No people in this case."));
        return;
      }
      items.forEach(function (r) { pplGrid.appendChild(personCard(r)); });
    }
    selPeople.onchange = function () { setQ({ people: selPeople.value }); drawPeople(); };
    drawPeople();
  };


  // G-5: Timeline — chapter bands (collapsible) → event rows → capped photo strips.
  // Bands render collapsed; each band's events/photos DOM is built lazily on first
  // expand (a 184-chapter case stays cheap). Clicking a photo threads the whole
  // chapter's rendered strip into the lightbox for ←/→.
  function buildTimelineBand(body, ch) {
    var chapterPhotos = [];   // every rendered photo in the band → lightbox ←/→ set
    (ch.events || []).forEach(function (ev) {
      (ev.photos || []).forEach(function (p) { chapterPhotos.push(p); });
    });
    var ctx = { list: chapterPhotos };
    (ch.events || []).forEach(function (ev) {
      var row = el("div", "tl-event");
      var span = ev.date_from === ev.date_to ? ev.date_from : (ev.date_from + " – " + ev.date_to);
      row.appendChild(el("div", "tl-eventhead",
        esc(span) + " · " + num(ev.count) + " photo" + (ev.count === 1 ? "" : "s")));
      var strip = el("div", "tl-strip");
      (ev.photos || []).forEach(function (p) { strip.appendChild(photoCard(p, ctx)); });
      var extra = ev.count - (ev.photos || []).length;
      if (extra > 0) strip.appendChild(el("div", "tl-more", "+" + num(extra) + " more"));
      row.appendChild(strip);
      body.appendChild(row);
    });
  }

  P.timeline = function (main, d) {
    var chapters = d.chapters || [];
    var undated = (d.undated && d.undated.count) || 0;
    head(main, "Timeline", "Timeline",
      num(d.chapter_count || chapters.length) + " chapter" + ((d.chapter_count || chapters.length) === 1 ? "" : "s") +
      " · " + num(d.event_count || 0) + " moments" + (undated ? " · " + num(undated) + " undated" : "."));
    if (!chapters.length) {
      main.appendChild(el("p", "notice", "No dated photos to place on a timeline."));
      return;
    }
    var wrap = el("div", "timeline");
    chapters.forEach(function (ch) {
      var band = el("section", "tl-band");
      var hdr = el("button", "tl-bandhead"); hdr.type = "button";
      hdr.setAttribute("aria-expanded", "false");
      var range = ch.date_from === ch.date_to ? ch.date_from : (ch.date_from + " – " + ch.date_to);
      hdr.innerHTML = '<span class="tl-caret" aria-hidden="true">▸</span>' +
        '<span class="tl-title">' + esc(ch.label ? prettyPlace(ch.label) : ch.chapter) + "</span>" +
        '<span class="tl-range">' + esc(range) + "</span>" +
        '<span class="tl-count">' + num(ch.count) + " photo" + (ch.count === 1 ? "" : "s") + "</span>";
      var body = el("div", "tl-body"); body.style.display = "none";
      var built = false;
      hdr.onclick = function () {
        var open = body.style.display === "none";
        body.style.display = open ? "" : "none";
        hdr.classList.toggle("open", open);
        hdr.setAttribute("aria-expanded", open ? "true" : "false");
        if (open && !built) { built = true; buildTimelineBand(body, ch); }
      };
      band.appendChild(hdr); band.appendChild(body);
      wrap.appendChild(band);
    });
    main.appendChild(wrap);
    if (undated) {
      var shelf = el("div", "tl-undated");
      shelf.appendChild(el("span", "tl-undated-n", num(undated)));
      shelf.appendChild(el("span", null,
        " photo" + (undated === 1 ? "" : "s") + " without a date — not placed on the timeline."));
      main.appendChild(shelf);
    }
  };

  // Shared canvas map (F-6): plots the given AGGREGATE markers (trip or venue
  // centroids, never 30k raw points) on a fresh Leaflet map inside `mapEl`. Each
  // marker carries {lat,lon,key,n,target?} — a click go()s to the target (drill-in).
  function placesMap(mapEl, markerData) {
    var pts = (markerData || []).filter(function (mk) { return mk.lat != null && mk.lon != null; });
    if (!pts.length) { mapEl.outerHTML = '<p class="notice">No GPS data in this case.</p>'; return; }
    try {
      var map = L.map(mapEl, { preferCanvas: true });   // canvas renderer (F-6)
      if (typeof addBasemap === "function") addBasemap(map);
      var ms = pts.map(function (mk) {
        var radius = Math.min(16, 5 + Math.sqrt(mk.n || 1));   // scale by photo count
        var m = L.circleMarker([mk.lat, mk.lon], { radius: radius, color: "#9c4f3a", fillColor: "#c9a964", fillOpacity: .85, weight: 1 })
          .bindTooltip(esc(prettyPlace(mk.key || "location") + " · " + num(mk.n) + " photo" + (mk.n === 1 ? "" : "s")));
        m.on("click", function () {  // open the location's gallery, not one photo (#C)
          if (mk.target) go(mk.target);
          else if (mk.id) lightbox(mk.id, true);
        });
        return m;
      });
      var grp = L.featureGroup(ms).addTo(map); map.fitBounds(grp.getBounds(), { padding: [30, 30] });
      setTimeout(function () { map.invalidateSize(); }, 60);  // pane has a fixed size now
    } catch (e) { mapEl.innerHTML = "Map error: " + esc(e.message); }
  }

  // One grouping list (trips OR venues): a table of {name, count, Export}. Clicking a
  // row drills into that location's photos via the supplied target/export handlers.
  function placesList(listPane, heading, items, opts) {
    if (!items.length) return;
    listPane.appendChild(el("h2", null, heading));
    var tbl = el("table"); tbl.innerHTML = "<tr><th>Location</th><th>Photos</th><th></th></tr>";
    items.forEach(function (it) {
      var tr = el("tr", "clickable");
      tr.innerHTML = "<td>" + esc(prettyPlace(it.name)) + "</td><td>" + num(it.count) + "</td><td></td>";
      tr.onclick = function (e) { if (e.target.tagName !== "BUTTON") go(opts.target(it)); };
      keyable(tr, null);   // F-12: keep row semantics, add focus + Enter/Space
      var ex = el("button", "btn small", "Export");
      ex.onclick = function (e) { e.stopPropagation(); opts.exportItem(it); };
      tr.lastChild.appendChild(ex);
      tbl.appendChild(tr);
    });
    listPane.appendChild(tbl);
  }

  P.places = function (main, d) {
    var pts = d.points || [], trips = d.trips || [], venues = d.venues || [];
    // G-10 venue drill-in: a filtered grid of the venue's member photos, resolved by
    // the venue's member_ids (the trip drill-in below filters by place name instead).
    if (Q.venue) {
      var v = venues.filter(function (x) { return x.venue_id === Q.venue; })[0];
      backLink(main, "All places", { page: "places", tab: "venues" });
      setCrumb(v ? prettyPlace(v.name) : "Place");
      head(main, "Places", v ? prettyPlace(v.name) : "Place", "");
      if (!v) { main.appendChild(el("p", "notice", "That place is no longer available.")); return; }
      var vexp = el("button", "btn", "Export all from here");
      vexp.onclick = function () {
        doVerb("/api/export", { items: v.member_ids || [] }, "Exported photos from " + prettyPlace(v.name));
      };
      main.appendChild(vexp);
      var vids = {}; (v.member_ids || []).forEach(function (id) { vids[id] = 1; });
      var vholder = el("div"); main.appendChild(vholder);
      loadAllRows("/api/photos", function (photos) {
        photoGrid(vholder, photos.filter(function (p) { return vids[p.id]; }));
      });
      return;
    }
    // Drill into one location's photos when ?place=NAME is present (#6).
    if (Q.place) {
      backLink(main, "All places", { page: "places" });
      setCrumb(prettyPlace(Q.place));
      head(main, "Places", prettyPlace(Q.place), "");
      var exp = el("button", "btn", "Export all from here");
      exp.onclick = function () { exportCollection("place", Q.place, prettyPlace(Q.place)); };
      main.appendChild(exp);
      var ids = {}; pts.forEach(function (p) { if ((p.trip === Q.place || p.place === Q.place) && p.id) ids[p.id] = 1; });
      var holder = el("div"); main.appendChild(holder);
      // /api/photos is paginated now — page through the whole set to filter to
      // this location's ids (the places drill-in needs the complete set).
      loadAllRows("/api/photos", function (photos) {
        photoGrid(holder, photos.filter(function (p) { return ids[p.id]; }));
      });
      return;
    }
    // Two grouping tabs (G-10): Trips (multi-day journeys) | Places (everyday venue
    // clusters — the house/school/park). Both reuse the same map + list machinery.
    var tab = Q.tab === "venues" ? "venues" : "trips";
    head(main, "Places", "Places",
      pts.length + " geotagged photos across " + trips.length + " trips and " + venues.length + " everyday places.");
    var tabsEl = el("div", "placetabs");
    [["trips", "Trips", trips.length], ["venues", "Places", venues.length]].forEach(function (t) {
      var b = el("button", "ptab" + (tab === t[0] ? " active" : "")); b.type = "button";
      b.textContent = t[1] + " (" + t[2].toLocaleString() + ")";
      b.setAttribute("aria-pressed", tab === t[0] ? "true" : "false");
      b.onclick = function () { if (tab !== t[0]) { setQ({ tab: t[0] }); render(); } };
      tabsEl.appendChild(b);
    });
    main.appendChild(tabsEl);
    // Two-pane: map pinned (sticky) on the left, Locations list scrolls on the right (#8).
    var layout = el("div", "placeslayout");
    var mapPane = el("div", "mappane"); var mapEl = el("div"); mapEl.id = "map"; mapPane.appendChild(mapEl);
    var listPane = el("div", "listpane");
    layout.appendChild(mapPane); layout.appendChild(listPane); main.appendChild(layout);
    if (tab === "venues") {
      var vMarkers = venues.map(function (x) {
        return { lat: x.lat, lon: x.lon, key: x.name, n: x.count, target: { page: "places", venue: x.venue_id } };
      });
      placesMap(mapEl, vMarkers);
      placesList(listPane, "Everyday places", venues, {
        target: function (x) { return { page: "places", venue: x.venue_id }; },
        exportItem: function (x) {
          doVerb("/api/export", { items: x.member_ids || [] }, "Exported photos from " + prettyPlace(x.name));
        },
      });
    } else {
      // Trip markers from the AGGREGATE centroids (F-6); fall back to raw points only
      // when there are no trip clusters but points exist.
      var tMarkers = trips.length
        ? trips.map(function (t) { return { lat: t.lat, lon: t.lon, key: t.name, n: t.count, target: { page: "places", place: t.name } }; })
        : pts.map(function (p) { return { lat: p.lat, lon: p.lon, key: p.trip || p.place, n: 1, id: p.id, target: (p.trip || p.place) ? { page: "places", place: p.trip || p.place } : null }; });
      placesMap(mapEl, tMarkers);
      placesList(listPane, "Trips", trips, {
        target: function (t) { return { page: "places", place: t.name }; },
        exportItem: function (t) { exportCollection("place", t.name, prettyPlace(t.name)); },
      });
    }
  };

  // selectable document/file table (Documents, Correspondence) with verbs.
  // Checkboxes support shift-click range selection (select one, shift-click another
  // → the whole range takes the clicked box's new state).
  function fileTable(container, rows) {
    var tbl = el("table", "filetable");
    tbl.innerHTML = "<tr><th></th><th>Name</th><th>Category</th><th>Summary</th></tr>";
    var picks = [], lastIdx = -1;
    rows.forEach(function (r, i) {
      var tr = el("tr");
      var pickTd = el("td");
      var cb = el("input", "rowpick"); cb.type = "checkbox"; cb.checked = !!SEL[r.file];
      function set(on) { cb.checked = on; tr.classList.toggle("sel", on); toggleSel(r.file, on); }
      picks.push(set);
      cb.onclick = function (e) {
        if (e.shiftKey && lastIdx >= 0) {
          var a = Math.min(lastIdx, i), b = Math.max(lastIdx, i);
          for (var k = a; k <= b; k++) picks[k](cb.checked);   // extend the range to this box's state
        } else { set(cb.checked); }
        lastIdx = i;
      };
      DOC_CAT[r.file] = r.category;   // for the batch sub-move financial gate
      pickTd.appendChild(cb); tr.appendChild(pickTd);
      // Open in the lightbox rather than a new tab: for every office format the
      // raw /media bytes are attachment+octet-stream, so target="_blank" only ever
      // produced a download prompt. The lightbox renders the extracted text inline
      // and still offers Download. Kept as a real href so middle-click/"open in new
      // tab" (and any no-JS path) still reach the file.
      var nameTd = el("td");
      var nameLink = el("a", null, esc(r.name));
      nameLink.href = mediaURL(r.file);
      nameLink.setAttribute("rel", "noopener noreferrer");
      nameLink.onclick = function (ev) {
        if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button) return;  // let the browser do it
        ev.preventDefault();
        lightbox(r.file, mediaKind(r.file), null, { name: r.name, preview: r.preview });
      };
      nameTd.appendChild(nameLink);
      nameTd.appendChild(el("span", null, " " + sig(r.significance)));
      tr.appendChild(nameTd);
      var catTd = el("td", null, esc(pretty(r.category)) + (r.subcategory ? " · " + esc(pretty(r.subcategory)) : ""));
      // Per-row document-category move (Phase 2.5), examiner-only. Re-files this one
      // doc into another category; the server re-validates (account_credentials/email/
      // no-op refused, §13.3/§13.4), then a full re-render reflects the new bucket.
      if (EXAMINER) {
        var mv = el("button", "rowmove", "Move to category…");
        mv.onclick = function () {
          pickCategory("Move this document to which category?", r.category, function (cat) {
            doVerb("/api/move", { view: "document", src: r.file, to: cat }, "Moved to category")
              .then(function (x) { if (x) render(); });
          });
        };
        catTd.appendChild(document.createTextNode(" "));
        catTd.appendChild(mv);
        // Per-row financial SUB-category move (Phase 2.6), examiner-only. Shown
        // ONLY on rows already category=="financial" (client-side gate, §14.5) —
        // in-financial re-filing; the server re-validates. POSTs to=financial +
        // subcategory; a full re-render reflects the new sub-bucket.
        if (r.category === "financial") {
          var mvs = el("button", "rowmove", "Move to sub-category…");
          mvs.onclick = function () {
            pickSubcategory("Move this document to which sub-category?", r.subcategory, function (sub) {
              doVerb("/api/move", { view: "document", src: r.file, to: "financial", subcategory: sub }, "Moved to sub-category")
                .then(function (x) { if (x) render(); });
            });
          };
          catTd.appendChild(document.createTextNode(" "));
          catTd.appendChild(mvs);
        }
      }
      tr.appendChild(catTd);
      // The row used to print the summary AND ~200 characters of raw extracted
      // text beneath it — "OMAHA NE 68103-2577 BD) Ameritrade °°?”…" — which
      // roughly tripled row height and buried the one line written to be read.
      // The full text is already in the lightbox; that is where it belongs.
      tr.appendChild(el("td", null, esc(r.summary || "")));
      tbl.appendChild(tr);
    });
    // #27: on a narrow viewport this table (4 columns, one free-text) is wider
    // than the page — contain the overflow to a scrollable wrapper instead of
    // letting it push the whole page body horizontally.
    var scroller = el("div", "table-scroll");
    scroller.appendChild(tbl);
    container.appendChild(scroller);
  }

  // Which near-miss drawers are open, and how many rows deep — target -> row count.
  //
  // Outlives render(). Acting on a near-miss (promote/dismiss/reassign) changes the
  // checklist ABOVE the drawer — the tally, the ✓/— mark, the confirmed items — so
  // those verbs correctly trigger a full re-render rather than an in-place patch.
  // Without this map that re-render dropped the examiner back to a collapsed panel,
  // losing both the open drawer and any "Show more" pages: reviewing a 40-hit
  // near-miss list meant re-expanding and re-paging after every single decision.
  var NEARMISS_OPEN = {};

  // #17: bulk-select across near-miss drawers (a decision applies to whichever
  // rows are checked, possibly spanning more than one open target's drawer —
  // same "selection isn't scoped to one sub-group" precedent as the Confirm
  // queue's rsel). id -> {id, label}. Reset on every vitalDocsPanel rebuild
  // (a promote/dismiss already triggers a full render(), so there is no
  // in-place patch to preserve selection across — like NEARMISS_OPEN, this
  // just needs to not leak a stale #vselbar into the new render).
  var VITAL_SEL = {};

  function vitalSelBar() {
    var bar = document.getElementById("vselbar");
    var items = Object.keys(VITAL_SEL).map(function (k) { return VITAL_SEL[k]; });
    if (!items.length) { if (bar) bar.remove(); return; }
    if (!bar) { bar = el("div", "selbar"); bar.id = "vselbar"; document.body.appendChild(bar); }
    bar.innerHTML = '<span class="n">' + items.length + ' selected</span><span class="sep"></span>';
    var mark = el("button", "act primary", "Mark as vital");
    mark.onclick = function () {
      mark.disabled = true; notv.disabled = true;
      doVerb("/api/vital/promote", { ids: items.map(function (i) { return i.id; }) },
             "Marked " + items.length + " as vital").then(function (x) {
        if (x) { VITAL_SEL = {}; render(); } else { mark.disabled = false; notv.disabled = false; }
      });
    };
    bar.appendChild(mark);
    var notv = el("button", "act", "Not a vital document");
    notv.onclick = function () {
      mark.disabled = true; notv.disabled = true;
      doVerb("/api/vital/dismiss", { ids: items.map(function (i) { return i.id; }) },
             "Dismissed " + items.length + " near-miss(es)").then(function (x) {
        if (x) { VITAL_SEL = {}; render(); } else { mark.disabled = false; notv.disabled = false; }
      });
    };
    bar.appendChild(notv);
    bar.classList.add("show");
  }

  // Scroll an element the next frame, once the panel it belongs to is in the DOM.
  // "nearest" so a row already on screen does not jump — restoring a drawer should
  // be invisible when nothing moved.
  function scrollBackTo(node) {
    if (!node.scrollIntoView) return;
    window.requestAnimationFrame(function () {
      node.scrollIntoView({ block: "nearest" });
    });
  }

  // Near-miss review drawer (examiner-only): the candidate hits for one vital-doc
  // target that did NOT make the checklist, with WHY, and a deep link to each.
  //
  // PAGINATED, because `vital_per_target_k` is a per-case knob (default 8) that an
  // examiner raises precisely when they suspect a document was missed — the list is
  // not bounded, and silently truncating the case that needed the recall would be
  // the worst possible failure here. `offset` appends; `total` comes from the
  // server so the count shown is the true one.
  //
  // Ordering (not_evaluated first, then score) is fixed SERVER-side before the
  // slice, so the rows that matter most are on page 1 — never sort here.
  //
  // `want` (optional, offset 0 only) asks for that many rows in one request
  // instead of one page — how a drawer restores itself after a verb re-renders
  // the panel. See NEARMISS_OPEN.
  function nearMissLoad(target, box, offset, vd, want) {
    var more = box.querySelector(".vcand-more");
    if (more) more.remove();
    if (!offset) box.appendChild(el("div", "vcand-loading", "Loading…"));
    getJSON("/api/vital/near-misses?target=" + encodeURIComponent(target) +
            "&offset=" + offset +
            (!offset && want ? "&limit=" + want : "")).then(function (d) {
      var wait = box.querySelector(".vcand-loading");
      if (wait) wait.remove();
      box.dataset.loaded = "1";
      (d.rows || []).forEach(function (r) { box.appendChild(nearMissRow(target, r, vd)); });
      var shown = offset + (d.rows || []).length;
      NEARMISS_OPEN[target] = shown;   // survive the next render()
      if (shown < d.total) {
        var btn = el("button", "vcand-more",
          "Show more (" + num(shown) + " of " + num(d.total) + ")");
        btn.onclick = function () { nearMissLoad(target, box, shown, vd); };
        box.appendChild(btn);
      } else if (d.total) {
        box.appendChild(el("div", "vcand-count",
          "Showing all " + num(d.total) + "."));
      }
    }).catch(function (e) {
      var wait = box.querySelector(".vcand-loading");
      if (wait) wait.remove();
      box.appendChild(el("div", "vcand-err",
        esc("Couldn't load near-misses: " + (e && e.message ? e.message : "error"))));
    });
  }

  // One near-miss row. All estate-derived text (reason, snippet, subject, name) is
  // escaped — it is model output and mail content, never trusted markup.
  function nearMissRow(target, r, vd) {
    var blind = r.disposition === "not_evaluated";
    var rowEl = el("div", "vcand-row" + (blind ? " unread" : ""));

    var head = el("div", "vcand-head");
    // #17: bulk-select checkbox — first in the head so it reads before the link.
    var cb = el("input"); cb.type = "checkbox";
    cb.checked = !!VITAL_SEL[r.id];
    cb.setAttribute("aria-label", "Select " + (r.name || r.thread_subject || r.conversation_subject || "this near-miss"));
    cb.onclick = function (e) { e.stopPropagation(); };  // don't trigger the row link underneath
    cb.onchange = function () {
      if (cb.checked) VITAL_SEL[r.id] = { id: r.id, label: r.name || r.thread_subject || r.conversation_subject };
      else delete VITAL_SEL[r.id];
      vitalSelBar();
    };
    head.appendChild(cb);
    // Link the same two ways the confirmed items do: a browsable document opens
    // in place, an email-sourced hit deep-links to its conversation. Most hits on
    // a mail-heavy case are the latter, so a documents-only link would be dead.
    if (r.file_id) {
      var a = el("a", "vcand-link", esc(r.name || "document"));
      a.href = "#";
      a.onclick = function (e) { e.preventDefault(); go({ open: true, file: r.file_id }); };
      head.appendChild(a);
    } else if (r.thread_id) {
      var ta = el("a", "vcand-link", esc(r.thread_subject || "(no subject)"));
      ta.href = "#";
      ta.onclick = function (e) {
        e.preventDefault();
        go({ page: "emails", thread: r.thread_id }, { label: r.thread_subject || "(no subject)" });
      };
      head.appendChild(ta);
      head.appendChild(el("span", "vitals-inemails", "in Emails"));
    } else if (r.conversation_id) {
      var ca = el("a", "vcand-link", esc(r.conversation_subject || "(conversation)"));
      ca.href = "#";
      ca.onclick = function (e) {
        e.preventDefault();
        go({ page: "messages", conversation: r.conversation_id },
           { label: r.conversation_subject || "(conversation)" });
      };
      head.appendChild(ca);
      head.appendChild(el("span", "vitals-inemails", "in Messages"));
    } else {
      head.appendChild(el("span", "vcand-noitem", esc(r.name || "document")));
    }
    if (r.score != null) {
      head.appendChild(el("span", "vcand-score", r.score.toFixed(2)));
    }
    // "Never read" is a stronger signal than a considered NO — say so plainly.
    head.appendChild(el("span", "vcand-chip " + (blind ? "chip-unread" : "chip-no"),
      blind ? "never read" : (r.disposition === "unknown" ? "no reason recorded" : "not confirmed")));
    rowEl.appendChild(head);

    // Same evidence as a confirmed candidate: promote/dismiss here are the same
    // judgement, so the row that asks for it carries the same sentence. It goes
    // ABOVE the rejection reason — what the document is, then why the pipeline
    // passed on it, which is the order the examiner needs them in.
    if (r.summary) rowEl.appendChild(el("div", "vrow-sum", esc(r.summary)));
    if (r.reason) rowEl.appendChild(el("div", "vcand-reason", esc(r.reason)));
    if (r.snippet) rowEl.appendChild(el("div", "preview", esc(r.snippet)));

    // The same three dispositions a CONFIRMED item offers, so reviewing a
    // near-miss is not a dead end and the two lists behave alike.
    //
    // "Mark as vital", NOT "this IS the document" — a category can hold several
    // vital documents (two deeds, a will and its codicil), and the singular
    // wording implied promoting one settled the category. vital_doc_promoted is
    // keyed by target::path, so promoting a second path under the same target
    // has always worked; only the label said otherwise.
    var acts = el("div", "vcand-acts");

    var promote = el("button", "vcand-act primary", "Mark as vital");
    promote.onclick = function () {
      doVerb("/api/vital/promote", { id: r.id }, "Marked as a vital document")
        .then(function (x) { if (x) render(); });
    };
    acts.appendChild(promote);

    // Dismissal is a statement about the DOCUMENT (keyed by path), so it drops
    // this file from every vital category it is a candidate under, not just this
    // one — same semantics as dismissing a confirmed item. It also takes the row
    // out of the near-miss list, which is the point: a reviewed-and-rejected
    // candidate should not keep reappearing as unreviewed.
    var dismiss = el("button", "vcand-act", "Not a vital document");
    dismiss.onclick = function () {
      doVerb("/api/vital/dismiss", { id: r.id }, "Dismissed near-miss")
        .then(function (x) { if (x) render(); });
    };
    acts.appendChild(dismiss);

    // Reassign = promote AND file under a different category, in ONE audited
    // action ("this is vital, but it's a deed, not a will"). No scope prompt: a
    // near-miss promotion creates exactly one item, so the global/single choice
    // a confirmed multi-category doc needs does not arise here.
    var reassign = el("button", "vcand-act", "Reassign…");
    reassign.onclick = function () {
      pickVitalTarget("Mark as vital under which document type?",
                      (vd && vd.all_targets) || [], target, function (to) {
        doVerb("/api/vital/promote", { id: r.id, to_target: to },
               "Marked as vital and reassigned")
          .then(function (x) { if (x) render(); });
      });
    };
    acts.appendChild(reassign);

    rowEl.appendChild(acts);
    return rowEl;
  }


  // G-2: full vital-documents checklist — one row per searched-for document type,
  // ✓ found (deep-links to the doc[s]) or "not found in this collection". All
  // estate-derived text (labels, item names, tags) is escaped via esc().
  // ── vital documents: where the estate stands (N-2) ────────────────────────
  // Rebuilt from a wall of buttons into a statement of position.
  //
  // What it replaced: twenty-seven headings, each spilling its candidates inline
  // as wrapping text, every one carrying Confirm / Not a vital document /
  // Reassign… — about 538 buttons above a page named for 4,643 documents. Two
  // documents routinely shared a line, so the gap inside a candidate's button
  // group equalled the gap between groups and you could not see which control
  // belonged to which file. The panel's only summary line, "14 of 27 key document
  // types found", read as a finished score while 172 candidates sat undecided and
  // 1,147 near-misses unreviewed — the exact work the release gate is waiting on.
  //
  // Two jobs live here and they are not the same job: reviewing candidates, and
  // reading what review has already concluded. This screen is built for the
  // second. It answers "where does this estate stand, and can I trust the
  // answer?" — the per-item decision controls survive inside an expanded type,
  // one row each, but they are no longer what the page is about.

  // Where the PIPELINE filed a document, read out of its own delivered path:
  // /output/documents/legal/court_filing/DISSOLUTION JUDGMENT.pdf → "legal · court filing"
  //
  // This is the evidence that was missing from every decision on the old screen.
  // A reviewer looking at a filename alone signed off a divorce judgment as a
  // marriage certificate, a will draft and a power of attorney as property deeds.
  // The classifier had already disagreed with all three, in the path, on the
  // client, for free. No claim is made about who is right — the two readings are
  // simply shown side by side, which is all a human needs to catch it.
  function filedUnder(path) {
    var m = /\/output\/documents\/([^\/]+)(?:\/([^\/]+))?\/[^\/]+$/.exec(String(path || ""));
    if (!m) return "";
    return sentenceCase(m[1]) + (m[2] ? " · " + pretty(m[2]) : "");
  }

  function vitalStats(targets) {
    var st = { types: targets.length, found: 0, signed: 0, undecided: 0, near: 0, capped: 0 };
    targets.forEach(function (t) {
      if (t.found) st.found++;
      (t.items || []).forEach(function (it) { it.reviewed ? st.signed++ : st.undecided++; });
      st.near += t.near_miss_count || 0;
      if (t.near_miss_capped) st.capped++;
    });
    return st;
  }

  function vitalStat(bar, n, label, work, href) {
    var d = el(href ? "a" : "div", "vstat" + (work ? " work" : "") + (href ? " go" : ""));
    if (href) { d.href = href; d.title = "Open the review queue"; }
    d.appendChild(el("b", null, esc(num(n))));
    d.appendChild(el("span", null, esc(label)));
    bar.appendChild(d);
  }

  // One candidate, one row. The old layout let these wrap inline like words.
  function vitalItemRow(t, it, vd) {
    var row = el("div", "vrow" + (it.reviewed ? " done" : ""));
    var main_ = el("div", "vrow-main");
    var label = it.name || it.thread_subject || it.conversation_subject || it.tag || "document";

    var link;
    if (it.file_id) {
      link = el("a", "vrow-name", esc(label));
      link.href = "#";
      link.onclick = function (e) { e.preventDefault(); go({ open: true, file: it.file_id }); };
    } else if (it.thread_id) {
      link = el("a", "vrow-name", esc(it.thread_subject || "(no subject)"));
      link.href = "#";
      link.onclick = (function (tid, subj) {
        return function (e) { e.preventDefault(); go({ page: "emails", thread: tid }, { label: subj }); };
      })(it.thread_id, it.thread_subject || "(no subject)");
    } else if (it.conversation_id) {
      link = el("a", "vrow-name", esc(it.conversation_subject || "(conversation)"));
      link.href = "#";
      link.onclick = (function (cid, subj) {
        return function (e) { e.preventDefault(); go({ page: "messages", conversation: cid }, { label: subj }); };
      })(it.conversation_id, it.conversation_subject || "(conversation)");
    } else {
      link = el("span", "vrow-name", esc(label));
    }
    main_.appendChild(link);

    // WHAT THIS DOCUMENT IS — the sentence the decision actually turns on, and
    // the reason this row exists at all. "Yes, this is it" asks the examiner to
    // certify that a file IS the deed; a filename cannot answer that, and until
    // now a filename (plus where the pipeline filed it) was the whole row. The
    // summary was written at classification time and was already on screen fifty
    // rows further down, in the documents table — never where the click happens.
    // Absent when the server withheld it because this role may not read the
    // underlying item; the row still works, it just has less to go on.
    if (it.summary) main_.appendChild(el("div", "vrow-sum", esc(it.summary)));

    // Provenance and decision state are EXAMINER vocabulary. "The pipeline filed
    // this under Legal · court filing" is meaningless to a family, and "Not yet
    // decided" on their own father's will reads as an alarm they cannot act on.
    // audience.py's asymmetry applies here as everywhere: a leak on the family
    // side fails open, so gate rather than reword.
    var where = filedUnder(it.path || it.file_id);
    var prov = it.thread_id ? "Found in an email"
             : it.conversation_id ? "Found in a message thread"
             : (EXAMINER && where) ? "The pipeline filed this under " + where
             : "";
    if (prov) main_.appendChild(el("div", "vrow-prov", esc(prov)));

    // WHERE ELSE THIS DOCUMENT IS. A vital match is per (document, type) pair, so
    // the same file can sit under several types at once — and nothing on the row
    // said so, which made two of this panel's verbs behave in ways a reader could
    // not predict. Dismiss is keyed by PATH and silently drops the document from
    // every type it matched; reassign moves only the clicked pairing and leaves
    // the others where they are. Neither is wrong, but both are surprising while
    // the row pretends the document exists only here.
    if (EXAMINER) {
      var elsewhere = vitalPathTargets(vd, it.path).filter(function (tgt) {
        return tgt !== t.target;
      });
      if (elsewhere.length) {
        main_.appendChild(el("div", "vrow-also",
          "Also a candidate under " + esc(elsewhere.map(function (tgt) {
            return vitalTargetLabel(vd, tgt);
          }).join(", ")) + "."));
      }
    }
    row.appendChild(main_);

    if (EXAMINER) {
      var state = el("div", "vrow-state");
      state.appendChild(el("span", "vpill " + (it.reviewed ? "yes" : "open"),
        it.reviewed ? "Signed off" : "Not yet decided"));
      row.appendChild(state);
    }

    if (EXAMINER) {
      // Same three audited, reversible overlay verbs as before. What changed is
      // their weight: Confirm was the only filled button in the group, first in
      // reading order, repeated 172 times — the cheapest thing on screen to click,
      // asking for a judgement the row gave no evidence for. Three equal buttons
      // is the honest signal when the system genuinely does not know.
      var acts = el("div", "vrow-acts");
      if (!it.reviewed) {
        var confirm = el("button", "vact", "Yes, this is it");
        confirm.onclick = function () {
          doVerb("/api/vital/confirm", { id: it.id }, "Signed off").then(function (x) { if (x) render(); });
        };
        acts.appendChild(confirm);
      }
      var dismiss = el("button", "vact", it.reviewed ? "Undo — not this" : "No");
      dismiss.onclick = function () {
        // "Not a vital document" is a statement about the DOCUMENT, so the verb
        // is keyed by path and drops it from EVERY type it matched. That is a
        // defensible design and an indefensible surprise: a reader clicking
        // "Undo — not this" under one heading has no reason to expect a pending
        // candidate under another heading to vanish with it. Ask first, and only
        // when there is actually something else to lose.
        var also = vitalPathTargets(vd, it.path).filter(function (tgt) {
          return tgt !== t.target;
        });
        function send() {
          doVerb("/api/vital/dismiss", { id: it.id }, "Dismissed")
            .then(function (x) { if (x) render(); });
        }
        if (!also.length) return send();
        confirmModal(
          "Remove from " + (also.length + 1) + " document types?",
          "\"Not a vital document\" is recorded about the document itself, so this "
          + "also removes it from " + also.map(function (tgt) {
              return vitalTargetLabel(vd, tgt); }).join(", ")
          + ", where it is still awaiting a decision. To move it out of "
          + t.label + " without touching those, use \u201CAnother type\u2026\u201D "
          + "instead.",
          "Remove from all of them", send);
      };
      acts.appendChild(dismiss);
      var reassign = el("button", "vact", "Another type…");
      reassign.onclick = function () {
        pickVitalTarget("Reassign this document", vd.all_targets, t.target, function (to) {
          var cats = vitalPathTargets(vd, it.path);
          function send(scope) {
            doVerb("/api/vital/reassign", { id: it.id, to_target: to, scope: scope }, "Reassigned")
              .then(function (x) { if (x) render(); });
          }
          if (cats.length > 1) { pickScope(cats.length, send); } else { send("single"); }
        });
      };
      acts.appendChild(reassign);
      row.appendChild(acts);
    }
    return row;
  }

  function vitalDocsPanel(main, vd) {
    // #17: a promote/dismiss triggers a full render(); drop selection state and
    // any floating bar left over from the previous build.
    VITAL_SEL = {};
    var staleVBar = document.getElementById("vselbar"); if (staleVBar) staleVBar.remove();
    if (!vd) return;
    if (vd.available === false) {
      main.appendChild(el("p", "notice", "Vital-document scan not available for this case."));
      return;
    }
    if (!vd.available) return;

    var targets = (vd.targets || []).slice();
    var st = vitalStats(targets);
    var panel = el("section", "vitals2");

    var h = el("div", "vitals2-head");
    h.appendChild(el("h2", null, "Vital documents"));
    h.appendChild(el("p", "vitals2-lead",
      esc(num(st.types) + " document types an estate needs, and where each one stands.")));
    panel.appendChild(h);

    // The state bar. The old panel had ONE number and it flattered: "14 of 27
    // found" says nothing about whether anyone has looked. Four numbers, because
    // four things are true at once and only one of them is a score.
    var bar = el("div", "vstats");
    vitalStat(bar, st.found, "types have a candidate");
    if (EXAMINER) {
      var queue = urlFor({ page: "review", group: "vital" }, { label: "Vital review" });
      vitalStat(bar, st.signed, "signed off");
      vitalStat(bar, st.undecided, "candidates undecided", st.undecided > 0,
                st.undecided ? queue : null);
      vitalStat(bar, st.near, "near-misses unreviewed", st.near > 0,
                st.near ? queue : null);
      var hint = el("p", "vstats-hint");
      hint.textContent = (st.undecided || st.near)
        ? "Nothing is released to the family until every one of those has a decision."
          + (st.capped ? " Each list stops at " + num(vd.per_target_k || 25)
              + " candidates, so " + num(st.near) + " is a floor, not a total." : "")
        : "Every candidate and near-miss has a decision.";
      bar.appendChild(hint);
    }
    panel.appendChild(bar);

    // Sorted by what is waiting on you, not alphabetically: the types with
    // undecided candidates first (most first), then the types already worked,
    // then the ones nothing was found for. The old panel used the config's
    // declaration order, which buried the 45-candidate types among the empties.
    function rank(t) {
      var open = (t.items || []).filter(function (i) { return !i.reviewed; }).length;
      if (t.found && open) return [0, -open];
      if (t.found) return [1, 0];
      return [2, -(t.near_miss_count || 0)];
    }
    if (EXAMINER) {
      targets.sort(function (a, b) {
        var ra = rank(a), rb = rank(b);
        return ra[0] - rb[0] || ra[1] - rb[1];
      });
    }

    // The examiner grid carries four numeric columns, the family view one.
    var tbl = el("div", "vtable" + (EXAMINER ? " ex" : ""));
    var hd = el("div", "vtr vthead");
    hd.appendChild(el("div", "vc-name", "Document type"));
    hd.appendChild(el("div", "vc-n", "Candidates"));
    if (EXAMINER) {
      hd.appendChild(el("div", "vc-n", "Signed off"));
      hd.appendChild(el("div", "vc-n", "Undecided"));
      hd.appendChild(el("div", "vc-n", "Near-misses"));
    }
    tbl.appendChild(hd);

    targets.forEach(function (t) {
      var items = t.items || [];
      var signed = items.filter(function (i) { return i.reviewed; }).length;
      var open = items.length - signed;

      var tr = el("div", "vtr" + (open ? " todo" : "") + (t.found ? "" : " none"));
      var nameCell = el("div", "vc-name");
      var caret = el("span", "vcaret", "▸");
      nameCell.appendChild(caret);
      nameCell.appendChild(el("span", "vdot " + (t.found ? "ok" : "no")));
      nameCell.appendChild(el("span", "vlabel2", esc(t.label)));
      tr.appendChild(nameCell);
      tr.appendChild(el("div", "vc-n", items.length ? esc(num(items.length)) : "—"));
      if (EXAMINER) {
        tr.appendChild(el("div", "vc-n" + (signed ? "" : " nil"), signed ? esc(num(signed)) : "—"));
        tr.appendChild(el("div", "vc-n" + (open ? " todo" : " nil"), open ? esc(num(open)) : "—"));
        tr.appendChild(el("div", "vc-n" + (t.near_miss_count ? " todo" : " nil"),
          t.near_miss_count ? esc(num(t.near_miss_count)) : "—"));
      }

      var detail = el("div", "vdetail");
      detail.hidden = true;
      tbl.appendChild(tr);
      tbl.appendChild(detail);

      var built = false;
      function build() {
        if (built) return;
        built = true;
        // The two jobs, side by side and clearly separated: work this type's
        // candidates one at a time (the queue), or scan its weaker matches in
        // place (the drawer, further down). Reading what has already been decided
        // is this panel's own job and needs neither.
        if (EXAMINER && (open || t.near_miss_count)) {
          var goq = el("a", "vgo",
            open ? "Review " + num(open) + " undecided" : "Work this type in the queue");
          goq.href = urlFor({ page: "review", group: "vital", target: t.target },
                            { label: t.label });
          goq.onclick = function (e) { e.stopPropagation(); };
          detail.appendChild(goq);
        }
        if (items.length) {
          items.forEach(function (it) { detail.appendChild(vitalItemRow(t, it, vd)); });
        } else {
          detail.appendChild(el("p", "vnone",
            EXAMINER && t.near_miss_count
              ? "Nothing matched well enough to be a candidate. "
                + num(t.near_miss_count) + " weaker matches are listed below."
              : "Nothing in this collection matched."));
        }
        if (EXAMINER && t.near_miss_count) {
          // The existing near-miss drawer, unchanged — it is the REVIEW surface,
          // and reviewing is the other job. It stays behind its own button here.
          var box = el("div", "vcand-drawer");
          box.hidden = true;
          var toggle = el("button", "vcand",
            "Review " + num(t.near_miss_count) + " near-miss" + (t.near_miss_count === 1 ? "" : "es"));
          toggle.setAttribute("aria-expanded", "false");
          toggle.onclick = (function (tgt, btn, bx, vdoc) {
            return function () {
              if (!bx.hidden) {
                bx.hidden = true; btn.setAttribute("aria-expanded", "false");
                delete NEARMISS_OPEN[tgt]; return;
              }
              bx.hidden = false; btn.setAttribute("aria-expanded", "true");
              NEARMISS_OPEN[tgt] = NEARMISS_OPEN[tgt] || 0;
              if (!bx.dataset.loaded) nearMissLoad(tgt, bx, 0, vdoc);
            };
          })(t.target, toggle, box, vd);
          var foot = el("div", "vdetail-foot");
          foot.appendChild(toggle);
          // No per-row cap chip. It used to repeat "SHOWING THE TOP 25 — MORE MAY
          // EXIST" on all 27 rows, in capitals, which turned the one thing it had
          // to say — these lists are truncated — into furniture nobody reads. The
          // state bar says it once, with the real total attached.
          detail.appendChild(foot);
          detail.appendChild(box);
        }
      }

      function setOpen(on) {
        detail.hidden = !on;
        tr.classList.toggle("open", on);
        caret.textContent = on ? "▾" : "▸";
        tr.setAttribute("aria-expanded", String(on));
        if (on) build();
      }
      tr.onclick = function () { setOpen(detail.hidden); };
      keyable(tr, "button", t.label);

      // Re-open a type the examiner was working in before this render, and put it
      // back in front of them — the panel is rebuilt from the top on every verb.
      if (NEARMISS_OPEN[t.target] != null) {
        setOpen(true);
        var bx = detail.querySelector(".vcand-drawer");
        var bt = detail.querySelector(".vcand");
        if (bx && bt) {
          bx.hidden = false; bt.setAttribute("aria-expanded", "true");
          nearMissLoad(t.target, bx, 0, vd, NEARMISS_OPEN[t.target]);
        }
        scrollBackTo(tr);
      }
    });

    panel.appendChild(tbl);
    main.appendChild(panel);
  }

  // ── Documents: an index of what is in here, then one branch at a time ──
  // The page used to open on the vital checklist with a category dropdown under
  // it. The dropdown worked, but a filter in a <select> is not a place anybody
  // browses: the D&D character sheets under creative writing were reachable and
  // effectively invisible. And the sub-taxonomy the pipeline had already built
  // for `legal` was thrown away by the row builder, so a thousand documents that
  // ARE sorted into will / deed / power-of-attorney folders looked unsorted.
  //
  // Same shape as Emails now: an index of branches with true counts, then a
  // filtered list. The estate checklist is the first branch, because that is
  // what the archive is for, but it is a branch rather than the whole page.

  var DOC_CAT_LABELS = {
    financial: "Financial", legal: "Legal", medical: "Medical",
    recipe: "Recipes", creative_writing: "Creative writing",
    personal_correspondence: "Personal correspondence",
    miscellaneous: "Miscellaneous", account_credentials: "Account credentials",
  };
  function docCatLabel(c) { return DOC_CAT_LABELS[c] || pretty(c); }

  // One expandable category branch: the name, its count, and its subcategories
  // where the pipeline actually filed some. A category with no sub-taxonomy has
  // no caret — offering one that opens onto a single row is a lie about depth.
  function docCatRow(container, c) {
    var subs = c.subcategories || [];
    var sec = el("section", "rec-sec");
    var head_ = el("div", "rec-sechead");
    if (subs.length) {
      var caret = el("span", "vcaret", "▸");
      head_.appendChild(caret);
    } else {
      head_.appendChild(el("span", "vcaret vcaret-none", "·"));
    }
    head_.appendChild(el("span", "rec-seclabel", esc(docCatLabel(c.category))));
    head_.appendChild(el("span", "rec-secn", num(c.count)));
    var open = el("a", "rec-seconly", "Open all " + num(c.count) + " →");
    open.href = urlFor({ page: "documents", cat: c.category },
                       { label: docCatLabel(c.category) });
    open.onclick = function (e) { e.stopPropagation(); };
    head_.appendChild(open);
    sec.appendChild(head_);

    if (subs.length) {
      var body = el("div", "rec-secbody doc-subs");
      body.hidden = true;
      subs.forEach(function (sub) {
        var a = el("a", "ebd-chip");
        a.href = urlFor({ page: "documents", cat: c.category, subcat: sub.name },
                        { label: docCatLabel(c.category) + " · " + pretty(sub.name) });
        a.appendChild(el("span", "ebd-chip-l", esc(pretty(sub.name))));
        a.appendChild(el("span", "ebd-chip-n", num(sub.count)));
        body.appendChild(a);
      });
      head_.onclick = function () {
        body.hidden = !body.hidden;
        caret.textContent = body.hidden ? "▸" : "▾";
        head_.classList.toggle("open", !body.hidden);
        head_.setAttribute("aria-expanded", String(!body.hidden));
      };
      keyable(head_, "button", docCatLabel(c.category));
      head_.setAttribute("aria-expanded", "false");
      sec.appendChild(body);
    }
    container.appendChild(sec);
  }

  function documentsIndex(main, data) {
    var index = data.index || [];
    head(main, "Documents", "Documents",
      num(data.total || 0) + " documents. Every count below is the whole "
      + "collection, not a page of it.");

    // The estate branch first — a summary and a way in, not the whole checklist.
    var vd = data.vital_docs;
    if (vd && vd.available) {
      var st = vitalStats((vd.targets || []).slice());
      var vsec = el("section", "eix-panel doc-estate");
      vsec.appendChild(el("h2", null, "Vital documents"));
      vsec.appendChild(el("p", "eix-note",
        num(st.types) + " document types an estate needs, searched across every "
        + "document and email in the archive."));
      var bar = el("div", "vstats");
      vitalStat(bar, st.found, "types have a candidate");
      if (EXAMINER) {
        vitalStat(bar, st.signed, "signed off");
        vitalStat(bar, st.undecided, "candidates undecided", st.undecided > 0);
        vitalStat(bar, st.near, "near-misses unreviewed", st.near > 0);
      }
      vsec.appendChild(bar);
      var go_ = el("a", "eix-more", "Work the checklist, type by type →");
      go_.href = urlFor({ page: "documents", view: "vital" },
                        { label: "Vital documents" });
      vsec.appendChild(go_);
      main.appendChild(vsec);
    }

    var csec = el("section", "doc-cats");
    csec.appendChild(el("h2", null, "By category"));
    // The two panels on this page have OPPOSITE inclusion rules and nothing said
    // so. The classifier tagged 59,758 items — 55,115 of them emails — and this
    // list drops every one, or the page would be 92% email. The estate checklist
    // above does not drop them, because a will written in an email is still the
    // will. Both choices are right; the silence between them is what confuses.
    csec.appendChild(el("p", "eix-note",
      "How the pipeline filed each document. Emails are not here — they have "
      + "their own section, and including them would leave this page almost "
      + "entirely email. The vital-documents checklist above does search them, so a "
      + "conversation can be a candidate for a vital document without ever "
      + "appearing in this list."));
    index.forEach(function (c) { docCatRow(csec, c); });
    main.appendChild(csec);
  }

  // One category (optionally one subcategory): the documents, with the sibling
  // subcategories offered as a break-down so a reader can move sideways without
  // going back to the index.
  function documentsCategory(main, data, cat, subcat) {
    var index = data.index || [];
    var entry = index.filter(function (c) { return c.category === cat; })[0] || {};
    var subs = entry.subcategories || [];
    var title = docCatLabel(cat) + (subcat ? " · " + pretty(subcat) : "");
    setCrumb(Q.crumb || title);
    // Seed the lead from the index we already have rather than an empty string:
    // head() only creates the .lead element when there is something to put in
    // it, so passing "" left nothing for the pager to write into afterwards.
    var seedCount = subcat
      ? ((subs.filter(function (x) { return x.name === subcat; })[0] || {}).count || 0)
      : (entry.count || 0);
    var controls = head(main, "Documents", title,
      num(seedCount) + " document" + (seedCount === 1 ? "" : "s") + ".");
    var back = el("button", "btn chip", "← All documents");
    back.onclick = function () { go({ page: "documents" }); };
    controls.appendChild(back);
    var expBtn = el("button", "btn", "Export filtered");
    controls.appendChild(expBtn);

    if (subs.length) {
      var strip = el("div", "ebd"); main.appendChild(strip);
      var chips = el("div", "ebd-body"); strip.appendChild(chips);
      function chip(name, label, n) {
        var a = el("a", "ebd-chip" + (subcat === name ? " on" : ""));
        a.href = urlFor(name ? { page: "documents", cat: cat, subcat: name }
                             : { page: "documents", cat: cat },
                        { label: docCatLabel(cat) + (name ? " · " + pretty(name) : "") });
        a.appendChild(el("span", "ebd-chip-l", esc(label)));
        a.appendChild(el("span", "ebd-chip-n", num(n)));
        chips.appendChild(a);
      }
      chip("", "All " + docCatLabel(cat).toLowerCase(), entry.count || 0);
      subs.forEach(function (s) { chip(s.name, pretty(s.name), s.count); });
    }

    var holder = el("div"); main.appendChild(holder);
    var pg = pager("/api/documents", {
      getParams: function () { return { cat: cat, subcat: subcat || "" }; },
      render: function (all, total) {
        holder.innerHTML = "";
        fileTable(holder, all);
        var leadEl = main.querySelector(".pagehead-title .lead");
        if (leadEl) leadEl.textContent = num(total || 0) + " document"
          + (total === 1 ? "" : "s") + ".";
      },
    });
    main.appendChild(pg.footer);
    expBtn.onclick = function () {
      var key = subcat ? cat + ":" + subcat : cat;
      exportCollection("category", key, pretty(subcat || cat));
    };
    pg.load(true);
  }

  P.documents = function (main, data) {
    // The 27-type checklist, on its own page now rather than above every browse.
    if (Q.view === "vital") {
      setCrumb(Q.crumb || "Vital documents");
      var ctrls = head(main, "Documents", "Vital documents", "");
      var back = el("button", "btn chip", "← All documents");
      back.onclick = function () { go({ page: "documents" }); };
      ctrls.appendChild(back);
      vitalDocsPanel(main, data.vital_docs);
      return;
    }
    if (Q.cat) return documentsCategory(main, data, Q.cat, Q.subcat || "");
    return documentsIndex(main, data);
  };

  // ── Document photos (was "Correspondence") ──
  // The old section stapled two unrelated things together and named itself after
  // the smaller one. Its "typed" list was an EXACT duplicate of Documents →
  // Personal correspondence — the same 111 files, byte for byte — while its real
  // content was 1,221 photographs and screenshots OF documents: pictures of
  // paper, produced by the photo pipeline, carrying no extracted text.
  //
  // Those images are the reason this page has to exist. They are excluded from
  // Photos for being documents ("photo universe: … excluded 1,221 scanned
  // documents") and absent from Documents for being photographs, so this is the
  // only place in the archive they can be seen at all. The duplicated letters
  // now link to where they are actually filed instead of being listed twice.
  P.correspondence = function (main, d) {
    var typed = d.typed || [], hand = d.handwritten || [];
    var scanned = d.scanned || { rows: [], total: 0 };
    var scannedTotal = scanned.total || 0;

    var controls = head(main, "Document Photos", "Document photos",
      num(scannedTotal) + " photographs and screenshots of documents and letters.");
    main.appendChild(el("p", "eix-note",
      "Pictures of paper, rather than files the archive could read. They come "
      + "from the photographs rather than the documents, so they carry no "
      + "extracted text and no summary — and because they are set aside from "
      + "Photos for being documents, and absent from Documents for being "
      + "photographs, this page is the only place they appear."));

    // The letters that used to be listed here are filed as documents. Point at
    // them rather than printing the same rows a second time under a second name.
    if (typed.length || hand.length) {
      var note = el("p", "eix-note");
      note.appendChild(document.createTextNode(
        num(typed.length + hand.length) + " written letters and cards are filed "
        + "with the documents, because that is what they are. "));
      var a_ = el("a", null, "Open them under Documents →");
      a_.href = urlFor({ page: "documents", cat: "personal_correspondence" },
                       { label: "Personal correspondence" });
      note.appendChild(a_);
      main.appendChild(note);
    }

    if (!scannedTotal) {
      main.appendChild(el("p", "notice", "No photographed documents in this archive."));
      return;
    }

    var holder = el("div"); main.appendChild(holder);
    var sec = el("div", "corr-sec"); holder.appendChild(sec);
    var scanCtrls = el("div", "controls"); sec.appendChild(scanCtrls);
    var scanGrid = el("div"); sec.appendChild(scanGrid);
    if (EXAMINER) marqueeSelect(scanCtrls, scanGrid, ".card", selPick);
    // The scanned images live under `.scanned` in the payload, so the pager
    // unwraps that sub-envelope. photoGrid is stateful — build once, then feed
    // it rows — so a Load-more does not rebuild the grid under the reader.
    var scanCtrl = null;
    var pg = pager("/api/correspondence", {
      unwrap: function (resp) { return resp.scanned || resp; },
      render: function (all) {
        if (scanCtrl) scanCtrl.setRows(all); else scanCtrl = photoGrid(scanGrid, all);
      },
    });
    sec.appendChild(pg.footer);
    pg.seed(d);   // the full payload; unwrap picks .scanned out of it
  };

  function emailDemoteBtn(r) {
    var b = el("button", "btn small" + (r.demoted ? " primary" : ""), r.demoted ? "Restore" : "Demote");
    b.title = r.demoted ? "Restore to its ranked position" : "Remove from top of the sort";
    b.onclick = function (ev) {
      ev.stopPropagation();
      doVerb("/api/demote/email", { thread_id: r.thread_id, subject: r.subject, restore: !!r.demoted },
        r.demoted ? "Restored email" : "Demoted email").then(function (x) { if (x) render(); });
    };
    return b;
  }

  // Redraw the significance-banded email list from the accumulated rows (rebuilt
  // on each Load-more page — the bands regroup the full loaded set).
  function drawEmailBands(container, rows) {
    container.innerHTML = "";
    // Group into significance bands so the (already significance-sorted) list reads
    // as organized by importance, not a flat date jumble (#D).
    var BANDS = [[5, "Major life events"], [4, "Emotionally resonant"], [3, "Personal"],
                 [2, "Everyday"], [1, "Routine"], [0, "Unranked"]];
    BANDS.forEach(function (band) {
      var n = band[0];
      var group = rows.filter(function (r) { return (parseInt(r.significance, 10) || 0) === n; });
      if (!group.length) return;
      container.appendChild(el("h2", null, band[1] + " (" + group.length + ")"));
      var tbl = el("table");
      tbl.innerHTML = "<tr><th>Subject</th><th>With</th><th>When</th><th>Msgs</th>" + (EXAMINER ? "<th></th>" : "") + "</tr>";
      group.forEach(function (r) {
        var tr = el("tr", "clickable");
        tr.innerHTML = "<td>" + esc(r.subject) + " " + sig(r.significance) +
          "</td><td class='preview clamp2'>" + recipients(r.participants) + "</td><td class='preview'>" +
          esc((r.date_last || "").slice(0, 10)) + "</td><td>" + num(r.message_count) + "</td>" +
          (EXAMINER ? "<td class='actcell'></td>" : "");
        tr.onclick = function () {
          go({ page: "emails", thread: r.thread_id }, { label: r.subject || "(no subject)" });
        };
        keyable(tr, null);   // F-12
        if (EXAMINER) tr.lastChild.appendChild(emailDemoteBtn(r));
        tbl.appendChild(tr);
      });
      container.appendChild(tbl);
    });
  }

  // G-4: attachment chips under one email message. Inline cid-embedded images
  // (logos/signatures) are suppressed so the row isn't cluttered. A resolved
  // file_id deep-links into the doc/photo lightbox (go({open,file})); an
  // unresolved attachment renders name + size only — never a broken link.
  // Estate-derived filenames go through textContent only; file_id through go()
  // (which encodeURIComponent's it). Returns null when there is nothing to show.
  function attachmentChips(list) {
    var atts = (list || []).filter(function (a) { return a && !a.is_inline; });
    if (!atts.length) return null;
    var wrap = el("div", "attrow");
    atts.forEach(function (a) {
      var label = "📎 " + (a.filename || "attachment") +
        (a.size_bytes ? " · " + fmtBytes(a.size_bytes) : "");
      var node;
      if (a.file_id) {
        node = el("a", "attchip");
        node.href = "#";
        node.onclick = function (e) { e.preventDefault(); go({ open: true, file: a.file_id }); };
      } else {
        node = el("span", "attchip noatt");
      }
      node.textContent = label;
      node.title = a.filename || "attachment";
      wrap.appendChild(node);
    });
    return wrap;
  }

  // Thread message list — the Emails detail page AND the vital-doc bulk-review
  // pager render the same thing, so it lives in one place. `holder` gets one
  // .emsg per message, reply nesting via `depth`.
  function drawThreadMessages(holder, t) {
    (t.messages || []).forEach(function (m) {
      var it = el("div", "item emsg");
      it.style.marginLeft = Math.min(m.depth || 0, 6) * 22 + "px";  // reply nesting (#3)
      it.innerHTML = "<div class='emh'><strong>" + esc(m.from_display || m.from || "") + "</strong> &rarr; " + recipients(m.to) +
        sig(m.significance) + "<span class='when'>" + esc((m.date || "").replace("T", " ")) + "</span></div>" +
        (m.subject ? "<div class='emsub'>" + esc(m.subject) + "</div>" : "") +
        "<div class='embody'>" + esc(m.body || "") + "</div>";
      var atts = attachmentChips(m.attachments);   // G-4
      if (atts) it.appendChild(atts);
      holder.appendChild(it);
    });
    if (!(t.messages || []).length)
      holder.appendChild(el("p", "notice", "No message bodies recovered."));
  }

  // Conversation transcript — messages and call events interleaved into one
  // chronological stream. Shared by the Messages detail page and the pager.
  function drawConversationStream(holder, c) {
    var stream = [];
    (c.messages || []).forEach(function (m) { stream.push({ ts: m.ts, msg: m }); });
    (c.call_events || []).forEach(function (ce) { stream.push({ ts: ce.ts, call: ce }); });
    stream.sort(function (a, b) {
      var x = a.ts || "", y = b.ts || "";
      return x < y ? -1 : x > y ? 1 : 0;
    });
    stream.forEach(function (s) {
      if (s.call) {
        var ce = s.call, dur = callDuration(ce.duration_s);
        var cev = el("div", "callevent");
        cev.innerHTML = "📞 <span class='calltype'>" + esc(pretty(ce.call_type || "call")) + "</span>" +
          (dur ? " <span class='when'>" + esc(dur) + "</span>" : "") +
          "<span class='when'>" + esc(String(ce.ts || "").replace("T", " ")) + "</span>";
        holder.appendChild(cev);
        return;
      }
      var m = s.msg;
      var bub = el("div", "bubble " + (m.direction === "sent" ? "sent" : "received"));
      bub.appendChild(el("div", "bubh",
        "<strong>" + esc(m.sender_display || m.sender || "") + "</strong><span class='when'>" +
        esc(String(m.ts || "").replace("T", " ")) + "</span>"));
      if (m.text) bub.appendChild(el("div", "bubtext", esc(m.text)));
      (m.attachments || []).forEach(function (a) { bub.appendChild(messageAttachment(a)); });
      holder.appendChild(bub);
    });
    if (!stream.length)
      holder.appendChild(el("p", "notice", "No messages recovered in this conversation."));
    return stream.length;
  }

  // ── Emails: an index first, then one group at a time ──
  // 21,988 conversations used to open as a single significance-sorted list whose
  // band headings counted the rows the pager happened to have loaded (2,000), so
  // every heading below the first stated a page-local number as a total. And the
  // per-thread `categories` the pipeline writes were never shown at all, so the
  // shape of the mail — 8,586 work, 8,389 personal, 3,514 newsletters — was
  // invisible. The page now opens on that shape and drills into ONE group, with
  // every count coming from the server's facets over the whole filtered set.

  var EMAIL_CATS = {
    personal_correspondence: "Personal", work_correspondence: "Work",
    financial: "Financial", medical: "Medical", legal: "Legal",
    newsletters_lists: "Newsletters & lists", miscellaneous: "Miscellaneous",
  };
  function emailCatLabel(name) { return EMAIL_CATS[name] || pretty(name); }

  // One clickable row in an index panel: label, count, and a bar sized by share
  // of the largest row. The bar is set through CSSOM, never an inline style
  // attribute — the page CSP is style-src 'self'.
  function emailIndexRow(label, count, max, dest, crumb) {
    var a = el("a", "eix-row");
    a.href = urlFor(dest, { label: crumb || label });
    a.appendChild(el("span", "eix-label", esc(label)));
    a.appendChild(el("span", "eix-count", num(count)));
    var track = el("span", "eix-bar");
    var fill = el("span", "eix-fill");
    fill.style.width = Math.max(2, Math.round((count / (max || 1)) * 100)) + "%";
    track.appendChild(fill);
    a.appendChild(track);
    return a;
  }

  function emailIndexPanel(title, note, rows) {
    var sec = el("section", "eix-panel");
    sec.appendChild(el("h2", null, title));
    if (note) sec.appendChild(el("p", "eix-note", note));
    var max = rows.reduce(function (m, r) { return Math.max(m, r.count); }, 0);
    rows.forEach(function (r) {
      sec.appendChild(emailIndexRow(r.label, r.count, max, r.dest, r.crumb));
    });
    return sec;
  }

  // The landing view: what is in here, and the two ways of cutting it. Both cuts
  // are the pipeline's own — significance is its ranking, categories are the ones
  // named in case_config — so neither is invented here.
  function emailIndex(main, data) {
    var f = (data && data.facets) || {};
    var total = (data && data.total) || 0;
    head(main, "Emails", "Emails",
      num(total) + " conversations. Pick a way in — every count below is the whole "
      + "archive, not a page of it.");
    // Said from this side too, because the boundary is invisible from either one.
    main.appendChild(el("p", "eix-note",
      "This is the mail. Files that arrived as documents are in Documents, and "
      + "an email is never listed there — but the vital-documents checklist on that page "
      + "does search this mail, so a conversation here can also be a candidate "
      + "for a vital document."));

    var wrap = el("div", "eix-cols"); main.appendChild(wrap);
    wrap.appendChild(emailIndexPanel(
      "By significance", "How the pipeline ranked each conversation.",
      (f.bands || []).map(function (b) {
        return { label: b.label, count: b.count,
                 dest: { page: "emails", band: String(b.n) }, crumb: b.label };
      })));
    wrap.appendChild(emailIndexPanel(
      "By subject", "What the conversation is about. A thread can be in more than one.",
      (f.categories || []).map(function (c) {
        return { label: emailCatLabel(c.name), count: c.count,
                 dest: { page: "emails", cat: c.name }, crumb: emailCatLabel(c.name) };
      })));

    // People and years are the other two cuts, but they are long tails rather
    // than short lists — offered as a way in, not enumerated here.
    var more = el("div", "eix-cols"); main.appendChild(more);
    more.appendChild(emailIndexPanel(
      "By year", null,
      (f.years || []).map(function (y) {
        return { label: y.year, count: y.count,
                 dest: { page: "emails", year: y.year }, crumb: y.year };
      })));
    var people = (f.correspondents || []).slice(0, 12);
    var pplPanel = emailIndexPanel(
      "By person", "Who the conversation was with.",
      people.map(function (c) {
        return { label: c.name || c.address, count: c.count,
                 dest: { page: "emails", participant: c.address },
                 crumb: c.name || c.address };
      }));
    var all = el("a", "eix-more", "All correspondents →");
    all.href = urlFor({ page: "correspondents" }, { label: "Correspondents" });
    pplPanel.appendChild(all);
    // The owner guess is disclosed rather than applied in silence: nothing
    // upstream records whose mailbox this is (case_config's
    // owner_email_addresses is empty), so these were inferred from how much of
    // the mail they appear in, and a wrong guess would quietly reshape the list.
    if ((f.owner_addresses || []).length) {
      pplPanel.appendChild(el("p", "eix-note",
        "Treated as the account's own addresses and left out of this list: "
        + f.owner_addresses.join(", ")
        + ". The archive does not record who the mailbox belongs to, so this was "
        + "worked out from how much of the mail they appear in."));
    }
    more.appendChild(pplPanel);
  }

  // A flat thread table. Inside a group the significance bands are either the
  // thing you already picked or beside the point, so the old band headings would
  // only reintroduce the counting problem they caused.
  function emailThreadTable(container, rows) {
    container.innerHTML = "";
    if (!rows.length) {
      container.appendChild(el("p", "notice", "No conversations match."));
      return;
    }
    var tbl = el("table");
    tbl.innerHTML = "<tr><th>Subject</th><th>With</th><th>When</th><th>Msgs</th>"
      + (EXAMINER ? "<th></th>" : "") + "</tr>";
    rows.forEach(function (r) {
      var tr = el("tr", "clickable");
      // The estate scan touched 300 of 21,988 conversations. The checklist could
      // already reach a thread; the thread could not tell you it was on the
      // checklist. This is that missing direction, said on the row itself.
      // Mail the family will never see. A distinct claim from the estate marker
      // — this one is about audience, not relevance — so a distinct chip.
      var resc = r.rescued
        ? ' <span class="est-chip resc" title="Triage had discarded this as bulk;'
          + ' it was pulled back because it mentions the estate. The family index'
          + ' does not contain it.">family will not see</span>' : "";
      var est = r.estate
        ? ' <span class="est-chip ' + (r.estate.kind === "candidate" ? "cand" : "near")
          + '" title="' + esc((r.estate.labels || []).join(", "))
          + '">' + (r.estate.kind === "candidate" ? "estate candidate" : "near miss")
          + "</span>" : "";
      tr.innerHTML = "<td>" + esc(r.subject) + " " + sig(r.significance) + est + resc +
        "</td><td class='preview clamp2'>" + recipients(r.participants) +
        "</td><td class='preview'>" + esc((r.date_last || "").slice(0, 10)) +
        "</td><td>" + num(r.message_count) + "</td>" +
        (EXAMINER ? "<td class='actcell'></td>" : "");
      tr.onclick = function () {
        go({ page: "emails", thread: r.thread_id }, { label: r.subject || "(no subject)" });
      };
      keyable(tr, null);
      if (EXAMINER) tr.lastChild.appendChild(emailDemoteBtn(r));
      tbl.appendChild(tr);
    });
    container.appendChild(tbl);
  }

  // The break-down strip inside a group: the dimensions you have NOT used yet,
  // each listing its subgroups with the server's count for this group. Clicking
  // one adds a filter rather than re-sorting the page, which is what keeps every
  // number on screen true — a heading can only claim what the server counted.
  function emailBreakdown(main, facets, active, reload) {
    var dims = [];
    if (!active.year) {
      dims.push({ key: "year", label: "Year", items: (facets.years || []).map(function (y) {
        return { label: y.year, count: y.count, q: { year: y.year }, crumb: y.year };
      }) });
    }
    if (!active.participant) {
      dims.push({ key: "who", label: "Person", items: (facets.correspondents || [])
        .slice(0, 24).map(function (c) {
          return { label: c.name || c.address, count: c.count,
                   q: { participant: c.address }, crumb: c.name || c.address };
        }) });
    }
    if (!active.band) {
      dims.push({ key: "band", label: "Significance", items: (facets.bands || []).map(function (b) {
        return { label: b.label, count: b.count, q: { band: String(b.n) }, crumb: b.label };
      }) });
    }
    if (!active.cat) {
      dims.push({ key: "cat", label: "Subject", items: (facets.categories || []).map(function (c) {
        return { label: emailCatLabel(c.name), count: c.count,
                 q: { cat: c.name }, crumb: emailCatLabel(c.name) };
      }) });
    }
    if (!Q.rescued && facets.rescued) {
      dims.push({ key: "rescued", label: "Audience", items: [
        { label: "Rescued — the family will not see these", count: facets.rescued,
          q: { rescued: "1" }, crumb: "Estate-rescued" }] });
    }
    if (!Q.estate && (facets.estate || []).length) {
      dims.push({ key: "estate", label: "Estate", items: facets.estate.map(function (e) {
        var lab = e.kind === "candidate" ? "Candidate for a vital document"
                                         : "Weaker match (near miss)";
        return { label: lab, count: e.count, q: { estate: e.kind }, crumb: lab };
      }) });
    }
    dims = dims.filter(function (d) { return d.items.length > 1; });
    if (!dims.length) return;

    var box = el("section", "ebd"); main.appendChild(box);
    var tabs = el("div", "ebd-tabs"); box.appendChild(tabs);
    tabs.appendChild(el("span", "ebd-lead", "Break this down by"));
    var body = el("div", "ebd-body"); box.appendChild(body);
    var open = Q.by || dims[0].key;
    if (!dims.some(function (d) { return d.key === open; })) open = dims[0].key;

    function draw() {
      body.innerHTML = "";
      var dim = dims.filter(function (d) { return d.key === open; })[0];
      Array.prototype.forEach.call(tabs.querySelectorAll("button"), function (b) {
        b.classList.toggle("on", b.dataset.key === open);
        b.setAttribute("aria-pressed", String(b.dataset.key === open));
      });
      dim.items.forEach(function (it) {
        var a = el("a", "ebd-chip");
        a.href = urlFor(mergeQ(it.q), { label: it.crumb });
        a.appendChild(el("span", "ebd-chip-l", esc(it.label)));
        a.appendChild(el("span", "ebd-chip-n", num(it.count)));
        body.appendChild(a);
      });
    }
    dims.forEach(function (d) {
      var b = el("button", "ebd-tab", d.label);
      b.dataset.key = d.key;
      b.onclick = function () { open = d.key; setQ({ by: d.key }); draw(); };
      tabs.appendChild(b);
    });
    draw();
  }

  // Current Emails query + an override, so a break-down link NARROWS rather than
  // replacing (picking a year inside "Major life events" must keep the band).
  function mergeQ(extra) {
    var out = { page: "emails" };
    ["band", "cat", "year", "participant", "q", "date_from", "date_to", "sort",
     "estate", "rescued"].forEach(function (k) { if (Q[k]) out[k] = Q[k]; });
    Object.keys(extra || {}).forEach(function (k) { out[k] = extra[k]; });
    return out;
  }

  // One group: the threads that match every active filter, the controls to sort
  // them, and the break-down strip to go a level deeper.
  function emailGroup(main, data, active) {
    // The heading names each active filter in the words the reader clicked. The
    // person is the awkward one: the filter keys on an ADDRESS, but the chip they
    // clicked carried a display name, and a heading that answers with a raw
    // mailbox address reads as a different thing entirely. The server's facets
    // carry the name, so prefer it and keep the address as the fallback for a
    // correspondent who never had one.
    function titleFrom(facets) {
      var parts = [];
      if (active.band) {
        var bl = ((facets || {}).bands || []).filter(function (b) {
          return String(b.n) === active.band; })[0];
        parts.push(bl ? bl.label : "Significance " + active.band);
      }
      if (active.cat) parts.push(emailCatLabel(active.cat));
      if (active.year) parts.push(active.year);
      if (active.participant) {
        var who = ((facets || {}).correspondents || []).filter(function (c) {
          return c.address === active.participant; })[0];
        parts.push("with " + ((who && who.name) || active.participant));
      }
      if (Q.estate) {
        parts.push(Q.estate === "candidate" ? "estate candidates"
                                            : "estate near misses");
      }
      if (Q.rescued) parts.push("estate-rescued");
      return parts.length ? parts.join(" · ") : "All emails";
    }
    var title = titleFrom(data.facets);
    setCrumb(Q.crumb || title);

    var controls = head(main, "Emails", title, num(data.total || 0) + " conversations.");
    var back = el("button", "btn chip", "← All emails");
    back.onclick = function () { go({ page: "emails" }); };
    controls.appendChild(back);

    var listctrls = el("div", "filterbar"); main.appendChild(listctrls);
    var search = el("input", "listsearch"); search.type = "search";
    search.placeholder = "Find by subject or person…"; search.autocomplete = "off";
    search.setAttribute("aria-label", "Find emails");
    if (Q.q) search.value = Q.q;
    var dFrom = el("input"); dFrom.type = "date"; dFrom.title = "From date";
    var dTo = el("input"); dTo.type = "date"; dTo.title = "To date";
    if (Q.date_from) dFrom.value = Q.date_from;
    if (Q.date_to) dTo.value = Q.date_to;
    var sortSel = el("select"); sortSel.setAttribute("aria-label", "Sort emails");
    [["", "Most significant"], ["recent", "Newest first"], ["oldest", "Oldest first"],
     ["subject", "Subject (A–Z)"]]
      .forEach(function (o) { sortSel.appendChild(new Option(o[1], o[0])); });
    if (Q.sort) sortSel.value = Q.sort;
    listctrls.appendChild(search);
    listctrls.appendChild(el("span", "flabel", "From")); listctrls.appendChild(dFrom);
    listctrls.appendChild(el("span", "flabel", "To")); listctrls.appendChild(dTo);
    listctrls.appendChild(sortSel);

    var bdHolder = el("div"); main.appendChild(bdHolder);
    var body = el("div"); main.appendChild(body);
    var pg = pager("/api/emails", {
      getParams: function () {
        var p = {};
        ["band", "cat", "year", "participant"].forEach(function (k) {
          if (active[k]) p[k] = active[k];
        });
        if (Q.estate) p.estate = Q.estate;
        if (Q.rescued) p.rescued = Q.rescued;
        if (search.value) p.q = search.value;
        if (dFrom.value) p.date_from = dFrom.value;
        if (dTo.value) p.date_to = dTo.value;
        if (sortSel.value) p.sort = sortSel.value;
        return p;
      },
      render: function (all) { emailThreadTable(body, all); },
      // The lead and the break-down are both written from the SERVER's numbers for
      // the filter actually in force — never from the rows that happen to be
      // loaded, which is the mistake the old page made in every band heading.
      onData: function (raw) {
        var leadEl = main.querySelector(".pagehead-title .lead");
        if (leadEl) leadEl.textContent = num((raw && raw.total) || 0) + " conversations.";
        // The seed payload is unfiltered, so the person's display name is only
        // knowable once the filtered facets arrive. Rewrite the heading then.
        var h1 = main.querySelector(".pagehead-title h1");
        if (h1) h1.textContent = titleFrom((raw && raw.facets) || {});
        bdHolder.innerHTML = "";
        emailBreakdown(bdHolder, (raw && raw.facets) || {}, active, function () {
          pg.load(true);
        });
      },
    });
    main.appendChild(pg.footer);
    var reload = debounce(function () { setQ({ q: search.value }); pg.load(true); }, 250);
    search.oninput = reload;
    function onDate() { setQ({ date_from: dFrom.value, date_to: dTo.value }); pg.load(true); }
    dFrom.onchange = onDate; dTo.onchange = onDate;
    sortSel.onchange = function () { setQ({ sort: sortSel.value }); pg.load(true); };
    pg.load(true);
  }

  P.emails = function (main, data) {
    // Thread detail when ?thread=ID is present.
    if (Q.thread) {
      backLink(main, "All emails", { page: "emails" });
      var holder = el("div", "reading emthread"); main.appendChild(holder);
      getJSON("/api/email/thread?id=" + encodeURIComponent(Q.thread)).then(function (t) {
        setCrumb(t.subject || "(no subject)");
        var ctrls = head(holder, "Emails", t.subject || "(no subject)", (t.messages || []).length + " message(s).");
        if (EXAMINER) ctrls.appendChild(emailDemoteBtn({ thread_id: Q.thread, subject: t.subject, demoted: t.demoted }));
        drawThreadMessages(holder, t);
      }).catch(function (e) { holder.appendChild(el("p", "notice", "Couldn't load thread: " + esc(e.message))); });
      return;
    }
    var active = {
      band: (Q.band != null && Q.band !== "") ? String(Q.band) : "",
      cat: Q.cat || "", year: Q.year || "", participant: Q.participant || "",
    };
    var narrowed = !!(active.band || active.cat || active.year || active.participant
                      || Q.q || Q.date_from || Q.date_to || Q.sort || Q.estate || Q.rescued);
    if (narrowed) return emailGroup(main, data, active);
    return emailIndex(main, data);
  };

  // ── correspondents / relationships (G-6) ──
  // One correspondent card. Balance-bar widths are set via CSSOM (.style.width),
  // not inline style="" attributes, because the page CSP is style-src 'self'. All
  // estate-derived text (name, address) is esc()'d; the address is encodeURIComponent'd
  // into the Emails deep-link.
  function correspondentCard(c) {
    var addr = c.address || "";
    var name = c.name || addr;
    var y0 = String(c.first_seen || "").slice(0, 4), y1 = String(c.last_seen || "").slice(0, 4);
    var span = (y0 && y1) ? (y0 === y1 ? y0 : (y0 + "–" + y1)) : "";
    var card = el("div", "corrcard clickable");
    card.innerHTML =
      "<div class='cc-name'>" + esc(name) +
        (c.bidirectional ? " <span class='cc-badge' title='Two-way correspondence'>⇄</span>" : "") + "</div>" +
      (name !== addr ? "<div class='cc-addr'>" + esc(addr) + "</div>" : "") +
      "<div class='cc-meta'>" + (span ? esc(span) + " · " : "") + num(c.total) + " messages</div>";
    var sent = c.sent || 0, received = c.received || 0, tot = sent + received;
    var bar = el("div", "cc-bar");
    var sSeg = el("span", "cc-sent"), rSeg = el("span", "cc-recv");
    sSeg.style.width = (tot ? sent / tot * 100 : 0) + "%";
    rSeg.style.width = (tot ? received / tot * 100 : 0) + "%";
    bar.appendChild(sSeg); bar.appendChild(rSeg);
    card.appendChild(bar);
    card.appendChild(el("div", "cc-bal", "Sent " + num(sent) + " · Received " + num(received)));
    // The card is the only place that knows this address is "Alex Rendon",
    // so it names the destination crumb; Emails itself only ever sees the address.
    card.onclick = function () { go({ page: "emails", participant: addr }, { label: name }); };
    keyable(card, "button", "Correspondence with " + name);   // F-12
    return card;
  }

  // P2 #9: one possible-duplicate-identity suggestion card (examiner-only).
  // `onResolved()` re-fetches both the suggestion panel and the correspondent
  // grid after either verb succeeds, so a confirmed merge's combined stats
  // and the shrunk suggestion list both show immediately.
  function duplicateSuggestionCard(c, onResolved) {
    var addrs = c.addresses || [];
    var card = el("div", "dupecard");
    card.innerHTML = "<div class='cc-name'>" + esc(c.name || "") + "</div>" +
      "<p class='dupe-hint'>These addresses look like the same person — review and confirm.</p>";
    var list = el("ul", "dupe-addrs");
    addrs.forEach(function (a) {
      list.appendChild(el("li", null,
        "<span class='cc-addr'>" + esc(a.address || "") + "</span>" +
        " <span class='dupe-total'>" + num(a.total || 0) + " messages" +
        (a.bidirectional ? " · two-way" : "") + "</span>"));
    });
    card.appendChild(list);
    var row = el("div", "qrow");
    var addresses = addrs.map(function (a) { return a.address; });
    var yes = el("button", "btn primary", "Same person — merge");
    yes.onclick = function () {
      yes.disabled = true; no.disabled = true;
      doVerb("/api/correspondent/merge", { addresses: addresses }, "Merged").then(function (res) {
        if (res) onResolved(); else { yes.disabled = false; no.disabled = false; }
      });
    };
    var no = el("button", "btn", "Not the same person");
    no.onclick = function () {
      yes.disabled = true; no.disabled = true;
      doVerb("/api/correspondent/reject", { addresses: addresses }, "Dismissed").then(function (res) {
        if (res) onResolved(); else { yes.disabled = false; no.disabled = false; }
      });
    };
    row.appendChild(yes); row.appendChild(no);
    card.appendChild(row);
    return card;
  }

  P.correspondents = function (main, data) {
    head(main, "Correspondents", "Correspondents",
      num((data && data.total) || 0) + " people the owner exchanged email with — most frequent first. Open one to see those emails.");
    // #11: in-list search (name/address) + sort — thousands of correspondents
    // with no way to narrow the list was close to unusable for "find person X"
    // without falling back to full-text search first.
    var controls = el("div", "filterbar");
    var search = el("input", "listsearch"); search.type = "search";
    search.placeholder = "Find a correspondent…"; search.autocomplete = "off";
    search.setAttribute("aria-label", "Find a correspondent");
    if (Q.q) search.value = Q.q;
    var sort = el("select"); sort.setAttribute("aria-label", "Sort correspondents");
    [["", "Most messages"], ["name", "Name (A–Z)"], ["recent", "Most recent"]]
      .forEach(function (o) { sort.appendChild(new Option(o[1], o[0])); });
    if (Q.sort) sort.value = Q.sort;
    controls.appendChild(search); controls.appendChild(sort);
    main.appendChild(controls);
    var grid = el("div", "corrgrid"); main.appendChild(grid);
    var pg = pager("/api/correspondents", {
      getParams: function () { return { q: search.value, sort: sort.value }; },
      render: function (all) {
        grid.innerHTML = "";
        if (!all.length) {
          grid.appendChild(el("p", "notice",
            search.value ? "No correspondents match “" + search.value + "”." : "No correspondents in this case."));
          return;
        }
        all.forEach(function (c) { grid.appendChild(correspondentCard(c)); });
      },
    });
    var reload = debounce(function () { setQ({ q: search.value }); pg.load(true); }, 250);
    search.oninput = reload;
    sort.onchange = function () { setQ({ sort: sort.value }); pg.load(true); };
    if (EXAMINER) {
      // Suggestions render ABOVE the grid (inserted before it was appended to
      // main), so the panel appears first while still using pg to refresh the
      // grid once a merge is confirmed.
      var dupePanel = el("div", "dupepanel");
      main.insertBefore(dupePanel, grid);
      var loadDupes = function () {
        getJSON("/api/correspondent-duplicates").then(function (cands) {
          dupePanel.innerHTML = "";
          if (!cands || !cands.length) return;
          dupePanel.appendChild(el("h3", null, "Possible duplicate identities"));
          cands.forEach(function (c) {
            dupePanel.appendChild(duplicateSuggestionCard(c, function () {
              loadDupes();
              pg.load(true);
            }));
          });
        }).catch(function () { /* non-fatal: the grid still works without suggestions */ });
      };
      loadDupes();
    }
    main.appendChild(pg.footer);
    pg.seed(data);
  };

  // ── messages (SMS / chat / voicemail conversations) ──
  // Human duration for a call/voicemail event (seconds → "1m 36s" / "42s").
  function callDuration(sec) {
    var n = parseInt(sec, 10);
    if (isNaN(n) || n <= 0) return "";
    var m = Math.floor(n / 60), s = n % 60;
    return m ? (m + "m " + s + "s") : (s + "s");
  }
  // Conversation title. Prefer the server's display_name — the same string the
  // Messages LIST shows (a resolved contact, or a group title composed from
  // members). Joining participants here instead made one thread read as
  // "Alex Rendon, Brian Okafor, Casey Lindqvist + 1" in the list and
  // "Alex Rendon (+15035550178), Brian Okafor (+15035550179), ..." one click
  // later. Falls back to the join for payloads without it (older cases).
  // Estate-derived → escape at the sink; here we build a plain string, esc'd by the
  // head() helper which esc()s its title.
  function conversationTitle(c) {
    if (c && c.display_name) return c.display_name;
    var others = (c.participants || []).filter(function (p) { return p && p !== "owner"; });
    return others.join(", ") || "(conversation)";
  }
  // One attachment inside a bubble. Estate-derived name → esc()/textContent only;
  // the servable src goes through mediaURL/thumbURL (which encodeURIComponent it) set
  // as a DOM property, never interpolated into an HTML string. src null → name-only.
  // Typed placeholder for an attachment we cannot serve. "Not recovered" is
  // the wrong words for ALL of them: an iMessage app payload (link preview,
  // sticker, Apple Pay) never had a media file, so reporting it as missing
  // invents a loss. A real photo the case does not contain IS a genuine gap and
  // should say so plainly, naming what it was. Server sets `kind` (see
  // attachment_kind); unknown/absent kind falls back to the neutral wording.
  var ATT_MISSING = {
    app_payload: "link preview or app content — no file to show",
    image: "photo not in this archive",
    video: "video not in this archive",
    document: "document not in this archive"
  };
  function messageAttachment(a) {
    var name = (a && a.name) || "attachment";
    var kindKey = (a && a.kind) || "unknown";
    // An app payload is described the same way whether or not its blob happens
    // to have survived into the case, because the blob is not the point: the
    // link preview's URL is already in the message text, and the payload
    // itself is an opaque binary. Rendering the resolved ones as prominent
    // links titled with a raw UUID promised a family reader something real and
    // handed them a binary download — and put two spellings of the identical
    // content in one bubble. The bytes stay reachable for the examiner role
    // through a discreet trailing link.
    if (kindKey === "app_payload") {
      var w = el("span", "matt noatt att-app_payload");
      w.appendChild(document.createTextNode("📎 " + ATT_MISSING.app_payload));
      w.title = name;
      if (a && a.src && EXAMINER) {
        var raw = el("a", "attraw");
        raw.href = mediaURL(a.src);       // already encoded by mediaURL
        raw.textContent = "raw";
        raw.title = name;
        raw.onclick = function (e) { e.stopPropagation(); };
        w.appendChild(document.createTextNode(" "));
        w.appendChild(raw);
      }
      return w;
    }
    if (!a || !a.src) {
      var s = el("span", "matt noatt att-" + kindKey);
      var note = ATT_MISSING[kindKey] || "not recovered";
      s.textContent = "📎 " + name + " (" + note + ")";
      s.title = name;
      return s;
    }
    var kind = mediaKind(a.src);
    if (kind === "image") {
      var wrap = el("div", "matt");
      var im = el("img"); lazyThumb(im, a.src); im.alt = name;
      im.title = name;
      im.onclick = function () { lightbox(a.src, true); };
      wrap.appendChild(im);
      return wrap;
    }
    var link = el("a", "matt attlink");
    link.href = mediaURL(a.src);            // DOM property, already encoded by mediaURL
    link.textContent = "📎 " + name;
    link.onclick = function (e) { e.preventDefault(); lightbox(a.src, mediaKind(a.src)); };
    return link;
  }

  // Redraw the conversation table from the accumulated rows (rebuilt per page).
  function drawMessagesTable(container, rows) {
    container.innerHTML = "";
    if (!rows.length) { container.appendChild(el("p", "notice", "No messages in this case.")); return; }
    var tbl = el("table");
    tbl.innerHTML = "<tr><th>With</th><th>Platform</th><th>When</th><th>Messages</th><th>Calls</th></tr>";
    rows.forEach(function (r) {
      var span = r.span || [null, null];
      var when = String(span[0] || "").slice(0, 10);
      if (span[1] && span[1] !== span[0]) when += " – " + String(span[1]).slice(0, 10);
      // Participants beyond the named other party (group chats), owner dropped.
      var others = (r.participants || []).filter(function (p) { return p && p !== "owner"; });
      var extra = others.length > 1 ? recipients(others) : "";
      var tr = el("tr", "clickable");
      // display_name / platform / participants are all estate-derived → esc() every sink.
      tr.innerHTML = "<td>" + esc(r.display_name || "(conversation)") +
        (extra ? "<div class='preview clamp2'>" + extra + "</div>" : "") + "</td>" +
        "<td class='preview'>" + esc(pretty(r.platform || "")) + "</td>" +
        "<td class='preview'>" + esc(when) + "</td>" +
        "<td>" + num(r.message_count) + "</td>" +
        "<td>" + (r.call_event_count ? num(r.call_event_count) : "") + "</td>";
      tr.onclick = function () {
        go({ page: "messages", conversation: r.conversation_id },
           { label: conversationTitle(r) || "Conversation" });
      };
      keyable(tr, null);   // F-12
      tbl.appendChild(tr);
    });
    container.appendChild(tbl);
  }

  P.messages = function (main, data) {
    // Conversation transcript when ?conversation=ID is present. Mirrors P.emails'
    // thread-detail view (lazy per-conversation load, back link, notice on failure).
    if (Q.conversation) {
      backLink(main, "All messages", { page: "messages" });
      var holder = el("div", "reading msgthread"); main.appendChild(holder);
      getJSON("/api/message/conversation?id=" + encodeURIComponent(Q.conversation)).then(function (c) {
        if (!c) { holder.appendChild(el("p", "notice", "This conversation could not be found.")); return; }
        setCrumb(conversationTitle(c));
        head(holder, "Messages · " + pretty(c.platform || ""), conversationTitle(c),
          (c.messages || []).length + " message(s)" +
          ((c.call_events || []).length ? " · " + c.call_events.length + " call(s)" : "") + ".");
        drawConversationStream(holder, c);
      }).catch(function (e) { holder.appendChild(el("p", "notice", "Couldn't load conversation: " + esc(e.message))); });
      return;
    }
    head(main, "Messages", "Messages", num((data && data.total) || 0) + " conversations.");
    var body = el("div"); main.appendChild(body);
    var pg = pager("/api/messages", { render: function (all) { drawMessagesTable(body, all); } });
    main.appendChild(pg.footer);
    pg.seed(data);
  };

  // G-3: format seconds → "m:ss" for a segment/timeline label.
  function clockTime(sec) {
    var n = Math.max(0, Math.floor(Number(sec) || 0));
    var m = Math.floor(n / 60), s = n % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  // G-3: the seek-synced transcript detail for ONE recording. Fetches
  // /api/transcript?id=<file> (segments + timings, containment-checked server-side),
  // renders an <audio preload="metadata"> plus clickable segments; clicking a segment
  // sets audio.currentTime to its start and the currently-playing segment highlights
  // via 'timeupdate'. Degrades to the plain transcript_text when no timing is
  // available, and drops the player when the audio itself was reaped (has_audio:false).
  function recordingDetail(main, file) {
    backLink(main, "All recordings", { page: "recordings" });
    var holder = el("div", "reading recdetail"); main.appendChild(holder);
    getJSON("/api/transcript?id=" + encodeURIComponent(file)).then(function (d) {
      if (!d) { holder.appendChild(el("p", "notice", "This recording could not be found.")); return; }
      var segs = d.segments || [];
      var lead = [];
      if (d.duration != null) lead.push(clockTime(d.duration));
      if (d.language) lead.push(esc(pretty(String(d.language))));
      setCrumb(cleanRecordingName(basename(file)));
      head(holder, "Recording", cleanRecordingName(basename(file)), lead.join(" · "));

      // Player — preload="metadata" on the OPENED detail (the LIST stays preload="none").
      var audio = null;
      if (d.has_audio) {
        audio = el("audio");
        audio.controls = true;
        audio.preload = "metadata";
        audio.src = mediaURL(file);            // DOM property, already encoded by mediaURL
        holder.appendChild(audio);
      } else {
        holder.appendChild(el("p", "notice",
          "The audio for this recording is not available; showing the transcript only."));
      }

      if (segs.length) {
        // Clickable, seek-synced segments.
        var list = el("div", "segments");
        var segEls = [];
        segs.forEach(function (s) {
          var seg = el("div", "segment");
          seg.appendChild(el("span", "segtime", esc(clockTime(s.start))));
          seg.appendChild(el("span", "segtext", esc(s.text || "")));
          if (audio) {
            seg.classList.add("seekable");
            seg.onclick = function () {
              try { audio.currentTime = Number(s.start) || 0; audio.play(); } catch (e) {}
            };
          }
          seg._start = Number(s.start) || 0;
          seg._end = (s.end != null ? Number(s.end) : seg._start);
          segEls.push(seg);
          list.appendChild(seg);
        });
        holder.appendChild(list);
        if (audio) {
          // Highlight the segment covering the playhead as it moves.
          var active = -1;
          audio.addEventListener("timeupdate", function () {
            var t = audio.currentTime, idx = -1;
            for (var i = 0; i < segEls.length; i++) {
              if (t >= segEls[i]._start && t < segEls[i]._end) { idx = i; break; }
              if (t >= segEls[i]._start) idx = i;   // fall back to last-started segment
            }
            if (idx === active) return;
            if (active >= 0) segEls[active].classList.remove("playing");
            active = idx;
            if (active >= 0) segEls[active].classList.add("playing");
          });
        }
      } else if (d.transcript_text) {
        // Degrade: no per-segment timing (or sidecar reaped) → plain transcript.
        holder.appendChild(el("div", "body transcript-plain", esc(d.transcript_text)));
      } else {
        holder.appendChild(el("p", "notice", "No transcript is available for this recording."));
      }
    }).catch(function (e) {
      holder.appendChild(el("p", "notice", "Couldn't load transcript: " + esc(e.message)));
    });
  }

  // ── Estate document report (attorney-facing) ──
  // Everything this page is for turns on one distinction the rest of the UI
  // blurs: "we searched and it is not there" and "we have not finished
  // searching" look identical on screen — both are an empty row — and only the
  // first supports advice. So the three states are named, the weakest one is
  // hard to qualify for, and the limitations are stated up front rather than in
  // a footnote, because a reader who stops halfway must not stop having been
  // misled.
  // A label / count / proportion row, no link. Same visual grammar as the Emails
  // index so a count means the same thing everywhere; bar widths go through
  // CSSOM because the page CSP forbids an inline style attribute.
  function reportBars(container, items) {
    var max = (items || []).reduce(function (m, i) { return Math.max(m, i.count); }, 0);
    (items || []).forEach(function (i) {
      var row = el("div", "eix-row rep-row");
      row.appendChild(el("span", "eix-label", esc(i.label)));
      // `display` lets a caller show a formatted figure (a file size) while the
      // bar is still sized by the underlying number.
      row.appendChild(el("span", "eix-count", i.display ? esc(i.display) : num(i.count)));
      var track = el("span", "eix-bar"), fill = el("span", "eix-fill");
      fill.style.width = Math.max(2, Math.round((i.count / (max || 1)) * 100)) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      container.appendChild(row);
    });
  }

  function reportHead(main, title, d) {
    var controls = head(main, "Reports", title,
      "Case " + esc((d && d.case_id) || "") + " · prepared "
      + esc((d && d.generated_at) || ""));
    var back = el("button", "btn chip", "← All reports");
    back.onclick = function () { go({ page: "reports" }); };
    controls.appendChild(back);
    var pr = el("button", "btn", "Print / Save as PDF");
    pr.onclick = function () { window.print(); };
    controls.appendChild(pr);
    return controls;
  }

  function reportLimits(main, list) {
    var lim = el("section", "rep-limits");
    lim.appendChild(el("h2", null, "What this report cannot tell you"));
    var ul = el("ul");
    (list || []).forEach(function (x) { ul.appendChild(el("li", null, esc(x))); });
    lim.appendChild(ul);
    main.appendChild(lim);
  }

  function reportDate(iso) {
    if (!iso) return "";
    var p = String(iso).slice(0, 10).split("-");
    if (p.length !== 3) return esc(iso);
    var M = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"];
    return (parseInt(p[2], 10) + " " + (M[parseInt(p[1], 10) - 1] || "") + " " + p[0]);
  }

  // The family report: an orientation document for somebody who has just been
  // handed the archive, rather than a statement of position on it. Limitations
  // sit at the END here, unlike the estate report where they lead — that one is
  // acted on by a professional and its caveats change what the figures support;
  // this one is read by a family, and opening on what is missing would be a
  // strange way to hand somebody their own life back.
  function familyReport(main, d) {
    reportHead(main, "Family report", d);
    var span = d.span || {};
    if (span.from && span.to) {
      main.appendChild(el("p", "rep-span",
        "This archive covers " + esc(reportDate(span.from)) + " to "
        + esc(reportDate(span.to)) + "."));
    }

    var bar = el("div", "vstats rep-stats");
    (d.headline || []).forEach(function (h) { vitalStat(bar, h.count, h.label); });
    main.appendChild(bar);

    var ppl = d.people || {};
    var psec = el("section", "rep-group");
    psec.appendChild(el("h2", null, "People"));
    psec.appendChild(el("p", "rep-note",
      num(ppl.total || 0) + " people are recognised across the photographs, "
      + num(ppl.named || 0) + " of them named. They appear in "
      + num(d.places || 0) + " places."));
    if ((ppl.top || []).length) {
      var chips = el("div", "ebd-body");
      ppl.top.forEach(function (p) {
        var c = el("span", "ebd-chip");
        c.appendChild(el("span", "ebd-chip-l", esc(p.name)));
        c.appendChild(el("span", "ebd-chip-n", num(p.photos)));
        chips.appendChild(c);
      });
      psec.appendChild(chips);
    }
    main.appendChild(psec);

    (d.sections || []).forEach(function (sec) {
      if (!(sec.items || []).length) return;
      var el_ = el("section", "rep-group");
      el_.appendChild(el("h2", null, esc(sec.label)));
      if (sec.note) el_.appendChild(el("p", "rep-note", esc(sec.note)));
      var panel = el("div", "rep-bars");
      reportBars(panel, sec.items);
      el_.appendChild(panel);
      main.appendChild(el_);
    });

    reportLimits(main, d.limitations);
  }

  // The pipeline report: what was examined to build this archive, and how much
  // of it survived to be looked at again. The interesting column is neither the
  // input nor the output but the GAP, and it is a different story in every row —
  // photographs mostly lost to duplicates, mail to bulk triage — so each row
  // carries a sentence saying where its difference went.
  function pipelineReport(main, d) {
    reportHead(main, "Pipeline report", d);
    var t = d.totals || {}, sz = d.size || {}, run = d.run || {};

    var bar = el("div", "vstats rep-stats");
    vitalStat(bar, t.examined, "items examined");
    vitalStat(bar, t.surfaced, "browsable in the archive");
    bar.appendChild(el("p", "vstats-hint",
      "The archive occupies " + esc(sz.total_human || "—") + " across "
      + num(sz.files || 0) + " files"
      + (run.elapsed ? ", and the run that produced it spans " + esc(run.elapsed) : "")
      + "."));
    main.appendChild(bar);

    var sec = el("section", "rep-group");
    sec.appendChild(el("h2", null, "What was examined"));
    sec.appendChild(el("p", "rep-note",
      "Everything the pipeline read, against what the archive shows today."));
    if (d.surfaced_note) sec.appendChild(el("p", "rep-note", esc(d.surfaced_note)));
    var tbl = el("table", "rep-table pipe-table");
    tbl.innerHTML = "<tr><th>Material</th><th>Examined</th><th>In the archive</th>"
      + "<th>Share</th></tr>";
    (d.rows || []).forEach(function (r) {
      var tr = el("tr");
      tr.innerHTML = "<td>" + esc(r.kind) + "</td><td>" + num(r.examined)
        + "</td><td>" + num(r.surfaced) + "</td><td>"
        + (r.share == null ? "—" : esc(r.share + "%")) + "</td>";
      tbl.appendChild(tr);
      // The explanation rides directly under its row, spanning the table, so a
      // share of 38% is never read without the reason beside it.
      var note = el("tr", "pipe-note");
      var td = el("td", null,
        (r.unit_change ? '<em>' + esc(r.unit_change) + '.</em> ' : "") + esc(r.note));
      td.colSpan = 4;
      note.appendChild(td);
      tbl.appendChild(note);
    });
    sec.appendChild(tbl);
    main.appendChild(sec);

    var rd = d.reading || {}, ex = d.expansion || {};
    var work = el("section", "rep-group");
    work.appendChild(el("h2", null, "The work behind it"));
    var wl = el("ul", "pipe-list");
    if (ex.archives_found) wl.appendChild(el("li", null,
      num(ex.archives_found) + " archives and containers were unpacked, adding "
      + num(ex.files_added) + " files that were inside them — including "
      + num(ex.email_attachments) + " email attachments that would otherwise "
      + "never have been seen."));
    if (rd.documents_read) wl.appendChild(el("li", null,
      num(rd.documents_read) + " documents were read for text — by optical "
      + "character recognition where they were scans — and text was recovered "
      + "from " + num(rd.text_recovered) + " of them."));
    if (rd.audio_hours) wl.appendChild(el("li", null,
      esc(String(rd.audio_hours)) + " hours of audio were transcribed."));
    work.appendChild(wl);
    main.appendChild(work);

    // How much of all that mattered to the estate. This is the answer to "you
    // surfaced 21,988 conversations — so what?", and it is a startlingly small
    // number, which is the point of showing it.
    var est = d.estate;
    if (est) {
      var esec = el("section", "rep-group");
      esec.appendChild(el("h2", null, "How much of it mattered to the estate"));
      esec.appendChild(el("p", "rep-note",
        "Browsable is not the same as important. Of everything in the archive, "
        + "this is what the estate scan flagged — and what still has no answer."));
      // A funnel, ending on the number somebody actually needs: how many
      // decisions are still outstanding. That was previously reachable only by
      // opening the Documents screen and reading a stat bar.
      var fun = el("div", "vstats rep-stats");
      // All four are DECISIONS, not documents. Mixing the two here would be the
      // same unit error the email row refuses to make: signed-off and undecided
      // are counts of answers, and a document that is a candidate for two types
      // can be answered for one and not the other. The distinct-document figures
      // are in the table directly below.
      vitalStat(fun, (est.candidates || {}).decisions, "candidates to decide");
      vitalStat(fun, est.decided, "signed off");
      vitalStat(fun, est.undecided, "still undecided", est.undecided > 0);
      vitalStat(fun, (est.near_misses || {}).decisions, "weaker matches unreviewed",
                ((est.near_misses || {}).decisions || 0) > 0);
      fun.appendChild(el("p", "vstats-hint",
        "Counts of decisions. They cover "
        + num((est.candidates || {}).documents) + " distinct documents and "
        + num((est.near_misses || {}).documents) + " weaker matches — fewer than "
        + "the decisions, because a document can be a candidate for more than "
        + "one type and each pairing needs its own answer."));
      esec.appendChild(fun);
      var et = el("table", "rep-table");
      et.innerHTML = "<tr><th>&nbsp;</th><th>Decisions</th><th>Documents</th>"
        + "<th>Of those, emails</th></tr>";
      [["Candidates for a vital document", est.candidates],
       ["Weaker matches (near misses)", est.near_misses]].forEach(function (pair) {
        var r = pair[1] || {};
        var tr = el("tr");
        tr.innerHTML = "<td>" + esc(pair[0]) + "</td><td>" + num(r.decisions)
          + "</td><td>" + num(r.documents) + "</td><td>"
          + num(r.from_mail_documents) + "</td>";
        et.appendChild(tr);
      });
      esec.appendChild(et);
      // Two units in one table, so both get named rather than left to be
      // inferred: one document can be a candidate for several types and each
      // pairing is its own decision.
      esec.appendChild(el("p", "rep-note",
        "A document can be a candidate for more than one type, and each pairing "
        + "needs its own answer — so the decisions outnumber the documents. "
        + "Only " + num((est.candidates || {}).from_mail_documents)
        + " emails became candidates and "
        + num((est.near_misses || {}).from_mail_documents)
        + " more were weaker matches: "
        + (est.candidate_mail_share == null ? "—"
           : esc(est.candidate_mail_share + "%")) + " and "
        + (est.near_mail_share == null ? "—" : esc(est.near_mail_share + "%"))
        + " of the " + num(est.mail_denominator) + " "
        + esc(est.mail_denominator_label) + ". The share is taken against "
        + "messages rather than conversations, because these counts are "
        + "individual emails and a conversation is a group of them."
        + (est.per_target_k
           ? " Retrieval stopped at " + num(est.per_target_k)
             + " candidates per document type, so both figures are a floor." : "")));
      main.appendChild(esec);
    }

    var ssec = el("section", "rep-group");
    ssec.appendChild(el("h2", null, "Size on disk"));
    ssec.appendChild(el("p", "rep-note",
      "The delivered archive as it sits on this machine. The material it was "
      + "built from is much larger and is not here."));
    var sbars = el("div", "rep-bars");
    reportBars(sbars, (sz.parts || []).map(function (p) {
      return { label: p.name, count: p.bytes, display: p.human };
    }));
    ssec.appendChild(sbars);
    main.appendChild(ssec);

    if ((run.stages || []).length) {
      var rsec = el("section", "rep-group");
      rsec.appendChild(el("h2", null, "The run"));
      // Say what this measurement IS. These are the completion times each stage
      // recorded, so the span includes any time the machine sat idle between
      // them, and a stage that was re-run appears where it actually happened
      // rather than in pipeline order. Calling it "processing time" would be a
      // stronger claim than the data supports.
      rsec.appendChild(el("p", "rep-note",
        "When each stage finished. The span between the first and the last is "
        + (run.elapsed ? esc(run.elapsed) + ", " : "") + "wall clock — it "
        + "includes any time the machine spent idle between stages, and is not "
        + "a measure of how much computing was done."));
      var rt = el("table", "rep-table");
      rt.innerHTML = "<tr><th>Stage</th><th>Finished</th></tr>";
      run.stages.forEach(function (st) {
        var tr = el("tr");
        tr.innerHTML = "<td>" + esc(pretty(st.stage || "")) + "</td><td>"
          + esc(String(st.at).replace("T", " ").slice(0, 19)) + "</td>";
        rt.appendChild(tr);
      });
      rsec.appendChild(rt);
      main.appendChild(rsec);
    }
  }

  P.reports = function (main, d) {
    // The router seeds every page from /api/<page> with NO query string, so the
    // payload above is always the index. A specific report has to fetch itself —
    // same as the recording and email-thread detail views do.
    var RENDER = { family: familyReport, estate: estateReport,
                   pipeline: pipelineReport };
    var LABEL = { family: "Family report", estate: "Estate document report",
                  pipeline: "Pipeline report" };
    if (RENDER[Q.r]) {
      var which = Q.r;
      setCrumb(LABEL[which]);
      var holder = el("div"); main.appendChild(holder);
      getJSON("/api/reports?r=" + encodeURIComponent(which)).then(function (rep) {
        holder.innerHTML = "";
        RENDER[which](holder, rep);
      }).catch(function (e) {
        holder.appendChild(el("p", "notice",
          "Couldn't build the report: " + esc(e.message)));
      });
      return;
    }
    // The index.
    head(main, "Reports", "Reports",
      "Statements drawn from the archive, meant to be printed and handed over.");
    var wrap = el("div", "eix-cols"); main.appendChild(wrap);
    ((d && d.reports) || []).forEach(function (r) {
      var card = el("a", "eix-panel rep-card");
      card.href = urlFor({ page: "reports", r: r.key }, { label: r.label });
      card.appendChild(el("h2", null, esc(r.label)));
      card.appendChild(el("p", "eix-note", esc(r.note)));
      wrap.appendChild(card);
    });
  };

  function estateReport(main, d) {
    if (!d || d.available === false) {
      reportHead(main, "Estate document report", d);
      main.appendChild(el("p", "notice",
        "The vital-document scan has not run for this case, so there is nothing "
        + "to report on yet."));
      return;
    }
    var t = d.totals;
    reportHead(main, "Estate document report", d);

    // The headline, in the terms an attorney reads: of the types an estate
    // needs, how many do we actually have.
    var bar = el("div", "vstats rep-stats");
    vitalStat(bar, t.present, "types confirmed present");
    vitalStat(bar, t.unconfirmed, "types not yet established", t.unconfirmed > 0);
    vitalStat(bar, t.absent, "types with nothing matched");
    bar.appendChild(el("p", "vstats-hint",
      "Across " + num(t.types) + " document types an estate needs. "
      + num(t.candidates) + " candidate documents were found, of which "
      + num(t.signed_off) + " have been confirmed and " + num(t.undecided)
      + " are still unreviewed, alongside " + num(t.near_misses)
      + " weaker matches that have not been reviewed."));
    main.appendChild(bar);

    // Limitations BEFORE the table, deliberately, and unlike the family report:
    // this one is acted on by a professional, so a reader who takes in only the
    // first screen must leave with the caveat rather than the figure alone.
    reportLimits(main, d.limitations);

    (d.groups || []).forEach(function (g) {
      var sec = el("section", "rep-group rep-" + g.key);
      sec.appendChild(el("h2", null, esc(g.label) + " (" + num((g.types || []).length) + ")"));
      sec.appendChild(el("p", "rep-note", esc(g.note)));
      if (!(g.types || []).length) {
        sec.appendChild(el("p", "rep-empty", "None."));
        main.appendChild(sec);
        return;
      }
      var tbl = el("table", "rep-table");
      tbl.innerHTML = "<tr><th>Document type</th><th>Found</th><th>Confirmed</th>"
        + "<th>Unreviewed</th><th>Weaker matches</th></tr>";
      g.types.forEach(function (r) {
        var tr = el("tr");
        // The cap marker rides on the row it qualifies, not only in the preamble:
        // for a capped type "nothing matched" means "nothing within what we
        // retrieved", which is a materially weaker claim.
        var name = esc(r.label) + (r.capped
          ? ' <span class="rep-cap" title="Retrieval reached its limit for this '
            + 'type — these counts are a floor">capped</span>' : "");
        tr.innerHTML = "<td>" + name + "</td><td>" + num(r.candidates) + "</td><td>"
          + num(r.signed_off) + "</td><td>" + num(r.undecided) + "</td><td>"
          + num(r.near_misses) + "</td>";
        tbl.appendChild(tr);
      });
      sec.appendChild(tbl);
      main.appendChild(sec);
    });
  }

  // ── Recordings: grouped by what kind of listening it is ──
  // 1,051 recordings used to render as one flat significance-sorted list, every
  // row carrying its own <audio> element, with no way to ask for the voicemails.
  // The classifier's category was on every row and on /api/overview as
  // `audio_counts`, and the page used neither.
  //
  // The grouping (and its reasoning) lives server-side in _archive_data.AUDIO_KINDS
  // where pytest can hold it; the page reads `kind` / `kind_label` off each row.
  // Counting here is honest because /api/recordings is not paginated — the client
  // has every row, so a count over them IS the total.
  var AUDIO_KIND_ORDER = ["voicemail", "voice_note", "conversation", "music",
                          "untranscribed", "other"];

  function recordingCard(r) {
    var it = el("div", "item card-row");
    addPick(it, r.file);
    var affordance = r.has_transcript
      ? ' <span class="tchip" title="Transcript available">📝 transcript</span>' : "";
    var body = el("div", "ibody",
      "<h3>" + esc(r.name) + " " + sig(r.significance) + affordance + "</h3>" +
      (r.summary ? '<div class="why">' + esc(r.summary) + "</div>" : "") +
      '<audio controls preload="none" src="' + mediaURL(r.file) + '"></audio>' +
      (r.preview ? '<div class="body">' + esc(r.preview) + "</div>" : ""));
    // What the classifier ACTUALLY said, for the examiner, beside where we filed
    // it. The two disagree often enough to matter: recordings that exist in two
    // file formats get two different answers about a third of the time, so a
    // reader who can only see the group cannot tell a real grouping from a coin
    // toss. Family sees the group only — this is a confidence signal, not content.
    if (EXAMINER && r.category) {
      body.appendChild(el("div", "rec-cat",
        "Classified as " + esc(pretty(r.category))));
    }
    var actrow = el("div", "actrow");
    var open = el("button", "btn small", "Open transcript");
    open.onclick = function () {
      go({ page: "recordings", rec: r.file },
         { label: cleanRecordingName(basename(r.file)) });
    };
    actrow.appendChild(open);
    var exp = el("button", "btn small", "Export");
    exp.onclick = function () { doVerb("/api/export", { items: [r.file] }, "Exported " + r.name); };
    actrow.appendChild(exp);
    if (EXAMINER) {
      var disc = el("button", "btn small danger", "Discard");
      disc.onclick = function () {
        doVerb("/api/banish", { src: r.file }, "Discarded").then(function (x) { if (x) it.remove(); });
      };
      actrow.appendChild(disc);
    }
    body.appendChild(actrow);
    it.appendChild(body);
    return it;
  }

  P.recordings = function (main, rows) {
    // Recording detail (seek-synced player + transcript) when ?rec=<file> is present.
    if (Q.rec) { recordingDetail(main, Q.rec); return; }
    rows = rows || [];

    // Count every kind present, in the server's order. A kind with nothing in it
    // is not offered — an empty group is a dead click.
    var byKind = {};
    rows.forEach(function (r) {
      var k = r.kind || "other";
      (byKind[k] = byKind[k] || []).push(r);
    });
    var kinds = AUDIO_KIND_ORDER.filter(function (k) { return (byKind[k] || []).length; });
    // Any kind the server invented that this list has not heard of still shows,
    // after the known ones, rather than silently vanishing from the page.
    Object.keys(byKind).forEach(function (k) {
      if (kinds.indexOf(k) === -1) kinds.push(k);
    });
    function labelFor(k) {
      var first = (byKind[k] || [])[0];
      return (first && first.kind_label) || pretty(k);
    }

    var active = Q.kind && byKind[Q.kind] ? Q.kind : "";
    if (active) setCrumb(Q.crumb || labelFor(active));
    head(main, "Recordings", active ? labelFor(active) : "Recordings",
      active ? num(byKind[active].length) + " recordings."
             : num(rows.length) + " recordings, in " + num(kinds.length) + " kinds.");

    // The filter strip. Same shape as the Emails break-down: each chip links and
    // carries its own count, so a chip can only claim what was actually counted.
    var strip = el("div", "ebd"); main.appendChild(strip);
    var chips = el("div", "ebd-body"); strip.appendChild(chips);
    function chip(k, label, n) {
      var a = el("a", "ebd-chip" + (active === k ? " on" : ""));
      a.href = urlFor(k ? { page: "recordings", kind: k } : { page: "recordings" },
                      { label: label });
      a.appendChild(el("span", "ebd-chip-l", esc(label)));
      a.appendChild(el("span", "ebd-chip-n", num(n)));
      chips.appendChild(a);
    }
    chip("", "All recordings", rows.length);
    kinds.forEach(function (k) { chip(k, labelFor(k), byKind[k].length); });
    // The grouping is only as good as the classifier, and this one contradicts
    // itself often enough that saying so is part of reporting it honestly.
    if (EXAMINER) {
      strip.appendChild(el("p", "eix-note",
        "Grouped by the pipeline's own classification of each recording, which is "
        + "not always right — the same recording saved in two formats is "
        + "sometimes filed two different ways. Each row says what it was "
        + "classified as."));
    }

    var wrap = el("div", "reading"); main.appendChild(wrap);
    if (active) {
      byKind[active].forEach(function (r) { wrap.appendChild(recordingCard(r)); });
      return;
    }
    // Unfiltered, every kind is a COLLAPSED section: six lines you can take in at
    // once, rather than six headings separated by four hundred audio players. The
    // headings alone were an improvement on a flat list and still left the second
    // section a long scroll below the first. Rows are built on first expand, so
    // the page does not pay for 1,051 <audio> elements nobody opened.
    kinds.forEach(function (k) {
      var sec = el("section", "rec-sec");
      var head_ = el("div", "rec-sechead");
      var caret = el("span", "vcaret", "▸");
      head_.appendChild(caret);
      head_.appendChild(el("span", "rec-seclabel", esc(labelFor(k))));
      head_.appendChild(el("span", "rec-secn", num(byKind[k].length)));
      var only = el("a", "rec-seconly", "Show only these");
      only.href = urlFor({ page: "recordings", kind: k }, { label: labelFor(k) });
      only.onclick = function (e) { e.stopPropagation(); };
      head_.appendChild(only);
      var body = el("div", "rec-secbody");
      body.hidden = true;
      var built = false;
      function setOpen(on) {
        body.hidden = !on;
        caret.textContent = on ? "▾" : "▸";
        head_.setAttribute("aria-expanded", String(on));
        head_.classList.toggle("open", on);
        if (on && !built) {
          built = true;
          byKind[k].forEach(function (r) { body.appendChild(recordingCard(r)); });
        }
      }
      head_.onclick = function () { setOpen(body.hidden); };
      keyable(head_, "button", labelFor(k));
      head_.setAttribute("aria-expanded", "false");
      sec.appendChild(head_);
      sec.appendChild(body);
      wrap.appendChild(sec);
    });
  };

  P.accounts = function (main, d) {
    head(main, "Online Accounts", "Online Accounts", d.note || "");
    var creds = d.credentials || {};
    main.appendChild(el("div", "banner",
      "<strong>" + num(creds.critical_count) + "</strong> critical and <strong>" + num(creds.informational_count) +
      "</strong> informational credential finding(s)."));
    // The services list, replacing a flat table of raw sender domains. Two
    // things changed. It is WIDER: the pipeline's inventory is built from the raw
    // corpus and on this case held only social and newsletter domains, while every
    // bank and brokerage the estate deals with sat unlisted in the mail. And it is
    // CLICKABLE: a row opens that service's conversations.
    //
    // The count is the number of conversations the Emails page can actually open,
    // not the pipeline's raw figure, because the row is a link and a link has to
    // deliver what it promises. Those diverge a long way — triage discards bulk
    // notification mail — so a service with hundreds of raw notifications can have
    // a handful of readable threads, or none at all. Where none survive the row is
    // NOT a link: an empty results page reads as a broken feature.
    var services = d.services || [];
    main.appendChild(el("h2", null, "Services found in the mail"));
    main.appendChild(el("p", "eix-note",
      num(services.length) + " services. A row opens that service's conversations. "
      + "Found by looking for the kind of mail an account sends — sign-ins, "
      + "security alerts, statements — so it is a strong hint rather than a "
      + "certainty, and a service the owner never received such mail from will "
      + "not appear."));
    var stbl = el("table", "svc-table");
    stbl.innerHTML = "<tr><th>Service</th><th>Conversations</th><th>Account mail</th><th></th></tr>";
    services.forEach(function (x) {
      var tr = el("tr");
      var nameCell = el("td");
      if (x.threads) {
        var a = el("a", "svc-link", esc(x.service));
        a.href = urlFor({ page: "emails", participant: x.service },
                        { label: x.service });
        nameCell.appendChild(a);
      } else {
        nameCell.appendChild(el("span", null, esc(x.service)));
      }
      if (x.from_pipeline) {
        nameCell.appendChild(el("span", "svc-src", "inventory"));
      }
      tr.appendChild(nameCell);
      tr.appendChild(el("td", "svc-n", x.threads ? num(x.threads) : "—"));
      tr.appendChild(el("td", "svc-n", x.signals ? num(x.signals) : "—"));
      // Say where the missing mail went rather than leaving a row that promises
      // hundreds of messages and opens onto nothing.
      var note = el("td", "svc-note");
      if (!x.threads) {
        note.textContent = x.filtered_out
          ? "Its " + num(x.filtered_out) + " notification email"
            + (x.filtered_out === 1 ? " was" : "s were")
            + " filtered out of the readable mail as bulk — nothing to open."
          : "Nothing readable in the mail.";
      } else if (x.filtered_out) {
        note.textContent = num(x.filtered_out) + " more filtered out as bulk.";
      }
      tr.appendChild(note);
      stbl.appendChild(tr);
    });
    main.appendChild(stbl);
    if ((creds.items || []).length) {
      main.appendChild(el("h2", null, "Credential documents"));
      if (creds.guidance) main.appendChild(el("p", "notice cred-guidance", esc(creds.guidance)));
      var ct = el("table"); ct.innerHTML = "<tr><th>File</th>" + (EXAMINER ? "<th>Types</th><th>Severity</th>" : "") + "</tr>";
      creds.items.forEach(function (it) {
        ct.appendChild(el("tr", null, "<td>" + esc(it.file) + "</td>" +
          (EXAMINER ? "<td>" + esc((it.types || []).join(", ")) + "</td><td>" + esc(it.severity || "") + "</td>" : "")));
      });
      main.appendChild(ct);
    }
  };

  function confirmActions(item, card) {
    // _gone (set by reviewConfirm) also splices the queue array + refreshes
    // counts; a bare card (lightbox-only context) just removes itself.
    function done(lb) {
      if (card) { if (card._gone) card._gone(); else card.remove(); }
      if (lb) closeLB();
    }
    if (item.kind === "unnamed_person") {
      return [{ label: "Name…", cls: "primary", onclick: function (lb) {
        textPrompt("Name for " + item.guess + ":", "", function (name) {
          if (!name) return;
          doVerb("/api/rename/person", { person_id: item.id, new_name: name }, "Named " + name).then(function (x) { if (x) done(lb); });
        }); return;
      } }];
    }
    var acts = [
      { label: "Confirm", cls: "primary", onclick: function (lb) {
        doVerb("/api/confirm", { queue: item.queue, id: item.id, decision: "accept" }, "Confirmed").then(function (x) { if (x) done(lb); }); } },
      { label: "Dismiss", onclick: function (lb) {
        doVerb("/api/confirm", { queue: item.queue, id: item.id, decision: "reject" }, "Dismissed").then(function (x) { if (x) done(lb); }); } },
    ];
    // G-15: an unidentified/noise face can be ASSIGNED to a person cluster (it then
    // joins that person and leaves the queue). item.id is the noise src.
    if (item.kind === "unidentified_face") {
      acts.push({ label: "Assign to…", onclick: function (lb) {
        pickPerson("Assign this face to which person?", null, function (pid) {
          doVerb("/api/assign/face", { src: item.id, person_id: pid }, "Assigned face")
            .then(function (x) { if (x) done(lb); });
        }); } });
      // Move verb: re-file this face under a person (face_placements overlay). Same
      // effect as Assign for a noise face, but the general person-Move entrypoint.
      acts.push({ label: "Move to person…", onclick: function (lb) {
        pickPerson("Move this face to which person?", null, function (pid) {
          doVerb("/api/move", { view: "person", src: item.id, to: pid }, "Moved to person")
            .then(function (x) { if (x) done(lb); });
        }); } });
    }
    // Media-bearing guesses can also be Discarded outright (#8); item.id is a src.
    if (isMediaKind(item.kind)) {
      acts.push({ label: "Discard", cls: "danger", onclick: function (lb) {
        doVerb("/api/banish", { src: item.id }, "Discarded").then(function (x) { if (x) done(lb); }); } });
    }
    return acts;
  }

  // ── review-queue bulk-triage PAGER (review-queue-bulk-triage.md) ─────────────
  // A one-item-at-a-time triage spine over the two surfaces with real audited
  // verbs today — quarantine and vital documents — reached from the Guided review
  // checklist via /review?group=quarantine|vital. Optimistic advance (Phase 0:
  // ~1s/action, all case.load()) with per-item pending/error state and a
  // persistent single-token undo bar. No new verbs: it rides the existing
  // release/discard/confirm/promote/dismiss/reassign endpoints.
  //
  // Verb map: pager action → endpoint + payload builder. `resolves:false` (reassign)
  // means the item changed target but stays reviewable (§5) — the pager advances
  // but does NOT count it cleared.
  var PAGER_VERBS = {
    release:  { ep: "/api/release", past: "Released",
                pay: function (it) { return { canonical_path: it.id }; }, resolves: true },
    discard:  { ep: "/api/discard/quarantine", past: "Discarded", cls: "danger",
                pay: function (it) { return { canonical_path: it.id }; }, resolves: true },
    confirm:  { ep: "/api/vital/confirm", past: "Confirmed", cls: "primary",
                pay: function (it) { return { id: it.id }; }, resolves: true },
    promote:  { ep: "/api/vital/promote", past: "Promoted", cls: "primary",
                pay: function (it) { return { id: it.id }; }, resolves: true },
    dismiss:  { ep: "/api/vital/dismiss", past: "Dismissed", cls: "danger",
                pay: function (it) { return { id: it.id }; }, resolves: true },
    // to_target / scope are filled by the reassign modal (pay is null here).
    reassign: { ep: "/api/vital/reassign", past: "Reassigned", pay: null, resolves: false },
  };
  var PAGER_LABEL = { release: "Release", discard: "Discard", confirm: "Confirm",
                      promote: "Promote", dismiss: "Dismiss", reassign: "Reassign…",
                      skip: "Skip" };
  // Keyboard: per-group accelerators. ←/→ back/forward and S skip are shared;
  // Space reveals a blurred quarantine item.
  var PAGER_KEYS = {
    quarantine: { r: "release", d: "discard" },
    vital: { c: "confirm", p: "promote", x: "dismiss", a: "reassign" },
  };
  var PAGER_KEYHANDLER = null;   // module-side so a re-entry unbinds the old one

  function reviewPager(main, group) {
    // ?target=<vital target key> scopes the queue to ONE document type. The
    // endpoint takes only a group, so the filter is applied here — the payload is
    // fetched whole either way (1,316 items on this case), so scoping client-side
    // costs nothing and needs no change to a file that is mirrored from upstream.
    var scope = (group === "vital" && Q.target) ? String(Q.target) : "";
    var title = group === "vital" ? "Vital documents — review"
                                  : "Quarantine — bulk review";
    // Prefer the human label the linking page passed us ("Will / testament") over
    // sentence-casing the raw key ("Will testament"). Falls back when someone
    // types the URL by hand.
    if (scope) title = (Q.crumb || sentenceCase(scope)) + " — review";
    head(main, "Examiner · Review", title,
      "One at a time. Read it, decide, and the queue moves on.");
    var esc0 = el("a", "act small", scope ? "← All vital documents" : "List view");
    esc0.href = group === "vital"
      ? (scope ? "/review?group=vital" : "/documents")
      : "/review?group=quarantine&list=1";
    main.appendChild(esc0);
    var body = el("div", "pager"); main.appendChild(body);
    body.appendChild(el("p", "notice", "Loading…"));
    getJSON("/api/review-pager?group=" + encodeURIComponent(group)).then(function (d) {
      if (scope) {
        d = { group: d.group, all_targets: d.all_targets,
              items: (d.items || []).filter(function (it) { return it.target === scope; }) };
        d.total = d.items.length;
      }
      buildPager(body, group, d);
    }).catch(function (e) {
      body.innerHTML = "";
      body.appendChild(el("p", "notice", "Couldn't load: " + esc((e && e.message) || "error")));
    });
  }

  function buildPager(body, group, d) {
    body.innerHTML = "";
    var items = (d.items || []).map(function (it) { it._state = "pending"; return it; });
    var allTargets = d.all_targets || [];
    var pos = 0;
    var revealed = false;   // per-item blur-reveal latch

    // ── the stopping place between a category's candidates and its near-misses ──
    // vital_pager_items groups the queue by target, so each category's candidates
    // run straight into that same category's near-misses. Crossing that line used
    // to be invisible: the queue simply carried on, and a reviewer had no way to
    // tell they had stopped reading candidates and started reading weaker matches.
    // gateAt() marks the FIRST near-miss of every category; renderGate() stops
    // there once and asks. gateAck remembers the answer, so paging back over a
    // handover you have already answered does not ask a second time.
    var gateAck = {};
    var catCand = {}, catNear = {};
    items.forEach(function (it) {
      var t = it.target || "";
      if (it.vqueue === "near_miss") catNear[t] = (catNear[t] || 0) + 1;
      else if (it.vqueue) catCand[t] = (catCand[t] || 0) + 1;
    });
    // Never at index 0: there is nothing behind it to have finished.
    function gateAt(i) {
      var it = items[i];
      if (!it || group !== "vital" || it.vqueue !== "near_miss" || i === 0) return false;
      var prev = items[i - 1];
      return prev.target !== it.target || prev.vqueue !== "near_miss";
    }
    function gated() { return gateAt(pos) && !gateAck[pos]; }
    // The human label the checklist uses ("Will / testament"), not the raw key.
    function targetLabel(t) {
      for (var i = 0; i < allTargets.length; i++) {
        if (allTargets[i].target === t) return allTargets[i].label || sentenceCase(t);
      }
      return t ? sentenceCase(t) : "this category";
    }

    if (!items.length) {
      body.appendChild(el("p", "notice",
        group === "vital" ? "No vital documents need review — the checklist is clear."
                          : "Nothing in quarantine to review."));
      body.appendChild(backToReviewLink());
      return;
    }

    var progress = el("div", "pager-progress"); body.appendChild(progress);
    var stage = el("div", "pager-stage"); body.appendChild(stage);
    var acts = el("div", "pager-actions"); body.appendChild(acts);
    var undobar = el("div", "pager-undo"); undobar.style.display = "none"; body.appendChild(undobar);
    var tray = el("div", "pager-tray"); tray.style.display = "none"; body.appendChild(tray);

    function counts() {
      var c = { done: 0, error: 0, skipped: 0, reassigned: 0, pending: 0 };
      items.forEach(function (it) { c[it._state] = (c[it._state] || 0) + 1; });
      return c;
    }
    function curItem() { return items[pos]; }

    function updateProgress() {
      var c = counts();
      progress.innerHTML = "";
      progress.appendChild(el("span", "pnum", esc("Item " + Math.min(pos + 1, items.length) +
        " of " + items.length)));
      var bits = [];
      if (c.done) bits.push(c.done + " resolved");
      if (c.reassigned) bits.push(c.reassigned + " reassigned");
      if (c.skipped) bits.push(c.skipped + " skipped");
      if (c.error) bits.push(c.error + " need attention");
      if (bits.length) progress.appendChild(el("span", "pmeta", esc("· " + bits.join(" · "))));
    }

    function backToReviewLink() {
      var a = el("a", "act", "Back to Review queue"); a.href = "/review"; return a;
    }

    // ── one item's preview + metadata ──
    // Bumped on every stage render. An inline document preview is fetched async,
    // so a slow reply for item N must not paint itself over item N+1 when the
    // examiner pages on before it lands — every callback re-checks its token.
    var stageToken = 0;
    function renderStage() {
      if (gated()) return renderGate();
      stage.innerHTML = "";
      stageToken++;
      revealed = false;
      var it = curItem();
      if (!it) return renderDone();
      var card = el("div", "pcard pcard-" + it.kind);
      // status ribbon for an already-touched item (revisited via ← )
      if (it._state !== "pending") {
        var s = it._state === "done" ? "✓ " + (it._pastLabel || "Done")
              : it._state === "reassigned" ? "→ Reassigned"
              : it._state === "skipped" ? "Skipped" : "⚠ Action failed";
        card.appendChild(el("div", "pribbon pribbon-" + it._state, esc(s)));
      }
      if (it.kind === "quarantine") renderQuar(card, it); else renderVital(card, it);
      stage.appendChild(card);
      renderActions(it);
      updateProgress();
    }

    // The handover card itself. It is a stop, not an item: no verb fires from
    // here, and the near-miss behind it is not rendered until the answer is given.
    function renderGate() {
      stage.innerHTML = ""; acts.innerHTML = "";
      stageToken++;            // an in-flight preview belongs to the item we left
      revealed = false;
      var t = (items[pos] || {}).target || "";
      var label = targetLabel(t);
      var nCand = catCand[t] || 0, nNear = catNear[t] || 0;
      var card = el("div", "pcard pgate");
      card.appendChild(el("h2", "pgate-title", esc(
        nCand ? "Finished reviewing candidates for " + label + "."
              : label + " has no candidates.")));
      card.appendChild(el("p", "pgate-ask", esc(
        "Review its " + num(nNear) + " near miss" + (nNear === 1 ? "" : "es") + "?")));
      card.appendChild(el("p", "pgate-note", esc(
        "A near miss is something the pipeline found and did not confirm as "
        + label.toLowerCase() + ". Reviewing them is optional — skipping leaves "
        + "them undecided, and they stay on the checklist.")));
      stage.appendChild(card);
      var go = el("button", "act primary", "Review near misses");
      go.onclick = function () { gateAck[pos] = true; renderStage(); };
      acts.appendChild(go);
      var sk = el("button", "act ghost", "Skip to next category");
      sk.onclick = function () { skipCategory(); };
      acts.appendChild(sk);
      var back = el("button", "act ghost", "← Back");
      back.disabled = pos <= 0;
      back.onclick = function () { goBack(); };
      acts.appendChild(back);
      acts.appendChild(el("span", "pkeyhint", esc("↵/→ review   ← back")));
      updateProgress();
    }

    // Skip every remaining item of the category we are standing in. Only the
    // near-misses are ahead of us (the candidates came before the handover), and
    // they are marked skipped exactly like a per-item Skip, so the completion
    // summary still offers them back as unresolved.
    function skipCategory() {
      var t = (items[pos] || {}).target;
      while (pos < items.length && items[pos].target === t) {
        if (items[pos]._state === "pending") items[pos]._state = "skipped";
        pos++;
      }
      if (pos >= items.length) return renderDone();
      renderStage();
    }

    function renderQuar(card, it) {
      card.appendChild(el("h2", "pname", esc(it.name || "(unnamed)")));
      if (it.filters && it.filters.length) {
        var fb = el("div", "pfilters");
        it.filters.forEach(function (f) { fb.appendChild(el("span", "chip danger", esc(pretty(f)))); });
        card.appendChild(fb);
      }
      if (it.src) {
        var wrap = el("div", "pager-media" + (it.blur ? " blurred" : ""));
        var node;
        if (it.media_kind === "video") { node = el("video"); node.src = mediaURL(it.src); node.controls = true; }
        else { node = el("img"); node.src = mediaURL(it.src); node.alt = ""; }
        wrap.appendChild(node);
        if (it.blur) {
          wrap.appendChild(el("div", "pager-reveal", "Hidden — click or press Space to reveal"));
          wrap.onclick = function () { revealCurrent(); };
        }
        card.appendChild(wrap);
      } else {
        card.appendChild(el("div", "pnobytes",
          "No preview — the file's bytes are not available. It can still be released or discarded."));
      }
    }

    function renderVital(card, it) {
      var titleRow = el("div", "pvitalhead");
      titleRow.appendChild(el("span", "chip", esc(it.vqueue === "near_miss" ? "Near-miss" : "To confirm")));
      if (it.target) titleRow.appendChild(el("span", "chip target", esc(sentenceCase(it.target))));
      card.appendChild(titleRow);
      card.appendChild(el("h2", "pname", esc(it.name || it.thread_subject || it.conversation_subject || "(unnamed)")));
      // Where the CLASSIFIER put this document, from its own delivered path. The
      // reviewer is being asked "is this a marriage certificate?" — knowing the
      // pipeline filed it under legal/court_filing is the cheapest possible check
      // against the wrong answer, and it is already on the client.
      var pw = filedUnder(it.file_id);
      if (pw) card.appendChild(el("div", "pfiled", esc("The pipeline filed this under " + pw)));
      if (it.vqueue === "near_miss") {
        if (it.disposition) card.appendChild(el("div", "pdisp",
          esc("Why it wasn't confirmed: " + pretty(it.disposition) +
              (it.reason ? " — " + it.reason : ""))));
        if (it.snippet) card.appendChild(el("blockquote", "psnip", esc(it.snippet)));
      }
      // Every item that HAS content shows it in the card. Confirm/dismiss/reassign
      // is a judgement about what the thing says, so the content it depends on
      // must not sit behind a click (or, for a thread, a navigation away from the
      // queue that loses your place). Email is the single biggest kind across
      // cases — 100% of jebb's 216 items, 277 of 724_vital's 670.
      var open;
      if (it.file_id) {
        card.appendChild(vitalDocPreview(it, stageToken));
        return;
      } else if (it.thread_id) {
        card.appendChild(vitalThreadPreview(it, stageToken));
        return;
      } else if (it.conversation_id) {
        card.appendChild(vitalConversationPreview(it, stageToken));
        return;
      } else {
        open = el("span", "muted", "No preview available for this item.");
      }
      card.appendChild(open);
    }

    // Shared shell for an embedded preview: a title bar with an "open in its own
    // page" escape hatch, plus the bounded scroll box the content renders into.
    function previewShell(title, href, linkLabel) {
      var wrap = el("div", "pdoc");
      var bar = el("div", "pdoc-bar");
      bar.appendChild(txt("span", "pdoc-name", title));
      var a = el("a", "act small", linkLabel);
      a.href = href;
      a.setAttribute("rel", "noopener noreferrer");
      bar.appendChild(a);
      wrap.appendChild(bar);
      var box = el("div", "pdoc-body");
      wrap.appendChild(box);
      return { wrap: wrap, box: box };
    }

    // An email thread, rendered in the card by the same code the Emails detail
    // page uses. Linking out used to cost the examiner their place in the queue.
    function vitalThreadPreview(it, token) {
      var sh = previewShell(it.thread_subject || it.name || "(no subject)",
                            "/emails?thread=" + encodeURIComponent(it.thread_id),
                            "Open in Emails");
      sh.box.classList.add("reading", "emthread", "pdoc-reading");
      sh.box.appendChild(el("p", "muted", "Loading thread…"));
      getJSON("/api/email/thread?id=" + encodeURIComponent(it.thread_id)).then(function (t) {
        if (token !== stageToken) return;          // paged on — stale reply
        sh.box.innerHTML = "";
        drawThreadMessages(sh.box, t);
      }).catch(function () {
        if (token !== stageToken) return;
        sh.box.innerHTML = "";
        sh.box.appendChild(el("p", "muted", "Couldn't load this thread — use Open in Emails."));
      });
      return sh.wrap;
    }

    // A chat/SMS conversation chunk (#26). Same treatment as a thread: 35 of
    // joex's 142 vital items are conversations.
    function vitalConversationPreview(it, token) {
      var sh = previewShell(it.conversation_subject || it.name || "(conversation)",
                            "/messages?conversation=" + encodeURIComponent(it.conversation_id),
                            "Open in Messages");
      sh.box.classList.add("reading", "msgthread", "pdoc-reading");
      sh.box.appendChild(el("p", "muted", "Loading conversation…"));
      getJSON("/api/message/conversation?id=" + encodeURIComponent(it.conversation_id)).then(function (c) {
        if (token !== stageToken) return;
        sh.box.innerHTML = "";
        if (!c) { sh.box.appendChild(el("p", "muted", "This conversation could not be found.")); return; }
        drawConversationStream(sh.box, c);
      }).catch(function () {
        if (token !== stageToken) return;
        sh.box.innerHTML = "";
        sh.box.appendChild(el("p", "muted", "Couldn't load this conversation — use Open in Messages."));
      });
      return sh.wrap;
    }

    // Embedded document preview for a vital-doc file item. PDFs (273 of this
    // corpus's 392 file items) use the browser's own viewer; office formats use
    // the text layer the ocr stage extracted; images render directly. Bounded to
    // a scrollable box so the action row stays reachable without scrolling past
    // a 40-page deed. "Open full size" keeps the lightbox for zooming/reading.
    function vitalDocPreview(it, token) {
      var kind = mediaKind(it.name || it.file_id);
      var wrap = el("div", "pdoc");

      var bar = el("div", "pdoc-bar");
      bar.appendChild(txt("span", "pdoc-name", it.name || basename(it.file_id)));
      var big = el("button", "act small", "Open full size");
      big.onclick = function () {
        lightbox(it.file_id, kind, null, { name: it.name });
      };
      bar.appendChild(big);
      wrap.appendChild(bar);

      // dv-body carries the document typography; the block styles are scoped to
      // it, so an office preview must wear that class to pick them up.
      var box = el("div", "pdoc-body" + (kind === "image" || kind === "pdf" ? "" : " dv-body"));
      wrap.appendChild(box);

      if (kind === "image") {
        var im = el("img"); im.src = mediaURL(it.file_id); im.alt = "";
        box.appendChild(im);
      } else if (kind === "pdf") {
        // Same-origin /media, served inline without the sandbox CSP that blanks
        // the viewer (see _media_headers); the page CSP allows frame-src 'self'.
        var fr = document.createElement("iframe");
        fr.className = "pdoc-frame";
        fr.src = mediaURL(it.file_id);
        fr.setAttribute("title", esc(it.name || "Document preview"));
        box.appendChild(fr);
      } else {
        box.appendChild(el("p", "muted", "Loading preview…"));
        getJSON("/api/doctext?src=" + encodeURIComponent(it.file_id)).then(function (r) {
          if (token !== stageToken) return;          // paged on — stale reply
          var blocks = (r && r.blocks) || [];
          if (!blocks.length) throw new Error("empty");
          box.innerHTML = "";
          blocks.forEach(function (b) {
            var n = docBlockNode(b);
            if (n) box.appendChild(n);
          });
        }).catch(function () {
          if (token !== stageToken) return;
          box.innerHTML = "";
          box.appendChild(el("p", "muted",
            "No inline preview for this file type — use Open full size."));
        });
      }
      return wrap;
    }

    // ── action row (only the item's real actions, + Skip) ──
    function renderActions(it) {
      acts.innerHTML = "";
      if (it._state === "done" || it._state === "reassigned") {
        // Already acted this pass — offer Next / (undo lives in the persistent bar).
        var nxt = el("button", "act primary", "Next →");
        nxt.onclick = function () { advance(); };
        acts.appendChild(nxt);
      } else {
        (it.actions || []).forEach(function (a) {
          var v = PAGER_VERBS[a];
          var b = el("button", "act" + (v && v.cls ? " " + v.cls : ""), PAGER_LABEL[a] || a);
          b.onclick = function () { doAction(a); };
          acts.appendChild(b);
        });
        var sk = el("button", "act ghost", "Skip");
        sk.onclick = function () { skip(); };
        acts.appendChild(sk);
      }
      // shared navigation
      var back = el("button", "act ghost", "← Back");
      back.disabled = pos <= 0;
      back.onclick = function () { goBack(); };
      acts.appendChild(back);
      var kh = el("span", "pkeyhint", esc(keyHint(it)));
      acts.appendChild(kh);
    }

    function keyHint(it) {
      var parts = [];
      var map = PAGER_KEYS[group] || {};
      Object.keys(map).forEach(function (k) {
        if ((it.actions || []).indexOf(map[k]) >= 0) parts.push(k.toUpperCase() + " " + PAGER_LABEL[map[k]]);
      });
      parts.push("S skip"); parts.push("←/→ back/next");
      if (group === "quarantine" && it.blur) parts.push("Space reveal");
      return parts.join("   ");
    }

    // ── reveal a blurred quarantine item ──
    function revealCurrent() {
      var w = stage.querySelector(".pager-media.blurred");
      if (w) { w.classList.remove("blurred"); revealed = true; }
    }

    // ── advance / back / skip ──
    function advance() {
      if (pos < items.length) pos++;
      if (pos >= items.length) return renderDone();
      renderStage();
    }
    function goBack() { if (pos > 0) { pos--; renderStage(); } }
    function skip() {
      var it = curItem();
      if (it && it._state === "pending") it._state = "skipped";
      advance();
    }

    // ── act on the current item (optimistic advance) ──
    function doAction(action) {
      var it = curItem();
      if (!it) return;
      if (action === "reassign") return openReassign(it);
      fire(it, action, PAGER_VERBS[action].pay(it));
    }

    function fire(it, action, payload) {
      var v = PAGER_VERBS[action];
      it._state = "acting";
      it._pastLabel = v.past;
      // Optimistic: mark the outcome and advance NOW; reconcile when the POST lands.
      it._state = v.resolves ? "done" : "reassigned";
      advance();
      postJSON(v.ep, payload).then(function (res) {
        if (res.ok && res.j && res.j.ok !== false) {
          var tok = res.j.undo_token;
          setUndo(it, v, tok);
        } else {
          it._state = "error";
          it._error = (res.j && res.j.error) || "verb failed";
          toast("Couldn't " + (PAGER_LABEL[action] || action) + ": " + it._error);
          renderTray();
          if (curItem() === it) renderStage();   // still on it → reflect the error
          updateProgress();
        }
      }).catch(function (e) {
        it._state = "error"; it._error = (e && e.message) || "server error";
        toast("Couldn't " + (PAGER_LABEL[action] || action) + ": " + it._error);
        renderTray(); updateProgress();
      });
    }

    // ── reassign modal: pick a target (+ scope for dup-path items) ──
    function openReassign(it) {
      var box = el("div");
      var selT = el("select");
      allTargets.forEach(function (t) {
        if (t.target === it.target) return;   // can't reassign to its own target
        selT.appendChild(new Option(t.label || t.target, t.target));
      });
      box.appendChild(el("label", "flabel", "New document type"));
      box.appendChild(selT);
      // Scope only matters when a document matched more than one vital target;
      // offered always but defaulting to the safe single-item scope.
      var selS = el("select");
      selS.appendChild(new Option("Only this item", "single"));
      selS.appendChild(new Option("Every category this document matched", "global"));
      box.appendChild(el("label", "flabel", "Apply to"));
      box.appendChild(selS);
      pickmodal("Reassign “" + (it.name || it.id) + "”", box, {
        onConfirm: function (close) {
          if (!selT.value) { toast("Pick a target"); return; }
          close();
          fire(it, "reassign", { id: it.id, to_target: selT.value, scope: selS.value });
        },
      });
    }

    // ── persistent single-token undo bar ──
    function setUndo(it, v, token) {
      undobar.innerHTML = "";
      undobar.style.display = "";
      undobar.appendChild(el("span", "ulabel",
        esc("Last action: " + v.past + " " + (it.name || it.id))));
      if (token) {
        var u = el("button", "act small", "Undo");
        u.onclick = function () {
          u.disabled = true;
          postJSON("/api/undo", { undo_token: token }).then(function (res) {
            if (res.ok && res.j && res.j.ok !== false) {
              // Server restored it → make it reviewable again and jump back to it.
              it._state = "pending"; it._error = null;
              pos = items.indexOf(it);
              undobar.style.display = "none";
              renderTray();
              renderStage();
            } else {
              u.disabled = false;
              toast("Couldn't undo: " + ((res.j && res.j.error) || "error"));
            }
          }).catch(function (e) {
            u.disabled = false;
            toast("Couldn't undo: " + ((e && e.message) || "server error"));
          });
        };
        undobar.appendChild(u);
      }
      updateProgress();
    }

    // ── error tray (items whose verb failed — reconciliation on failure) ──
    function renderTray() {
      var errs = items.filter(function (it) { return it._state === "error"; });
      tray.innerHTML = "";
      if (!errs.length) { tray.style.display = "none"; return; }
      tray.style.display = "";
      tray.appendChild(el("span", "tlabel",
        esc(errs.length + " item" + (errs.length === 1 ? "" : "s") + " need attention")));
      var b = el("button", "act small", "Review them");
      b.onclick = function () { pos = items.indexOf(errs[0]); renderStage(); };
      tray.appendChild(b);
    }

    // ── completion summary ──
    function renderDone() {
      stage.innerHTML = ""; acts.innerHTML = "";
      var c = counts();
      var card = el("div", "pcard pdone");
      card.appendChild(el("h2", null, "Queue reviewed"));
      var parts = [c.done + " resolved"];
      if (c.reassigned) parts.push(c.reassigned + " reassigned");
      if (c.skipped) parts.push(c.skipped + " skipped");
      if (c.error) parts.push(c.error + " need attention");
      card.appendChild(el("p", null, esc(parts.join(" · "))));
      var unresolved = items.filter(function (it) {
        return it._state === "skipped" || it._state === "error";
      });
      if (unresolved.length) {
        var again = el("button", "act primary", "Review " + unresolved.length + " unresolved");
        again.onclick = function () { pos = items.indexOf(unresolved[0]); renderStage(); };
        card.appendChild(again);
      }
      card.appendChild(backToReviewLink());
      stage.appendChild(card);
      updateProgress();
    }

    // ── keyboard ──
    if (PAGER_KEYHANDLER) document.removeEventListener("keydown", PAGER_KEYHANDLER);
    PAGER_KEYHANDLER = function (ev) {
      if (document.querySelector(".pickmodal-back")) return;           // modal owns keys
      if (document.body.classList.contains("lb-open")) return;         // lightbox owns keys
      var t = ev.target;
      if (t && /^(input|select|textarea|button)$/i.test(t.tagName) && ev.key !== "Escape") {
        // let buttons/inputs handle their own keys, except we still want arrows
        if (["ArrowLeft", "ArrowRight"].indexOf(ev.key) < 0) return;
      }
      var it = curItem();
      // On the handover card no verb key may fire — the item behind it has not
      // been read yet. Enter/→ answers "review them", ← steps back.
      if (gated()) {
        if (ev.key === "Enter" || ev.key === "ArrowRight") {
          gateAck[pos] = true; renderStage(); ev.preventDefault(); return;
        }
        if (ev.key === "ArrowLeft") { goBack(); ev.preventDefault(); return; }
        return;
      }
      if (ev.key === "ArrowRight") { advance(); ev.preventDefault(); return; }
      if (ev.key === "ArrowLeft") { goBack(); ev.preventDefault(); return; }
      if (!it) return;
      if (ev.key === " " && group === "quarantine" && it.blur && !revealed) { revealCurrent(); ev.preventDefault(); return; }
      if (ev.key === "s" || ev.key === "S") { skip(); ev.preventDefault(); return; }
      var act = (PAGER_KEYS[group] || {})[String(ev.key).toLowerCase()];
      if (act && (it.actions || []).indexOf(act) >= 0 && it._state === "pending") {
        doAction(act); ev.preventDefault();
      }
    };
    document.addEventListener("keydown", PAGER_KEYHANDLER);

    renderStage();
  }

  // Review queue is the examiner hub (#14/#15): a group selector switches between
  // To confirm / Quarantine / Sensitivity / Human review. The chosen group is
  // remembered module-side so an action's render() returns to the SAME group
  // (Release/Discard/etc. no longer bounce you back to "To confirm").
  var REVIEW_GROUP = "confirm";
  P.review = function (main, d) {
    // Guided deep-link into the bulk-triage pager: /review?group=quarantine|vital
    // opens the paged flow for that surface (unless ?list=1 asks for the classic
    // tabbed lists). §8.3 — the review UI used to ignore Q.group entirely.
    if ((Q.group === "quarantine" || Q.group === "vital") && Q.list !== "1") {
      return reviewPager(main, Q.group);
    }
    var controls = head(main, "Examiner · Review queue", "Review queue", "Resolve the pipeline's flags.");
    var qd = d.quarantine_entries || { entries: [], total: 0 };
    var groups = [["confirm", "To confirm", (d.confirm_queue || []).length],
                  ["quarantine", "Quarantine", qd.total || 0],
                  ["sensitivity", "Sensitivity", d.sensitive_total || 0],
                  ["human", "Human review", d.human_review_count || 0]];
    // A guided deep-link (?group=…) also seeds the classic tabbed view — including
    // the ?list=1 fallback from the pager's "List view" escape — so it opens on the
    // named tab instead of always snapping to "To confirm".
    if (Q.group && groups.some(function (g) { return g[0] === Q.group; })) REVIEW_GROUP = Q.group;
    if (!groups.some(function (g) { return g[0] === REVIEW_GROUP; })) REVIEW_GROUP = "confirm";
    var sel = el("select");
    groups.forEach(function (g) { sel.appendChild(new Option(g[1] + " (" + g[2] + ")", g[0])); });
    sel.value = REVIEW_GROUP;
    controls.appendChild(el("span", "flabel", "Group")); controls.appendChild(sel);
    // Group-specific banner tools (e.g. the confirm queue's Select/Deselect all);
    // cleared on every draw() so they live in the sticky banner, not the scroll body.
    var groupTools = el("span", "grouptools"); controls.appendChild(groupTools);
    var tiles = el("div", "tiles");
    groups.forEach(function (g) {
      var t = el("div", "tile clickable" + (g[0] === REVIEW_GROUP ? " active" : ""),
                 '<span class="n">' + num(g[2]) + '</span><span class="l">' + g[1] + "</span>");
      t.onclick = function () { setGroup(g[0]); };
      keyable(t, "button");   // F-12
      tiles.appendChild(t);
    });
    main.appendChild(tiles);
    var holder = el("div"); main.appendChild(holder);
    function setGroup(g) { REVIEW_GROUP = g; sel.value = g; draw(); }
    // Called by the sub-renderers after a targeted removal so the tile/select
    // counts track the arrays without re-fetching the whole review payload.
    function refreshCounts() {
      groups[0][2] = (d.confirm_queue || []).length;
      groups[1][2] = qd.total || 0;
      tiles.querySelectorAll(".tile").forEach(function (t, i) {
        var n = t.querySelector(".n"); if (n) n.textContent = num(groups[i][2]);
      });
      sel.querySelectorAll("option").forEach(function (o, i) {
        o.textContent = groups[i][1] + " (" + groups[i][2] + ")";
      });
    }
    function draw() {
      holder.innerHTML = "";
      groupTools.innerHTML = ""; document.body.classList.remove("selecting");  // reset banner tools + select mode on group switch
      // Drop any stale confirm-queue selection bar: it lives on document.body and
      // otherwise survives a group switch with a stale count and buttons bound to
      // the PREVIOUS group's items (clicking Confirm re-posted decisions for cards
      // no longer on screen). reviewConfirm recreates a fresh one when needed.
      var staleBar = document.getElementById("rselbar"); if (staleBar) staleBar.remove();
      var staleQBar = document.getElementById("qselbar"); if (staleQBar) staleQBar.remove();
      var g = sel.value;
      tiles.querySelectorAll(".tile").forEach(function (t, i) { t.classList.toggle("active", groups[i][0] === g); });
      if (g === "confirm") reviewConfirm(holder, (d.confirm_queue = d.confirm_queue || []), groupTools, refreshCounts);
      else if (g === "quarantine") reviewQuarantine(holder, qd, refreshCounts);
      else if (g === "sensitivity") reviewFlagged(holder, "Sensitivity", d.sensitive || [], true);
      else reviewFlagged(holder, "Human review", d.human_review || [], false);
    }
    sel.onchange = function () { REVIEW_GROUP = sel.value; draw(); };
    draw();
    transparencyReview(main);   // G-14: examiner suspense + significant-attachment noise
  };

  // Confirm queue with per-card actions + multi-select group actions (#13).
  // `tools` is the sticky-banner slot that holds the Select/Deselect-all buttons.
  // `onChange` is P.review's count refresher, called after in-place removals.
  function reviewConfirm(holder, q, tools, onChange) {
    if (!q.length) { holder.appendChild(el("p", "notice", "Nothing to confirm.")); return; }
    var rsel = {};  // review selection: queue:id -> item
    var cardOf = {};  // queue:id -> card element, for targeted removal
    // Pull resolved items out of the view in place: card out of the DOM, item
    // out of the queue array (so a group switch doesn't resurrect it), counts
    // refreshed — instead of re-fetching the entire review payload per action.
    function dropResolved(items) {
      items.forEach(function (it) {
        var key = it.queue + ":" + it.id;
        var c = cardOf[key];
        if (c) { c.remove(); delete cardOf[key]; }
        delete rsel[key];
        var ix = q.indexOf(it);
        if (ix >= 0) q.splice(ix, 1);
      });
      if (onChange) onChange();
      rbar();
      refreshMore();
      if (!q.length) holder.appendChild(el("p", "notice", "Nothing to confirm."));
    }
    function rbar() {
      var bar = document.getElementById("rselbar");
      if (!bar) { bar = el("div", "selbar"); bar.id = "rselbar"; document.body.appendChild(bar); }
      var items = Object.keys(rsel).map(function (k) { return rsel[k]; });
      bar.innerHTML = '<span class="n">' + items.length + ' selected</span><span class="sep"></span>';
      [["Confirm", "accept", "primary"], ["Dismiss", "reject", ""]].forEach(function (a) {
        var b = el("button", "act " + a[2], a[0]);
        b.onclick = function () {
          // accept/reject only applies to confirmable cards. unnamed_person cards
          // need a NAME (handled per-card) — batch-confirming them recorded a
          // decided name_person with value:null, so the "Name this person?" card
          // vanished unnamed. Skip them here.
          var confirmable = items.filter(function (it) { return it.kind !== "unnamed_person"; });
          if (!confirmable.length) {
            toast("Select photo or face cards to " + a[0].toLowerCase() + " — name people individually");
            return;
          }
          // ONE batched request: the server writes family_decisions.json once
          // for the whole selection (was N POSTs, each rewriting the file).
          postJSON("/api/confirm/batch", {
            items: confirmable.map(function (it) { return { queue: it.queue, id: it.id }; }),
            decision: a[1],
          }).then(function (res) {
            if (!res.ok || res.j.ok === false) {
              toast("Couldn't " + a[0].toLowerCase() + ": " + (res.j.error || "error"));
              return;
            }
            toast(a[0] + "ed " + res.j.count);
            dropResolved(confirmable);
          }).catch(function () {
            toast(a[0] + " failed — check the server");
          });
        };
        bar.appendChild(b);
      });
      var dz = el("button", "act danger", "Discard");
      dz.onclick = function () {
        var media = items.filter(function (it) { return isMediaKind(it.kind); });
        if (!media.length) { toast("No discardable media items selected"); return; }
        if (media.length > 1 && !confirm("Discard " + media.length + " items? Reversible from History.")) return;
        dz.disabled = true; dz.textContent = "Discarding…";
        doVerb("/api/banish", { srcs: media.map(function (it) { return it.id; }) }, "Discarded " + media.length)
          .then(function (x) { if (x) dropResolved(media); else { dz.disabled = false; dz.textContent = "Discard"; } });
      };
      bar.appendChild(dz);
      bar.classList.toggle("show", items.length > 0);
    }
    function rPick(c, on) {   // marquee/click selection → the local rsel set
      var it = c._item; if (!it) return;
      var key = it.queue + ":" + it.id;
      if (on) { rsel[key] = it; c.classList.add("sel"); } else { delete rsel[key]; c.classList.remove("sel"); }
      rbar();
    }
    function rClear() {
      rsel = {};
      grid.querySelectorAll(".sel").forEach(function (c) { c.classList.remove("sel"); });
      rbar();
    }
    var grid = el("div", "grid qgrid");
    marqueeSelect(tools || holder, grid, ".qcard", rPick, rClear);
    // Build ONE card for a queue item (was inline in the slice loop).
    function buildCard(item) {
      var c = el("div", "qcard"); c._item = item;
      cardOf[item.queue + ":" + item.id] = c;
      c._gone = function () { dropResolved([item]); };  // per-card verb resolution hook
      var thumbable = isMediaKind(item.kind);
      if (thumbable) {
        var im = el("img"); lazyThumb(im, item.id); im.alt = item.guess || pretty(item.kind);
        im.onclick = function () { lightbox(item.id, true, confirmActions(item, c)); };
        c.appendChild(im);
      } else c.appendChild(el("div", "ph", esc(pretty(item.kind))));
      var pick = el("div", "pick"); var key = item.queue + ":" + item.id;
      pick.onclick = function (e) {
        e.stopPropagation();
        var on;
        if (rsel[key]) { delete rsel[key]; c.classList.remove("sel"); on = false; } else { rsel[key] = item; c.classList.add("sel"); on = true; }
        pick.setAttribute("aria-pressed", on ? "true" : "false");
        rbar();
      };
      keyable(pick, "button", "Select item");   // F-12
      pick.setAttribute("aria-pressed", rsel[key] ? "true" : "false");
      c.appendChild(pick);
      var body = el("div", "body");
      var label = item.kind === "scene_guess" ? "Scene: " + esc(item.guess) :
        item.kind === "unnamed_person" ? "Name " + esc(item.guess) + "?" :
        item.kind === "event_guess" ? "Album: " + esc(item.guess) :
        item.kind === "unidentified_face" ? "Unidentified face" : esc(pretty(item.kind));
      body.innerHTML = "<div>" + label + (item.confidence != null ? ' <span class="badge">' + Math.round(item.confidence * 100) + "%</span>" : "") + "</div>";
      var row = el("div", "qrow");
      confirmActions(item, c).forEach(function (a) {
        var b = el("button", "btn" + (a.cls ? " " + a.cls : ""), esc(a.label));
        b.onclick = function () { a.onclick(null); };
        row.appendChild(b);
      });
      body.appendChild(row); c.appendChild(body); grid.appendChild(c);
    }
    // Load-more over the in-memory queue (was q.slice(0,120) — an examiner who
    // "confirmed all" never saw items 121+ while the tile showed the full count).
    // Rendering keys off cardOf, so it's robust to items being spliced out by a
    // verb; the visible count and the group total now agree.
    var CONFIRM_CHUNK = 200;
    var moreWrap = el("div", "pager");
    function refreshMore() {
      moreWrap.innerHTML = "";
      var shown = Object.keys(cardOf).length;
      moreWrap.appendChild(el("p", "count-note", "Showing " + num(shown) + " of " + num(q.length)));
      if (shown < q.length) {
        var b = el("button", "btn", "Load more (" + num(q.length - shown) + " remaining)");
        b.onclick = function () { renderChunk(); };
        moreWrap.appendChild(b);
      }
    }
    function renderChunk() {
      var added = 0;
      for (var i = 0; i < q.length && added < CONFIRM_CHUNK; i++) {
        var item = q[i], key = item.queue + ":" + item.id;
        if (cardOf[key]) continue;   // already on screen (robust to splices)
        buildCard(item); added++;
      }
      refreshMore();
    }
    holder.appendChild(grid);
    holder.appendChild(moreWrap);
    renderChunk();
  }

  // Quarantine group (folded in from the old page, #15): category filter + Release/Discard.
  // `onChange` refreshes P.review's counts after an in-place row removal.
  function reviewQuarantine(holder, qd, onChange) {
    var entries = qd.entries || [];
    if (!entries.length) { holder.appendChild(el("p", "notice", "Nothing in quarantine.")); return; }
    var cats = {}; entries.forEach(function (e) { if (e.filter) cats[e.filter] = 1; });
    var bar = el("div", "filterbar");
    var sel = el("select"); sel.appendChild(new Option("All categories", ""));
    Object.keys(cats).sort().forEach(function (c) { sel.appendChild(new Option(pretty(c), c)); });
    if (Q.qcat) sel.value = Q.qcat;
    bar.appendChild(el("span", "flabel", "Category")); bar.appendChild(sel);
    holder.appendChild(bar);
    var sub = el("div"); holder.appendChild(sub);
    // #17: bulk-select (mirrors the Confirm queue's floating-bar pattern) —
    // qsel: canonical_path -> entry, for the selected rows.
    var qsel = {};
    // On success: drop every resolved entry from the cached list and redraw
    // just this table (no full review-payload re-fetch), keeping counts honest.
    function dropResolved(resolved) {
      resolved.forEach(function (e) {
        var ix = entries.indexOf(e);
        if (ix >= 0) { entries.splice(ix, 1); qd.total = Math.max(0, (qd.total || 1) - 1); }
        delete qsel[e.canonical_path];
      });
      if (onChange) onChange();
      draw();
    }
    function qbar() {
      var bar2 = document.getElementById("qselbar");
      if (!bar2) { bar2 = el("div", "selbar"); bar2.id = "qselbar"; document.body.appendChild(bar2); }
      var items = Object.keys(qsel).map(function (k) { return qsel[k]; });
      bar2.innerHTML = '<span class="n">' + items.length + ' selected</span><span class="sep"></span>';
      var rel = el("button", "act primary", "Release");
      rel.onclick = function () {
        rel.disabled = true; dis.disabled = true;
        doVerb("/api/release", { canonical_paths: items.map(function (e) { return e.canonical_path; }) },
               "Released " + items.length).then(function (x) {
          if (x) dropResolved(items); else { rel.disabled = false; dis.disabled = false; }
        });
      };
      bar2.appendChild(rel);
      var dis = el("button", "act danger", "Discard");
      dis.onclick = function () {
        if (items.length > 1 && !confirm("Discard " + items.length + " items? Reversible from History.")) return;
        rel.disabled = true; dis.disabled = true;
        doVerb("/api/discard/quarantine", { canonical_paths: items.map(function (e) { return e.canonical_path; }) },
               "Discarded " + items.length).then(function (x) {
          if (x) dropResolved(items); else { rel.disabled = false; dis.disabled = false; }
        });
      };
      bar2.appendChild(dis);
      bar2.classList.toggle("show", items.length > 0);
    }
    // One action set per entry, reused by BOTH the row buttons and the expand
    // (lightbox) view so Release/Discard are reachable either way. onclick(lb)
    // closes the lightbox if it was opened from there (lb is null from the row).
    function quarActions(e) {
      function after(lb) {
        return function (x) {
          if (!x) return;
          if (lb) closeLB();
          dropResolved([e]);
        };
      }
      return [
        { label: "Release", onclick: function (lb) {
            // Single-item Release is immediate (reversible from History).
            doVerb("/api/release", { canonical_path: e.canonical_path }, "Released " + e.name).then(after(lb)); } },
        { label: "Discard", cls: "danger", onclick: function (lb) {
            doVerb("/api/discard/quarantine", { canonical_path: e.canonical_path }, "Discarded " + e.name).then(after(lb)); } },
      ];
    }
    function draw() {
      sub.innerHTML = "";
      var f = sel.value;
      var rows = entries.filter(function (e) { return !f || e.filter === f; })
        .slice().sort(function (a, b) { return (a.filter || "").localeCompare(b.filter || ""); });
      var tbl = el("table");
      var selAll = el("input"); selAll.type = "checkbox";
      selAll.setAttribute("aria-label", "Select all quarantined items shown");
      var thead = el("tr");
      var thCheck = el("th"); thCheck.appendChild(selAll); thead.appendChild(thCheck);
      ["Item", "Reason", "When", ""].forEach(function (t) { thead.appendChild(el("th", null, t)); });
      tbl.appendChild(thead);
      var selectable = rows.filter(function (e) { return !e.locked; });
      selAll.onchange = function () {
        selectable.forEach(function (e) {
          if (selAll.checked) qsel[e.canonical_path] = e; else delete qsel[e.canonical_path];
        });
        qbar();
        draw();
      };
      selAll.checked = selectable.length > 0 && selectable.every(function (e) { return qsel[e.canonical_path]; });
      rows.forEach(function (e) {
        var tr = el("tr");
        var td0 = el("td");
        if (!e.locked) {
          var cb = el("input"); cb.type = "checkbox";
          cb.checked = !!qsel[e.canonical_path];
          cb.setAttribute("aria-label", "Select " + e.name);
          cb.onchange = function () {
            if (cb.checked) qsel[e.canonical_path] = e; else delete qsel[e.canonical_path];
            qbar();
          };
          td0.appendChild(cb);
        }
        tr.appendChild(td0);
        var name = e.locked ? '<span class="locked">🔒 ' + esc(e.name) + " (locked)</span>"
          : '<a href="#" class="viewq">' + esc(e.name) + "</a>";
        var restCell = el("td", null, name);
        var reasonCell = el("td", null, esc(pretty(e.filter)));
        var whenCell = el("td", "preview", esc((e.timestamp || "").replace("T", " ")));
        var actCell = el("td");
        tr.appendChild(restCell); tr.appendChild(reasonCell); tr.appendChild(whenCell); tr.appendChild(actCell);
        if (!e.locked) {
          var acts = quarActions(e);
          var a = tr.querySelector("a.viewq");
          if (a) a.onclick = function (ev) { ev.preventDefault(); lightbox(e.src, mediaKind(e.src), acts); };
          acts.forEach(function (act) {
            var b = el("button", "btn small" + (act.cls ? " " + act.cls : ""), act.label);
            b.onclick = function () { act.onclick(null); };   // null lb → row button
            tr.lastChild.appendChild(b);
          });
        }
        tbl.appendChild(tr);
      });
      sub.appendChild(tbl);
      qbar();
    }
    sel.onchange = function () { setQ({ qcat: sel.value }); draw(); };
    draw();
  }

  // Sensitivity / Human-review flagged lists (#14): openable unless the row is locked.
  function reviewFlagged(holder, title, rows, showFilters) {
    if (!rows.length) { holder.appendChild(el("p", "notice", "Nothing flagged for " + title.toLowerCase() + ".")); return; }
    var tbl = el("table");
    tbl.innerHTML = "<tr><th>Item</th>" + (showFilters ? "<th>Flags</th>" : "") + "</tr>";
    rows.forEach(function (e) {
      var tr = el("tr");
      var name = e.locked ? '<span class="locked">🔒 ' + esc(e.name) + " (locked)</span>"
        : '<a href="#" class="viewq">' + esc(e.name) + "</a>";
      tr.innerHTML = "<td>" + name + "</td>" + (showFilters ? "<td class='preview'>" + esc((e.filters || []).map(pretty).join(", ")) + "</td>" : "");
      if (!e.locked && e.src) {
        var a = tr.querySelector("a.viewq");
        if (a) a.onclick = function (ev) { ev.preventDefault(); lightbox(e.src, mediaKind(e.src)); };
      }
      tbl.appendChild(tr);
      if (!e.locked && !e.src && e.chunk_text) {
        // Flagged conversation chunk: no servable file preview exists — the
        // flagged text itself is what the examiner must read. Click toggles it.
        var dtr = el("tr");
        dtr.style.display = "none";
        var dtd = el("td");
        dtd.colSpan = showFilters ? 2 : 1;
        var pre = el("pre", "preview");
        pre.style.whiteSpace = "pre-wrap";
        pre.textContent = e.chunk_text +
          (e.conversation_id ? "\n\n[conversation " + e.conversation_id + "]" : "");
        dtd.appendChild(pre);
        dtr.appendChild(dtd);
        tbl.appendChild(dtr);
        var ca = tr.querySelector("a.viewq");
        if (ca) ca.onclick = function (ev) {
          ev.preventDefault();
          dtr.style.display = dtr.style.display === "none" ? "" : "none";
        };
      }
    });
    holder.appendChild(tbl);
  }

  // ── History: descriptive label + item type + click-through (#12) ──
  function histType(e) {
    var a = (e.action || "").replace(/_undo$/, "");
    if (a === "demote_email" || a === "restore_email") return "Email";
    if (a === "rename_person" || a === "rename_folder" || a === "remove_person"
        || a === "merge_persons" || a === "assign_face") return "Person";
    if (a === "confirm") {
      var q = (e.target || "").split(":")[0];
      return { scene: "Scene", face: "Face", face_merge: "Face", name_person: "Person", event: "Album" }[q] || "Item";
    }
    if (a === "release") return "Quarantine";
    if (a === "export" || a === "export_collection") return "Export";
    var ext = basename(e.target || "").split(".").pop().toLowerCase();
    if (["jpg", "jpeg", "png", "gif", "heic", "tif", "tiff", "webp", "bmp"].indexOf(ext) >= 0) return "Photo";
    if (["mp3", "wav", "m4a", "aac", "flac", "ogg"].indexOf(ext) >= 0) return "Recording";
    if (["pdf", "doc", "docx", "txt", "rtf"].indexOf(ext) >= 0) return "Document";
    return "Item";
  }
  function nameOf(ident) { return ident && typeof ident === "object" ? ident.name : ident; }
  function histLabel(e) {
    var und = /_undo$/.test(e.action || ""), a = (e.action || "").replace(/_undo$/, ""), base;
    var b = e.before || {}, af = e.after || {};
    if (a === "rename_person") base = (nameOf(b.identity) || e.target) + " → " + (nameOf(af.identity) || "(unnamed)");
    else if (a === "confirm") base = ((af.decision === "accept") ? "Confirmed " : "Dismissed ") + basename((e.target || "").split(":").slice(1).join(":"));
    else if (a === "banish") base = "Discarded " + basename(e.target);
    else if (a === "release") base = "Released " + basename(e.target);
    else if (a === "export_collection") base = "Exported " + (af.exported != null ? af.exported : "") + " item(s)" + (af.skipped ? " (" + af.skipped + " skipped)" : "");
    else if (a === "export") base = "Exported " + (b.count != null ? b.count : "") + " item(s)";
    else if (a === "remove_person") base = "Removed person " + e.target;
    else if (a === "merge_persons") base = "Merged " + e.target + " → " + (af.winner || "");
    else if (a === "assign_face") base = "Assigned face to " + (af.person_id || "");
    else if (a === "demote_email" || a === "restore_email") base = (a === "demote_email" ? "Demoted" : "Restored") + " email" + (b.subject ? " '" + b.subject + "'" : "");
    else if (a === "reset") base = "Reset all changes (" + ((af.reversed != null ? af.reversed : b.reversed) || 0) + " reversed)";
    else base = pretty(a) + " " + basename(e.target);
    return (und ? "Undo: " : "") + base;
  }
  // Action column reflects the real action (decision-aware), not the raw verb (#14):
  // both Confirm and Dismiss use the "confirm" verb, so a dismissal showed "confirm".
  function histAction(e) {
    var und = /_undo$/.test(e.action || ""), a = (e.action || "").replace(/_undo$/, ""), af = e.after || {};
    var label = a === "confirm" ? (af.decision === "accept" ? "Confirm" : "Dismiss")
      : a === "banish" ? "Discard"
      : a === "release" ? "Release"
      : (a === "rename_person" || a === "rename_folder") ? "Rename"
      : (a === "export" || a === "export_collection") ? "Export"
      : a === "discard_quarantine" ? "Discard"
      : a === "demote_ranked" ? "Demote"
      : a === "demote_email" ? "Demote"
      : a === "restore_email" ? "Restore"
      : a === "remove_person" ? "Remove"
      : a === "merge_persons" ? "Merge"
      : a === "assign_face" ? "Assign"
      : pretty(a);
    return label + (und ? " (undo)" : "");
  }
  function histTarget(e) {
    var a = (e.action || "").replace(/_undo$/, ""), b = e.before || {}, af = e.after || {};
    if (a === "rename_person") return { page: "people", person: e.target };
    if (a === "merge_persons") return { page: "people", person: (e.after || {}).winner };
    if (a === "assign_face") return { open: true, file: e.target };
    if (a === "confirm") {
      var parts = (e.target || "").split(":"), q = parts[0], id = parts.slice(1).join(":");
      if (q === "scene" || q === "face" || q === "face_merge") return { open: true, file: id };
      if (q === "name_person") return { page: "people", person: id };
      return { page: "review" };
    }
    if (a === "banish") return af.location ? { open: true, file: af.location } : null;
    if (a === "release") return (b.entry && b.entry.canonical_path) ? { open: true, file: b.entry.canonical_path } : null;
    return null;
  }

  // ── G-13 Junk rescue (examiner) ──
  // A paginated grid of junk-routed images with per-item + batch Un-junk. Thumbs go
  // through the examiner /thumb resolver (the family allow-list excludes photos_junk).
  // Mirrors the batch-Discard UI: pick overlays + a sticky selection bar + toasts/undo.
  P.junk = function (main, data) {
    var controls = head(main, "Examiner · Junk rescue", "Junk review",
      "Images the pipeline routed out as junk. Un-junk to return one to the archive (reversible).");
    if (!EXAMINER) { main.appendChild(el("p", "notice", "Examiner only.")); return; }
    var grid = el("div", "grid jgrid"); main.appendChild(grid);
    var sel = {}, removed = {}, cardOf = {};
    var bar = el("div", "selbar"); bar.id = "jselbar"; document.body.appendChild(bar);
    function dropJunk(ids) {
      ids.forEach(function (id) {
        removed[id] = 1; delete sel[id];
        var c = cardOf[id]; if (c) { c.remove(); delete cardOf[id]; }
      });
      rbar();
    }
    function unjunkOne(id, name, lb) {
      doVerb("/api/unjunk", { id: id }, "Un-junked " + name).then(function (x) {
        if (x) { dropJunk([id]); if (lb) closeLB(); }
      });
    }
    function rbar() {
      var ids = Object.keys(sel);
      bar.innerHTML = '<span class="n">' + ids.length + ' selected</span><span class="sep"></span>';
      var b = el("button", "act primary", "Un-junk");
      b.onclick = function () {
        if (!ids.length) return;
        if (ids.length > 1 && !confirm("Un-junk " + ids.length + " items? They return to the archive (reversible from History).")) return;
        b.disabled = true; b.textContent = "Un-junking…";
        doVerb("/api/unjunk", { ids: ids }, "Un-junked " + ids.length + " item(s)").then(function (x) {
          if (!x) { b.disabled = false; b.textContent = "Un-junk"; return; }
          dropJunk(ids);
        });
      };
      bar.appendChild(b);
      bar.classList.toggle("show", ids.length > 0);
    }
    function jPick(c, on) {
      var id = c._id; if (!id) return;
      if (on) { sel[id] = 1; c.classList.add("sel"); } else { delete sel[id]; c.classList.remove("sel"); }
      rbar();
    }
    function jClear() { sel = {}; grid.querySelectorAll(".sel").forEach(function (c) { c.classList.remove("sel"); }); rbar(); }
    function card(r) {
      var c = el("div", "jcard"); c._id = r.id; cardOf[r.id] = c;
      var im = el("img"); lazyThumb(im, r.id); im.alt = r.name || "";
      im.onclick = function () {
        lightbox(r.id, "image", [{ label: "Un-junk", cls: "primary",
          onclick: function (lb) { unjunkOne(r.id, r.name, lb); } }]);
      };
      c.appendChild(im);
      var pick = el("div", "pick");
      pick.onclick = function (e) {
        e.stopPropagation();
        var on;
        if (sel[r.id]) { delete sel[r.id]; c.classList.remove("sel"); on = false; } else { sel[r.id] = 1; c.classList.add("sel"); on = true; }
        pick.setAttribute("aria-pressed", on ? "true" : "false");
        rbar();
      };
      keyable(pick, "button", "Select item");   // F-12
      pick.setAttribute("aria-pressed", sel[r.id] ? "true" : "false");
      c.appendChild(pick);
      var body = el("div", "body");
      body.innerHTML = '<div class="jname">' + esc(r.name) + '</div>' +
        (r.reason ? '<div class="jreason">' + esc(pretty(r.reason)) + '</div>' : '');
      var uj = el("button", "act small", "Un-junk");
      uj.onclick = function (e) { e.stopPropagation(); unjunkOne(r.id, r.name, null); };
      body.appendChild(uj);
      c.appendChild(body);
      return c;
    }
    var pg = pager("/api/junk", {
      render: function (rows) {
        grid.innerHTML = ""; cardOf = {};
        rows.forEach(function (r) { if (!removed[r.id]) grid.appendChild(card(r)); });
      }
    });
    main.appendChild(pg.footer);
    marqueeSelect(controls, grid, ".jcard", jPick, jClear);
    pg.seed(data);
  };

  // The vital-docs step's badge: two actionable numbers, both draining to 0.
  // `M to confirm` = found vital docs the examiner hasn't confirmed/dismissed/
  // reassigned yet (the Confirm verb drops this). `N near-miss to review` = the
  // candidate queue (promote/dismiss drops this). Both zero reads as "clear".
  function guidedVitalCount(ex) {
    var span = el("span", "gcount gcount-vital");
    var uc = ex.unconfirmed || 0, nm = ex.near_misses || 0;
    var parts = [];
    if (uc) parts.push(num(uc) + " to confirm");
    if (nm) parts.push(num(nm) + " near-miss" + (nm === 1 ? "" : "es") + " to review");
    if (!parts.length) { span.appendChild(el("span", "gcount-part", "clear")); return span; }
    parts.forEach(function (p, i) {
      if (i) span.appendChild(el("span", "gcount-sep", "·"));
      span.appendChild(el("span", "gcount-part" + (i ? " gcount-muted" : ""), esc(p)));
    });
    return span;
  }

  // ── G-12 Guided first-session review (examiner) ──
  // A checklist that sequences the existing review surfaces. Each step deep-links to
  // where the action already happens; a step is done when its count is 0 or the
  // examiner marks it done (persisted via the EXISTING confirm verb — no new verb).
  P.guided = function (main, d) {
    head(main, "Examiner · First-session review", "Guided review",
      "Work these in order; each links to where the action happens.");
    if (!EXAMINER) { main.appendChild(el("p", "notice", "Examiner only.")); return; }
    var steps = d.steps || [];
    var progress = el("p", "guided-progress",
      esc((d.done_count || 0) + " of " + (d.step_count || steps.length) + " steps done"));
    main.appendChild(progress);
    var list = el("ol", "guided-list");
    steps.forEach(function (s, i) {
      var li = el("li", "guided-step" + (s.done ? " done" : ""));
      li.appendChild(el("span", "gmark", s.done ? "✓" : String(i + 1)));
      var gbody = el("div", "gbody");
      var titleRow = el("div", "gtitle");
      var a = el("a", "glink", esc(s.label)); a.href = s.link || "#";
      titleRow.appendChild(a);
      // The vital-docs step carries TWO numbers — a reviewable queue (near-misses)
      // and a corpus status (types with no confirmed find) — because one number
      // ("N missing") read as a to-do that no examiner action could ever clear.
      // Every other step is a single drainable queue and uses the plain badge.
      if (s.key === "vital_docs" && s.extra && s.extra.available) {
        titleRow.appendChild(guidedVitalCount(s.extra));
      } else {
        titleRow.appendChild(el("span", "gcount",
          s.count ? esc(num(s.count) + " to do") : "clear"));
      }
      gbody.appendChild(titleRow);
      // Bulk-triage pager entry (review-queue-bulk-triage.md). The quarantine step's
      // main link already IS the pager (/review?group=quarantine); the vital step's
      // main link goes to Documents (the full checklist), so offer the paged flow as
      // a secondary affordance whenever it has a non-empty queue.
      if (s.key === "vital_docs" && s.extra && s.extra.available &&
          ((s.extra.unconfirmed || 0) + (s.extra.near_misses || 0)) > 0) {
        var pl = el("a", "glink-pager", "Bulk review →");
        pl.href = "/review?group=vital";
        gbody.appendChild(pl);
      }
      if (s.key === "vital_docs" && s.extra && s.extra.capped_targets) {
        // The near-miss lists are a floor, not the whole field — say so, and where
        // to look. The per-type detail (which types, and the cap) is on Documents.
        gbody.appendChild(el("div", "gnote",
          esc(num(s.extra.capped_targets) + " document type" +
              (s.extra.capped_targets === 1 ? "" : "s") +
              " hit the retrieval limit" +
              (s.extra.per_target_k ? " of " + num(s.extra.per_target_k) : "") +
              " — more candidates may exist; see Documents.")));
      }
      if (s.extra && s.extra.ocr_manual_review) {
        gbody.appendChild(el("div", "gnote",
          esc(num(s.extra.ocr_manual_review) + " OCR items also need a manual read")));
      }
      var ack = el("button", "act small" + (s.acknowledged ? " primary" : ""),
        s.acknowledged ? "Marked done" : "Mark done");
      ack.onclick = function () {
        var decision = s.acknowledged ? "reject" : "accept";
        doVerb("/api/confirm", { queue: "guided_progress", id: s.key, decision: decision },
          s.acknowledged ? "Reopened step" : "Marked done")
          .then(function (x) { if (x) render(); });
      };
      gbody.appendChild(ack);
      li.appendChild(gbody);
      list.appendChild(li);
    });
    main.appendChild(list);
    var h = d.handoff || {};
    var card = el("section", "handoff-card" + (h.ready ? " ready" : ""));
    card.appendChild(el("h2", null, "Ready for family handoff?"));
    if (h.delivery_blocked) {
      card.appendChild(el("p", "handoff-block",
        esc("Delivery is blocked: " + (h.reasons || []).join("; "))));
    } else if (!h.all_steps_done) {
      card.appendChild(el("p", "handoff-note", "Finish the steps above, then delivery is clear."));
    } else {
      card.appendChild(el("p", "handoff-ok",
        "All steps done and the export gate is clear — ready to hand off."));
    }
    // The release signature — a named human, not the machine, releases the bundle.
    signoffPanel(card, h);
    main.appendChild(card);
  };

  // The examiner's sign-off panel. Shows the current release state (signed_by /
  // signed_at / revoked / stale) and, when the gate is clear, the form that drives
  // POST /api/signoff. The wet-ink certificate is the authoritative artifact — the
  // panel says so, and points at output/examiner/release_certificate.md.
  function signoffPanel(card, handoff) {
    var st = RELEASE_STATUS || {};
    var box = el("section", "signoff");
    box.appendChild(el("h3", null, "Release signature"));
    if (st.state === "released") {
      box.appendChild(el("p", "signoff-state ok",
        "Released" + (st.signed_by ? " by " + esc(st.signed_by) : "")
        + (st.signed_at ? " on " + esc(st.signed_at) : "")
        + ". The wet-ink certificate is output/examiner/release_certificate.md — "
        + "print it, read it, and sign it by hand."));
      return;
    }
    if (st.state === "revoked") {
      box.appendChild(el("p", "signoff-state warn", "This release was revoked."));
    } else if (st.state === "stale") {
      box.appendChild(el("p", "signoff-state warn",
        "The archive changed after it was released; re-sign to reopen it."));
    }
    if (handoff && (handoff.delivery_blocked || handoff.all_steps_done === false)) {
      box.appendChild(el("p", "signoff-note",
        "Finish every disposition above — release or discard each quarantine item, "
        + "keep or waive each human-review item — before you can sign."));
      card.appendChild(box);
      return;
    }
    var form = el("div", "signoff-form");
    var name = el("input", "signoff-input"); name.placeholder = "Your full name";
    var cap = el("input", "signoff-input"); cap.placeholder = "Your capacity (e.g. Estate attorney)";
    var jud = el("textarea", "signoff-judgment");
    jud.placeholder = "In your own words: why release this set to the family? "
      + "(You are confirming a category rule over an inspectable set — not that "
      + "you read every item.)";
    var btn = el("button", "signoff-btn", "Sign & release");
    var msg = el("p", "signoff-msg");
    btn.addEventListener("click", function () {
      msg.textContent = "";
      postJSON("/api/signoff", { name: name.value.trim(), capacity: cap.value.trim(),
                                 judgment: jud.value.trim() })
        .then(function (r) {
          if (r.ok) { render(); }
          else { msg.textContent = (r.j && r.j.error) || "Sign-off refused."; }
        });
    });
    form.appendChild(name); form.appendChild(cap); form.appendChild(jud);
    form.appendChild(btn); form.appendChild(msg);
    box.appendChild(form);
    card.appendChild(box);
  }

  function transparencyReview(main) {
    if (!EXAMINER) return;
    getJSON("/api/transparency").then(function (t) {
      if (!t) return;
      var sec = el("section", "trust-review");
      sec.appendChild(el("h2", null, "Set-aside & noise (examiner)"));
      var meta = el("p", "trust-meta");
      meta.textContent = "Suspense (corrupt/unreadable): " + num(t.suspense_count || 0) +
        " · Near-duplicate groups: " + num(t.near_duplicate_groups || 0);
      sec.appendChild(meta);
      var noise = t.significant_attachment_noise || [];
      if (noise.length) {
        sec.appendChild(el("h3", null,
          "Noise-triaged emails with a significant attachment (" +
          num(t.significant_attachment_total || noise.length) + ")"));
        var tbl = el("table");
        tbl.innerHTML = "<tr><th>From</th><th>Subject</th><th>When</th></tr>";
        noise.forEach(function (e) {
          var tr = el("tr");
          tr.innerHTML = "<td>" + esc(e.from) + "</td><td>" + esc(e.subject) +
            "</td><td class='preview'>" + esc((e.date || "").replace("T", " ")) + "</td>";
          tbl.appendChild(tr);
        });
        sec.appendChild(tbl);
      }
      main.appendChild(sec);
    }).catch(function () { });
  }

  P.history = function (main, rows) {
    head(main, "Audit", "History", "Every action taken in this archive. " + rows.length + " entries.");
    if (EXAMINER) {  // one-shot reset back to the as-delivered state
      var rst = el("button", "btn danger", "Reset all changes");
      rst.onclick = function () {
        if (!confirm("Undo ALL changes and restore this case to its original delivered state? "
          + "This clears every Confirm/Discard/Rename/Release and the history. It cannot be undone.")) return;
        // Immediate disabled/label feedback (matches Discard/Move/Release elsewhere) —
        // reversing every action on a large case can take a while, and with no
        // in-flight state the button looked frozen/no-op'd for that whole stretch (#29).
        rst.disabled = true; rst.textContent = "Resetting…";
        doVerb("/api/reset", {}, "Reset to as-delivered state").then(function (x) {
          if (!x) { rst.disabled = false; rst.textContent = "Reset all changes"; return; }
          render();
        });
      };
      main.appendChild(rst);
    }
    var undone = {};
    rows.forEach(function (e) { if (e.undoes) undone[e.undoes] = 1; });
    // #24: client-side sort (all rows are already in memory) — click When/Action/
    // Type to toggle asc/desc, keyed on the same histAction(e)/histType(e) values
    // the cells already display. A stable secondary sort on `ts` (desc) keeps
    // equal Action/Type groups chronological. Default stays newest-first.
    var SORT = { key: "ts", dir: "desc" };
    var tbl = el("table");
    var thead = el("tr");
    var headerLinks = {};
    [["ts", "When"], ["action", "Action"], ["type", "Type"]].forEach(function (c) {
      var key = c[0], th = el("th");
      var a = el("a", "sorth", c[1]);
      a.href = "#";
      keyable(a, "button", "Sort by " + c[1]);
      a.onclick = function (ev) {
        ev.preventDefault();
        if (SORT.key === key) SORT.dir = SORT.dir === "asc" ? "desc" : "asc";
        else { SORT.key = key; SORT.dir = key === "ts" ? "desc" : "asc"; }
        draw();
      };
      headerLinks[key] = a;
      th.appendChild(a);
      thead.appendChild(th);
    });
    thead.appendChild(el("th", null, "What"));
    thead.appendChild(el("th"));
    tbl.appendChild(thead);
    var tbody = el("tbody"); tbl.appendChild(tbody);
    main.appendChild(tbl);
    if (!rows.length) main.appendChild(el("p", "notice", "No actions yet."));

    function sortKey(e, key) {
      if (key === "action") return histAction(e);
      if (key === "type") return histType(e);
      return e.ts || "";   // "ts"
    }
    function draw() {
      Object.keys(headerLinks).forEach(function (key) {
        headerLinks[key].className = "sorth" + (SORT.key === key ? " active " + SORT.dir : "");
      });
      var sorted = rows.slice().sort(function (a, b) {
        var ka = sortKey(a, SORT.key), kb = sortKey(b, SORT.key);
        var cmp = ka < kb ? -1 : ka > kb ? 1 : 0;
        if (SORT.dir === "desc") cmp = -cmp;
        if (cmp !== 0) return cmp;
        return (b.ts || "").localeCompare(a.ts || "");   // stable secondary: newest-first
      });
      tbody.innerHTML = "";
      sorted.forEach(function (e) {
        var tr = el("tr");
        var canUndo = EXAMINER && e.reversible && !undone[e.undo_token] && !e.undoes;
        var target = histTarget(e);
        var what = esc(histLabel(e));
        tr.innerHTML = "<td class='preview'>" + esc((e.ts || "").replace("T", " ")) + "</td><td>" +
          esc(histAction(e)) +
          "</td><td><span class='badge'>" + esc(histType(e)) + "</span></td><td>" +
          (target ? "<a href='#' class='whatlink'>" + what + "</a>" : what) + "</td><td></td>";
        if (target) {
          var wl = tr.querySelector("a.whatlink");
          if (wl) wl.onclick = function (ev) { ev.preventDefault(); go(target); };
        }
        if (canUndo) {
          var bn = el("button", "btn small", "Undo");
          bn.onclick = function () {
            postJSON("/api/undo", { undo_token: e.undo_token }).then(function (res) {
              if (res.ok && res.j.ok !== false) { render(); return; }
              toast("Couldn't undo: " + (res.j.error || "error"));
            }).catch(function (err) { toast("Couldn't undo: " + (err && err.message ? err.message : "server error")); });
          };
          tr.lastChild.appendChild(bn);
        }
        tbody.appendChild(tr);
      });
    }
    draw();
  };

  // ── Search (FTS5) — full-text results page (family-archive-full-text-search.md) ──
  // The server-produced snippet carries the intended <mark>…</mark> highlight over
  // RAW estate text. Escape the WHOLE snippet for HTML, then un-escape ONLY the two
  // mark tags — so estate content can never inject markup, but the highlight renders.
  // (A body that literally contains "<mark>" would at worst show a stray highlight;
  // <mark> has no attributes/handlers, so this is never an XSS sink.)
  function markSnippet(s) {
    return esc(s).replace(/&lt;mark&gt;/g, "<mark>").replace(/&lt;\/mark&gt;/g, "</mark>");
  }
  var KIND_LABEL = {
    document: "Documents", email: "Emails", conversation: "Messages",
    audio: "Recordings", photo: "Photos", person: "People"
  };
  function kindLabel(k) { return KIND_LABEL[k] || (pretty(k) || "Other"); }

  P.search = function (main) {
    if (Q.q) setCrumb("Search: " + Q.q);
    var q = Q.q || "";
    head(main, "Search", q ? "Results for “" + q + "”" : "Search the archive", "");
    // On-page search box (mirrors the rail; prefilled with the current query).
    var form = el("form", "searchpage-form");
    var input = el("input", "searchpage-input");
    input.type = "search"; input.value = q; input.autocomplete = "off";
    input.placeholder = "Search letters, emails, transcripts, names…";
    input.setAttribute("aria-label", "Search the archive");
    var btn = el("button", "btn primary", "Search"); btn.type = "submit";
    form.appendChild(input); form.appendChild(btn);
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var v = input.value.trim();
      location.href = v ? "/search?q=" + encodeURIComponent(v) : "/search";
    });
    main.appendChild(form);

    var status = el("div", "search-status"); main.appendChild(status);
    var results = el("div", "search-results"); main.appendChild(results);
    if (!q) {
      status.appendChild(el("p", "notice",
        "Type a word or phrase to search across the whole archive — the text of "
        + "letters and documents, email and message bodies, and transcripts."));
      return;
    }

    var LIMIT = 30, loaded = 0, total = 0, polling = false, moreBtn = null;

    function renderHits(hits) {
      // Group by kind so results read as sections; preserve rank order within.
      var order = [], byKind = {};
      hits.forEach(function (h) {
        var k = h.kind || "other";
        if (!byKind[k]) { byKind[k] = []; order.push(k); }
        byKind[k].push(h);
      });
      order.forEach(function (k) {
        results.appendChild(el("h2", "search-group", esc(kindLabel(k))));
        byKind[k].forEach(function (h) {
          var card = el("div", "search-hit");
          card.appendChild(el("div", "search-hit-title", esc(h.title || "(untitled)")));
          var snip = el("div", "search-hit-snip");
          snip.innerHTML = markSnippet(h.snippet || "");
          card.appendChild(snip);
          card.setAttribute("role", "button"); card.tabIndex = 0;
          // The hit's own title names the destination crumb, so a search result
          // opened from here reads "Search › Re: kombucha order" and one click
          // returns to the result list with the query intact.
          var open = function () {
            go(searchTarget({ p: h.page, k: h.kind, h: h.ref }), { label: h.title || "(untitled)" });
          };
          card.addEventListener("click", open);
          card.addEventListener("keydown", function (e) {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
          });
          results.appendChild(card);
        });
      });
    }

    function load(offset, append) {
      getJSON("/api/search?q=" + encodeURIComponent(q) + "&offset=" + offset + "&limit=" + LIMIT)
        .then(function (d) {
          if (d.building) {
            polling = true;
            status.innerHTML = "";
            var ph = (d.progress && d.progress.phase) ? " (" + esc(d.progress.phase) + ")" : "";
            status.appendChild(el("p", "notice",
              "Building the full-text index for the first time…" + ph
              + " Showing quick matches meanwhile; full results will appear shortly."));
            results.innerHTML = ""; loaded = 0;
            renderHits(d.hits || []);
            loaded = (d.hits || []).length;
            setTimeout(function () { load(0, false); }, 1500);
            return;
          }
          polling = false;
          if (!append) { results.innerHTML = ""; loaded = 0; status.innerHTML = ""; }
          total = d.total || 0;
          if (!total && offset === 0) {
            status.appendChild(el("p", "notice", "No matches for “" + esc(q) + "”."));
            return;
          }
          status.appendChild(el("p", "search-count",
            num(total) + " match" + (total === 1 ? "" : "es")));
          renderHits(d.hits || []);
          loaded += (d.hits || []).length;
          if (moreBtn) { moreBtn.remove(); moreBtn = null; }
          if (loaded < total) {
            moreBtn = el("button", "btn more", "Load more");
            moreBtn.addEventListener("click", function () {
              moreBtn.disabled = true; load(loaded, true);
            });
            main.appendChild(moreBtn);
          }
        }).catch(function (e) {
          status.innerHTML = "";
          status.appendChild(el("p", "notice", "Search failed: " + esc(e.message)));
        });
    }
    load(0, false);
  };

  // ── the release gate (E5) ──
  // The family GET surface is default-closed: bodies (/media, /api/<section>)
  // refuse until a named human releases the case. So EVERY page branches to
  // /api/release-status FIRST and renders a banner, BEFORE any section fetch —
  // otherwise a default-closed page would render an empty shell of 403s (§3).
  var RELEASE_STATUS = null;

  function releaseBanner(status) {
    if (!status) return null;
    if (status.valid) {
      // Released. Family sees nothing; the examiner gets a subtle confirmation.
      if (EXAMINER && status.state === "released") {
        var okb = el("div", "release-banner ok");
        okb.innerHTML = "Released"
          + (status.signed_by ? " by " + esc(status.signed_by) : "")
          + (status.signed_at ? " on " + esc(status.signed_at) : "") + ".";
        return okb;
      }
      return null;
    }
    var headings = { legacy_unsigned: "Not yet released", revoked: "Release revoked",
                     stale: "Release paused", invalid: "Release unreadable" };
    var b = el("div", "release-banner warn");
    b.innerHTML = "<strong>" + esc(headings[status.state] || "Not released") + ".</strong> "
      + esc(status.message || "This archive has not been released.");
    return b;
  }

  function renderReleaseClosed(main, status) {
    main.appendChild(el("p", "notice",
      "The family archive is closed until a named person releases it. "
      + "Nothing here is available yet."));
  }

  // ── boot / render ──
  function render() {
    document.body.classList.remove("selecting");  // exit any drag-select mode on nav/re-render
    destroyGrids();                               // drop stale virtual-grid scroll listeners
    VIEW.removeItems = null;                      // pages re-register their in-place hooks
    CRUMB.label = null;                           // filtered views re-declare their crumb label
    CRUMB.node = null;
    var main = shell();
    // One central call, not one per page. Per-page breadcrumb calls are exactly how
    // this app ended up with a back link on People and nothing at all on Events,
    // Collections and the video views — every new page had to remember. Drawn from
    // the URL's own trail, so a page that carries no trail shows nothing.
    if (parseTrail(Q.from).length) breadcrumb(main);
    var page = CTX.page;
    var api = "/api/" + page;
    function loadSection() {
      getJSON(api).then(function (data) {
        (P[page] || function (m) { m.appendChild(el("p", "notice", "Page not found.")); })(main, data);
      }).catch(function (e) {
        main.appendChild(el("p", "notice", "Couldn't load: " + esc(e.message)));
      });
    }
    getJSON("/api/release-status").then(function (status) {
      RELEASE_STATUS = status;
      var banner = releaseBanner(status);
      if (banner) main.appendChild(banner);
      // Family + not released → default-closed: show the banner, skip the body.
      if (!EXAMINER && status && status.valid === false) {
        return renderReleaseClosed(main, status);
      }
      loadSection();
    }).catch(function () {
      // Status endpoint missing/unreachable (older server) → normal behavior.
      loadSection();
    });
  }

  document.addEventListener("DOMContentLoaded", render);
})();
