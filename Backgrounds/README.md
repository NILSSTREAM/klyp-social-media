# Backgrounds

The owner's photo backgrounds for the Klyp demo pages. When this folder holds
usable photos, the klyp-reel skill uses only these (the release library
backgrounds are ignored).

- Drop any jpg, png or webp here (portrait works best, 1080x1920 or larger).
- Describe a photo in backgrounds.json: tags (first a tone: light, mid or dark,
  then subjects), license, source, and "usable".
- "usable": false keeps a photo out of the videos, with the reason: readable
  text, a logo or brand, a person, a known character. Set it to true to use it.
- Pages with dark text get a light photo, pages with light text a dark one.
- Tag "calm" (second) a photo with large plain areas (sky, mist, sea): pages
  with a lot of text ask for it, since the glass is clear and never blurred.
- Photos are never blurred on the pages; they may be darkened.
