# PolicyLogic share button — integration guide

A drop-in, client-side carousel generator. No backend. Renders the 10-slide
scorecard carousel on the visitor's device and hands it to the native share
sheet (mobile) or downloads PNGs (desktop fallback).

## Files
- `policylogic-share.js` — the module (the only file you deploy)

## 1. Load fonts and the module

The scorecard pages already use Playfair Display, DM Sans, and DM Mono. Make sure
those are in the page `<head>` (Google Fonts or self-hosted), then add the module:

```html
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,700;0,900;1,700;1,900&family=DM+Sans:wght@300;400;500;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="/policylogic-share.js" defer></script>
```

## 2. Add a button

```html
<button id="share-btn">Share scorecard</button>
```

## 3. Wire it up with the scorecard's data

```html
<script>
  PolicyLogicShare.attach('#share-btn', SCORECARD);
</script>
```

where `SCORECARD` is the data object the page already holds. Field mapping:

| Field      | Type     | Notes                                                        |
|------------|----------|--------------------------------------------------------------|
| `name`     | string   | Official's display name                                      |
| `sub`      | string   | "Party · State · Congress"                                   |
| `since`    | string   | Year first in office                                         |
| `total`    | number   | Total promises tracked (the honest denominator)              |
| `s1`,`s2`  | [label,value] | Two at-a-glance rows                                     |
| `web`      | string   | Official website                                             |
| `rule`     | string   | Selection-rule label, e.g. "ranked by stakes"               |
| `promises` | array    | Pre-sorted; module takes the top 12 automatically            |
| `sources`  | string[] | Source list                                                  |

Each promise:

| Field      | Values                                  |
|------------|------------------------------------------|
| `type`     | "Quantitative" \| "Qualitative" \| "Negative" |
| `text`     | the promise text                         |
| `delivery` | "D0"–"D4"                                |
| `diff`     | "H1" \| "H2" \| "H3"                     |
| `impact`   | short label, e.g. "High" / "Mid" / "Low" |
| `score`    | promise score out of 25                  |
| `detail`   | one-line evidence summary                |

## How the button behaves

- **Mobile (supports Web Share with files):** opens the native share sheet with all
  10 PNGs attached. User picks Save to Photos / Messages / etc., then posts to
  Instagram. (No API can post to IG directly — this is the standard flow.)
- **Desktop / unsupported:** automatically downloads the 10 PNGs in order.
- Same code path; the module feature-detects and falls back on its own.

## Why it's built this way

- **Fonts are awaited before drawing.** Canvas silently substitutes a fallback if a
  font isn't loaded yet, which would ship Times-instead-of-Playfair slides. The
  module calls `document.fonts.load()` for each weight and waits before rendering.
- **Data is passed in, not scraped.** One source of truth — the page's own object.
- **Top-12 + honest denominator.** Module slices to 12 promises and prints
  "showing 12 of N · {rule}" so the selection is always disclosed.
- **Negative = slate, not red.** Promise type colors the accent (a fact about the
  pledge), never the score (which would imply a verdict). Slate avoids the
  red=fail reflex on a context-free shared image.

## Programmatic use (optional)

```js
// Generate + share/download on demand, no button:
PolicyLogicShare.generate(SCORECARD);

// Get the raw canvases (e.g. to preview in-page before sharing):
var canvases = PolicyLogicShare.buildSlides(SCORECARD);
```

## Known constraints

- Web Share with files needs HTTPS and a supporting browser (iOS Safari, Android
  Chrome). Desktop Safari/Firefox fall back to download.
- If you self-host fonts later, keep the same family names ("Playfair Display",
  "DM Sans", "DM Mono") so the `document.fonts.load()` calls still match.
- 10 PNGs at 1080×1080 generate in well under a second on a modern phone; the
  button shows "Preparing…" during generation.
