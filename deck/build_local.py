"""Build a single self-contained HTML file from the deck source.

The deck lives on claude.ai as a Slides artifact, which is the right place for
it afterwards - it exports to PowerPoint and it can be shared. But on the day
itself, a deck that needs a login, a network and six megabytes of runtime
JavaScript to appear on screen is a dependency with no fallback.

This renders the same five slide files into one HTML file that opens straight
from disk. No server, no sign-in, no network except the webfont, which has a
declared fallback.

    python deck/build_local.py

Writes deck/MigrationGuard-ELT.html.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX = HERE / "project" / "deck.json"
SLIDES = HERE / "project" / "slides"
OUT = HERE / "MigrationGuard-ELT.html"

ASIDE = re.compile(r"<aside>(.*?)</aside>", re.DOTALL | re.IGNORECASE)


def load() -> tuple[str, list[dict]]:
    """Read the index and return the title plus slides in deck order."""
    idx = json.loads(INDEX.read_text(encoding="utf-8"))
    order = idx["order"]
    sections = idx.get("sections", {})
    # slide id -> the one-sentence description of the run it starts
    starts = {v["start"]: v.get("description", "") for v in sections.values()}

    out: list[dict] = []
    for sid in order:
        f = SLIDES / f"{sid}.html"
        if not f.is_file():
            raise SystemExit(f"  missing slide file: {f}")
        raw = f.read_text(encoding="utf-8")

        m = ASIDE.search(raw)
        notes = m.group(1).strip() if m else ""
        body = ASIDE.sub("", raw).strip()

        # The slide format pins footers with position:absolute, which needs a
        # positioned ancestor. The artifact runtime supplies one; here the
        # section itself has to be it.
        body = body.replace("<section ", '<section data-local="1" ', 1)

        out.append({"id": sid, "html": body, "notes": notes,
                    "lead": starts.get(sid, "")})
    return idx.get("title", "Deck"), out


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;700&display=swap">
<style>
  :root {
    --chrome-bg: #0b0e13;
    --chrome-fg: #e9eef5;
    --chrome-dim: #8794a6;
    --chrome-line: #222b38;
    --chrome-accent: #6cb0ff;
    --mono: 'JetBrains Mono', ui-monospace, 'Cascadia Mono', Consolas, monospace;
    --sans: 'IBM Plex Sans', system-ui, Arial, sans-serif;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; height: 100%; background: var(--chrome-bg);
    color: var(--chrome-fg); font-family: var(--sans);
    overflow: hidden;
  }
  /* --- stage ------------------------------------------------------ */
  #stage { position: fixed; inset: 0; overflow: hidden; }
  /* The canvas is a fixed 1920x1080 scaled down to fit. It is centred by
     absolute positioning plus translate(-50%,-50%), NOT by flex or grid:
     an auto grid track sizes itself to the item's 1920px max-content, so
     there is no free space left for place-items to centre, and the slide
     lands off-screen. */
  #frame {
    width: 1920px; height: 1080px;
    position: absolute; left: 50%; top: 50%;
    transform: translate(-50%, -50%) scale(0.01);
    transform-origin: center center;
    box-shadow: 0 24px 80px rgba(0,0,0,.6);
    border-radius: 4px; overflow: hidden;
  }
  /* Each slide is wrapped. A section carries its own display (flex or grid)
     in an INLINE style, which beats any stylesheet rule - so hiding slides
     with `#frame section {display:none}` silently does nothing and every
     slide paints on top of the last. The wrapper is hidden instead, and the
     section keeps whatever display it declared. */
  #frame .pane { position: absolute; inset: 0; display: none; }
  #frame .pane.on { display: block; }
  /* Pinned to 1080 so the slide's own flex layout behaves exactly as it
     does on the real canvas - children shrink and `space-between` has a
     fixed height to distribute. fit() grows this only for a slide that
     still overflows after that, so a long slide is scaled down slightly
     rather than losing its last row off the bottom edge. */
  #frame .pane > section {
    position: absolute; left: 0; top: 0;
    width: 1920px; height: 1080px;
  }
  /* The slide format excludes `margin` from the inline styles a slide may
     set, because the runtime zeroes the browser's own defaults. Without
     this reset the UA margins on h1/h2/h3/p/ul add 400-600px to a slide
     and push its last rows off the canvas. */
  #frame section h1, #frame section h2, #frame section h3,
  #frame section p, #frame section ul, #frame section ol,
  #frame section li, #frame section table, #frame section hr,
  #frame section figure, #frame section blockquote { margin: 0; }
  #frame section ul, #frame section ol { padding-left: 1.1em; }
  #frame section table { border-collapse: collapse; }
  #frame section h1, #frame section h2, #frame section h3 {
    font-weight: inherit;
  }
  /* --- notes ------------------------------------------------------ */
  #notes {
    position: fixed; left: 0; right: 0; bottom: 0; max-height: 46vh;
    overflow-y: auto;
    background: #11161e; border-top: 1px solid var(--chrome-line);
    padding: 18px 24px 22px; display: none;
    font-size: 15px; line-height: 1.55; color: #c3d0e0;
  }
  #notes.on { display: block; }
  #notes h4 {
    margin: 0 0 10px; font-family: var(--mono); font-size: 11px;
    letter-spacing: .14em; text-transform: uppercase;
    color: var(--chrome-accent); font-weight: 500;
  }
  #notes p { margin: 0 0 10px; max-width: 110ch; }
  #notes p:last-child { margin-bottom: 0; }
  /* --- bar -------------------------------------------------------- */
  #bar {
    position: fixed; top: 0; left: 0; right: 0; height: 34px;
    display: flex; align-items: center; gap: 18px; padding: 0 16px;
    background: rgba(11,14,19,.86); backdrop-filter: blur(6px);
    border-bottom: 1px solid var(--chrome-line);
    font-family: var(--mono); font-size: 12px; color: var(--chrome-dim);
    user-select: none;
  }
  #bar b { color: var(--chrome-fg); font-weight: 500; }
  #bar .sp { flex: 1; }
  #bar kbd {
    font-family: var(--mono); font-size: 11px; color: var(--chrome-fg);
    background: #1b2330; border: 1px solid var(--chrome-line);
    border-radius: 4px; padding: 1px 5px;
  }
  #lead { color: var(--chrome-dim); font-style: italic;
          max-width: 46ch; overflow: hidden; text-overflow: ellipsis;
          white-space: nowrap; font-family: var(--sans); font-size: 12px; }
  @media print { #bar, #notes { display: none !important; } }
</style>
</head>
<body>

<div id="bar">
  <span><b id="n">1</b> / __COUNT__</span>
  <span id="lead"></span>
  <span class="sp"></span>
  <span><kbd>&larr;</kbd><kbd>&rarr;</kbd> move</span>
  <span><kbd>N</kbd> notes</span>
  <span><kbd>F</kbd> full screen</span>
</div>

<div id="stage"><div id="frame">
__SLIDES__
</div></div>

<div id="notes"><h4>Speaker notes</h4><div id="notesbody"></div></div>

<script>
(function () {
  var NOTES = __NOTES__;
  var LEADS = __LEADS__;
  var slides = Array.prototype.slice.call(
    document.querySelectorAll('#frame .pane'));
  var i = 0;
  var notesOn = false;

  var frame = document.getElementById('frame');
  var stage = document.getElementById('stage');
  var panel = document.getElementById('notes');
  var panelBody = document.getElementById('notesbody');
  var counter = document.getElementById('n');
  var lead = document.getElementById('lead');

  function needed(sec) {
    // How tall this slide really needs to be.
    //
    // NOT scrollHeight: when a flex container's content overflows, the
    // container's bottom padding is left out of scrollHeight. On these
    // slides that padding is 160px and it is exactly the band the pinned
    // footer sits in - so trusting scrollHeight puts the footer straight
    // through the last row of content while reporting zero overflow.
    var cs = getComputedStyle(sec);
    var padBottom = parseFloat(cs.paddingBottom) || 0;
    var lowest = 0;
    for (var n = 0; n < sec.children.length; n++) {
      var el = sec.children[n];
      if (el.tagName === 'ASIDE') { continue; }
      var pos = getComputedStyle(el).position;
      if (pos === 'absolute' || pos === 'fixed') { continue; }
      var bottom = el.offsetTop + el.offsetHeight;
      if (bottom > lowest) { lowest = bottom; }
    }
    return Math.ceil(Math.max(1080, lowest + padBottom));
  }

  var heights = [];   // settled canvas height per slide
  var tallest = 1080; // the one the scale is computed from

  function measureAll() {
    // Settle every slide's height ONCE. A slide has to be visible to
    // measure, so each pane is shown briefly in turn.
    var wasOn = slides[i];
    heights = slides.map(function (pane) {
      pane.classList.add('on');
      var sec = pane.querySelector('section');
      sec.style.height = '1080px';
      var h = needed(sec);
      if (h > 1080) {
        sec.style.height = h + 'px';
        // Growing can change how text wraps; one correction is enough.
        var again = needed(sec);
        if (again > h) { h = again; }
      }
      sec.style.height = h + 'px';
      if (pane !== wasOn) { pane.classList.remove('on'); }
      return h;
    });
    tallest = heights.reduce(function (a, b) { return Math.max(a, b); }, 1080);
  }

  function fit() {
    // ONE scale for the whole deck, taken from the tallest slide. Scaling
    // each slide to fill the window separately would resize the text
    // between slides, which an audience sees immediately.
    var h0 = heights[i] || 1080;
    frame.style.height = h0 + 'px';
    var top = 34;
    var bottom = notesOn ? panel.offsetHeight : 0;
    var w = Math.max(80, stage.clientWidth - 16);
    var h = Math.max(60, stage.clientHeight - top - bottom - 16);
    var s = Math.min(w / 1920, h / tallest);
    frame.style.transform = 'translate(-50%, -50%) scale(' + s + ')';
    frame.style.left = (stage.clientWidth / 2) + 'px';
    frame.style.top = (top + h / 2 + 8) + 'px';
  }

  function show(k) {
    i = Math.max(0, Math.min(slides.length - 1, k));
    slides.forEach(function (s, n) { s.classList.toggle('on', n === i); });
    counter.textContent = String(i + 1);
    lead.textContent = LEADS[i] || '';
    var txt = (NOTES[i] || '').trim();
    panelBody.innerHTML = txt
      ? txt.split(/\\n\\s*\\n/).map(function (p) {
          return '<p>' + p.replace(/\\n/g, ' ') + '</p>';
        }).join('')
      : '<p style="color:#6a7786">No notes on this slide.</p>';
    // replaceState throws a SecurityError on an opaque origin (a data: URL,
    // and some sandboxed file views). The deck must still work, so the
    // bookmarkable hash is a convenience that is allowed to fail - and it
    // must fail BEFORE nothing, not before fit(), or the slide never scales.
    try {
      if (location.hash !== '#slide-' + (i + 1)) {
        history.replaceState(null, '', '#slide-' + (i + 1));
      }
    } catch (err) { /* no addressable hash here; carry on */ }
    fit();
  }

  function toggleNotes() {
    notesOn = !notesOn;
    panel.classList.toggle('on', notesOn);
    fit();
  }

  document.addEventListener('keydown', function (e) {
    var k = e.key;
    if (k === 'ArrowRight' || k === 'PageDown' || k === ' ' || k === 'Enter') {
      show(i + 1); e.preventDefault();
    } else if (k === 'ArrowLeft' || k === 'PageUp' || k === 'Backspace') {
      show(i - 1); e.preventDefault();
    } else if (k === 'Home') { show(0); e.preventDefault(); }
    else if (k === 'End') { show(slides.length - 1); e.preventDefault(); }
    else if (k === 'n' || k === 'N') { toggleNotes(); e.preventDefault(); }
    else if (k === 'f' || k === 'F') {
      if (document.fullscreenElement) { document.exitFullscreen(); }
      else if (document.documentElement.requestFullscreen) {
        document.documentElement.requestFullscreen().catch(function () {});
      }
      e.preventDefault();
    } else if (k >= '1' && k <= '9') {
      show(parseInt(k, 10) - 1); e.preventDefault();
    }
  });

  // Click the right two thirds to advance, the left third to go back.
  stage.addEventListener('click', function (e) {
    show(e.clientX < stage.clientWidth / 3 ? i - 1 : i + 1);
  });

  window.addEventListener('resize', fit);
  window.addEventListener('hashchange', function () {
    var m = /^#slide-(\\d+)$/.exec(location.hash);
    if (m) { show(parseInt(m[1], 10) - 1); }
  });

  var start = 0;
  try {
    var m0 = /^#slide-(\\d+)$/.exec(location.hash);
    if (m0) { start = parseInt(m0[1], 10) - 1; }
  } catch (err) { /* no readable hash; start at the cover */ }
  measureAll();
  show(start);
  // The webfont lands after first paint and changes how text wraps, which
  // changes the heights - so measure again once it has arrived.
  function resettle() { measureAll(); fit(); }
  window.addEventListener('load', resettle);
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(resettle).catch(function () {});
  }
})();
</script>
</body>
</html>
"""


def main() -> int:
    title, slides = load()

    page = (PAGE
            .replace("__TITLE__", html.escape(title))
            .replace("__COUNT__", str(len(slides)))
            .replace("__SLIDES__", "\n\n".join(
                f'<div class="pane">\n{s["html"]}\n</div>' for s in slides))
            .replace("__NOTES__", json.dumps([s["notes"] for s in slides]))
            .replace("__LEADS__", json.dumps([s["lead"] for s in slides])))

    OUT.write_text(page, encoding="utf-8")
    kb = round(len(page.encode("utf-8")) / 1024)
    print(f"  wrote {OUT}  ({kb} KB, {len(slides)} slides)")
    for n, s in enumerate(slides, 1):
        note = f"{len(s['notes'])} chars of notes" if s["notes"] else "no notes"
        print(f"    {n}. {s['id']:<14} {note}")
    print("\n  open it by double-clicking the file - no server, no sign-in")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
