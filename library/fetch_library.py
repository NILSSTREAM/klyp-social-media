#!/usr/bin/env python3
"""Fill the Klyp media library (runs in GitHub Actions, where the internet is open).

  python fetch_library.py --repo OWNER/NAME --defaults defaults.json \
      --request request.json --work /tmp/lib --publish

What it does:
  1. Reads the current manifest from the release "library" of --repo (if any).
  2. Works out what to fetch: the pending request in request.json (an id not yet in
     manifest.requests_done), plus a top-up to the targets in defaults.json.
  3. Backgrounds, from the first source that has enough usable photos:
       Pexels (PEXELS_API_KEY, Pexels License), Pixabay (PIXABAY_API_KEY, Pixabay
       Content License, images up to 1280 px so upscaled), Openverse (no key,
       CC0 and public domain photographs only, e.g. StockSnap).
     All three allow commercial use without attribution. Drops photos whose
     description or tags mention people, faces, logos or text; vertical only;
     crops to 1080x1920 JPEG.
  4. Music: Openverse API, category music, licences CC0 and CC BY only (both allow
     commercial use and edits); 30 s to 6 min; converted to MP3 192 kbps. CC BY
     tracks carry the credit line that must go in the caption.
  5. Uploads new files to the release, then manifest.json last (readers only see
     complete entries).

Environment: PIXABAY_API_KEY and/or PEXELS_API_KEY (optional, more and better
backgrounds; without either, Openverse CC0 only), OPENVERSE_CLIENT_ID and
OPENVERSE_CLIENT_SECRET (optional, higher rate limit), GH_TOKEN (for gh).
Only the Python standard library, Pillow, ffmpeg and gh are needed.
"""

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TAG = "library"
UA = "klyp-media-library/1.0 (+https://klyp.it; GitHub Actions)"
W, H = 1080, 1920
MUSIC_MIN_S, MUSIC_MAX_S = 30, 360

PEOPLE = re.compile(
    r"\b(person|people|man|men|woman|women|girl|girls|boy|boys|child|children|kid|kids|baby|face|faces|"
    r"portrait|selfie|couple|family|crowd|audience|hand|hands|finger|fingers|model|lady|guy|friends|someone|"
    r"player|athlete|runner|dancer|musician|dj|singer|bride|groom|worker|chef|baker|barista|student|"
    r"silhouette|human|body|skin|feet|legs|he|she|his|her|their)\b", re.I)
BRANDS = re.compile(r"\b(logo|brand|sign|signage|text|letters|word|words|quote|apple|iphone|macbook|nike|adidas|"
                    r"coca|starbucks|samsung|google|instagram|tiktok|youtube)\b", re.I)


# ---------------------------------------------------------------- helpers

def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def http(url, headers=None, data=None, timeout=60, retries=3):
    h = {"User-Agent": UA, **(headers or {})}
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(5 * (i + 1))
                continue
            raise
        except urllib.error.URLError as e:
            last = e
            time.sleep(3 * (i + 1))
    raise last


def http_json(url, headers=None, data=None):
    return json.loads(http(url, headers, data))


def gh(*args, check=True):
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=check)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- manifest

def empty_manifest():
    return {"version": 1, "updated": None, "backgrounds": [], "music": [], "requests_done": [], "runs": []}


def load_prev(repo, work, prev_path=None):
    if prev_path:
        p = Path(prev_path)
        return json.loads(p.read_text()) if p.exists() else empty_manifest()
    d = Path(work) / "prev"
    d.mkdir(parents=True, exist_ok=True)
    r = gh("release", "download", TAG, "--repo", repo, "-p", "manifest.json", "-D", str(d), "--clobber", check=False)
    p = d / "manifest.json"
    if r.returncode != 0 or not p.exists():
        log("no previous manifest, starting a new library")
        return empty_manifest()
    m = json.loads(p.read_text())
    for k, v in empty_manifest().items():
        m.setdefault(k, v)
    return m


# ---------------------------------------------------------------- planning

def plan(defaults, request, manifest):
    jobs_bg, jobs_music = [], []
    req_id = None
    if request and request.get("id") and request["id"] not in manifest["requests_done"]:
        req_id = request["id"]
        for b in request.get("backgrounds", []):
            jobs_bg.append({"query": b["query"], "tags": b.get("tags", []), "count": int(b.get("count", 4)),
                            "color": b.get("color"), "source": f"request:{req_id}"})
        for m in request.get("music", []):
            jobs_music.append({"q": m["q"], "mood": m.get("mood", "chill"), "count": int(m.get("count", 3)),
                               "source": f"request:{req_id}"})

    tgt = defaults.get("targets", {})
    per_tag = int(tgt.get("backgrounds_per_tag", 4))
    have_tag = {}
    for b in manifest["backgrounds"]:
        if b.get("blocked"):
            continue
        for t in b.get("tags", []):
            have_tag[t] = have_tag.get(t, 0) + 1
    planned_tag = {}
    for b in defaults.get("backgrounds", []):
        missing = min(per_tag - have_tag.get(t, 0) - planned_tag.get(t, 0) for t in b["tags"])
        if missing > 0:
            n = min(missing, 3)
            jobs_bg.append({"query": b["query"], "tags": b["tags"], "count": n, "source": "defaults"})
            for t in b["tags"]:
                planned_tag[t] = planned_tag.get(t, 0) + n

    per_mood = int(tgt.get("music_per_mood", 4))
    have_mood = {}
    for m in manifest["music"]:
        if not m.get("blocked"):
            have_mood[m["mood"]] = have_mood.get(m["mood"], 0) + 1
    queries = {}
    for m in defaults.get("music", []):
        queries.setdefault(m["mood"], []).append(m["q"])
    for mood, qs in queries.items():
        missing = per_mood - have_mood.get(mood, 0)
        for i, q in enumerate(qs):
            share = missing // len(qs) + (1 if i < missing % len(qs) else 0)
            if share > 0:
                jobs_music.append({"q": q, "mood": mood, "count": share, "source": "defaults"})
    return req_id, jobs_bg, jobs_music


# ---------------------------------------------------------------- backgrounds

def cover_crop(raw, out):
    """Crop to 1080x1920 and save. Returns the source size (w, h)."""
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im).convert("RGB")
    size = im.size
    im = ImageOps.fit(im, (W, H), method=Image.LANCZOS, centering=(0.5, 0.5))
    im.save(out, "JPEG", quality=88, optimize=True, progressive=True)
    return size


def src_pexels(job, page, key):
    q = {"query": job["query"], "orientation": "portrait", "size": "large", "per_page": 40, "page": page}
    if job.get("color"):
        q["color"] = job["color"]
    res = http_json("https://api.pexels.com/v1/search?" + urllib.parse.urlencode(q), headers={"Authorization": key})
    for p in res.get("photos", []):
        orig = p["src"]["original"]
        yield {"id": f"pexels-{p['id']}", "desc": (p.get("alt") or "").strip(), "w": p.get("width"), "h": p.get("height"),
               "download": orig + ("&" if "?" in orig else "?") + "auto=compress&cs=tinysrgb&fit=crop&w=1080&h=1920",
               "source": "pexels", "source_url": p.get("url"), "author": p.get("photographer"),
               "author_url": p.get("photographer_url"), "license": "pexels",
               "license_url": "https://www.pexels.com/license/", "avg_color": p.get("avg_color"),
               "credit": f"Photo by {p.get('photographer')} on Pexels"}


def src_pixabay(job, page, key):
    q = {"key": key, "q": job["query"][:100], "image_type": "photo", "orientation": "vertical", "safesearch": "true",
         "min_height": 1200, "per_page": 50, "page": page}
    res = http_json("https://pixabay.com/api/?" + urllib.parse.urlencode(q))
    for p in res.get("hits", []):
        url = p.get("fullHDURL") or p.get("largeImageURL")
        if not url:
            continue
        yield {"id": f"pixabay-{p['id']}", "desc": (p.get("tags") or "").strip(), "w": p.get("imageWidth"),
               "h": p.get("imageHeight"), "download": url, "source": "pixabay", "source_url": p.get("pageURL"),
               "author": p.get("user"), "author_url": f"https://pixabay.com/users/{p.get('user')}-{p.get('user_id')}/",
               "license": "pixabay", "license_url": "https://pixabay.com/service/license-summary/",
               "credit": f"Image by {p.get('user')} from Pixabay"}


def src_openverse(job, page, headers):
    q = {"q": job["query"], "license": "cc0,pdm", "category": "photograph", "aspect_ratio": "tall", "size": "large",
         "page_size": 20 if not headers else 50, "page": page, "mature": "false", "filter_dead": "true"}
    res = http_json("https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(q), headers=headers)
    for p in res.get("results", []):
        if (p.get("license") or "").lower() not in ("cc0", "pdm") or not p.get("url"):
            continue
        tags = " ".join(t.get("name", "") for t in (p.get("tags") or []) if isinstance(t, dict))
        yield {"id": "ovimg-" + str(p.get("id", "")).replace("-", "")[:16], "desc": f"{p.get('title') or ''} {tags}".strip(),
               "w": p.get("width"), "h": p.get("height"), "download": p["url"],
               "source": f"openverse/{p.get('source') or p.get('provider')}", "source_url": p.get("foreign_landing_url"),
               "author": p.get("creator"), "author_url": p.get("creator_url"), "license": (p.get("license") or "").lower(),
               "license_url": p.get("license_url"),
               "credit": f"Photo by {p.get('creator') or 'unknown'} ({(p.get('license') or '').upper()}, via Openverse)"}


def background_sources(ov_headers):
    srcs = []
    if os.environ.get("PEXELS_API_KEY"):
        srcs.append(("pexels", lambda j, pg: src_pexels(j, pg, os.environ["PEXELS_API_KEY"])))
    if os.environ.get("PIXABAY_API_KEY"):
        srcs.append(("pixabay", lambda j, pg: src_pixabay(j, pg, os.environ["PIXABAY_API_KEY"])))
    srcs.append(("openverse", lambda j, pg: src_openverse(j, pg, ov_headers)))
    return srcs


def fetch_backgrounds(jobs, manifest, outdir, ov_headers=None):
    """Fill each job from the sources in order: Pexels (key), Pixabay (key), Openverse CC0 (no key)."""
    added = []
    if not jobs:
        return added
    sources = background_sources(ov_headers or {})
    log("background sources: " + ", ".join(n for n, _ in sources))
    known = {b["id"] for b in manifest["backgrounds"]}
    for job in jobs:
        got = 0
        for sname, search in sources:
            page = 1
            while got < job["count"] and page <= 3:
                try:
                    cands = list(search(job, page))
                except Exception as e:  # noqa: BLE001
                    log(f"WARN {sname} search {job['query']!r} failed: {e}")
                    break
                if not cands:
                    break
                for c in cands:
                    if got >= job["count"]:
                        break
                    if c["id"] in known:
                        continue
                    w, h = c.get("w") or 0, c.get("h") or 0
                    if w and h and (w >= h or h < 1200):
                        continue
                    if PEOPLE.search(c["desc"]) or BRANDS.search(c["desc"]):
                        continue
                    name = f"bg-{c['id']}.jpg"
                    out = outdir / name
                    try:
                        sw, sh = cover_crop(http(c["download"], timeout=120), out)
                    except Exception as e:  # noqa: BLE001
                        log(f"WARN download {c['id']} failed: {e}")
                        out.unlink(missing_ok=True)
                        continue
                    if sh < 1200 or sw >= sh:
                        out.unlink(missing_ok=True)
                        continue
                    checks = []
                    if not c["desc"]:
                        checks.append("no description, check by eye")
                    if sh < H:
                        checks.append(f"upscaled from {sw}x{sh}")
                    entry = {
                        "id": c["id"], "file": name, "kind": "background", "tags": job["tags"], "query": job["query"],
                        "alt": c["desc"][:200], "avg_color": c.get("avg_color"), "width": W, "height": H,
                        "source_size": [sw, sh], "bytes": out.stat().st_size, "sha256": sha256(out),
                        "source": c["source"], "source_url": c.get("source_url"), "photographer": c.get("author"),
                        "photographer_url": c.get("author_url"), "license": c["license"],
                        "license_url": c.get("license_url"), "credit": c["credit"], "needs_credit": False,
                        "check": "; ".join(checks), "added": now(), "origin": job["source"],
                    }
                    added.append(entry)
                    known.add(c["id"])
                    got += 1
                    log(f"bg  + {name}  [{', '.join(job['tags'])}]  {c['desc'][:70]}")
                page += 1
            if got >= job["count"]:
                break
        if got < job["count"]:
            log(f"WARN {job['query']!r}: {got}/{job['count']} usable photos")
    return added


# ---------------------------------------------------------------- music (Openverse)

def openverse_headers():
    cid, sec = os.environ.get("OPENVERSE_CLIENT_ID"), os.environ.get("OPENVERSE_CLIENT_SECRET")
    if not (cid and sec):
        return {}
    try:
        body = urllib.parse.urlencode({"client_id": cid, "client_secret": sec, "grant_type": "client_credentials"}).encode()
        tok = http_json("https://api.openverse.org/v1/auth_tokens/token/", data=body,
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        return {"Authorization": f"Bearer {tok['access_token']}"}
    except Exception as e:  # noqa: BLE001
        log(f"WARN openverse token failed, going anonymous: {e}")
        return {}


def seconds_of(track):
    d = track.get("duration")
    if not d:
        return None
    return d / 1000.0 if d > 1000 else float(d)


def probe_seconds(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def credit_line(t):
    lic = (t.get("license") or "").lower()
    ver = t.get("license_version") or ""
    title = t.get("title") or "Untitled"
    creator = t.get("creator") or "unknown artist"
    if lic == "cc0":
        return f'Music: "{title}" by {creator} (CC0)'
    return f'Music: "{title}" by {creator}, CC BY {ver}'.strip()


def fetch_music(jobs, manifest, outdir, headers=None):
    added = []
    if not jobs:
        return added
    headers = headers or {}
    page_size = 20 if not headers else 50
    known = {m["id"] for m in manifest["music"]}
    for job in jobs:
        got, page = 0, 1
        while got < job["count"] and page <= 3:
            q = {"q": job["q"], "category": "music", "license": "cc0,by", "page_size": page_size, "page": page,
                 "filter_dead": "true", "mature": "false"}
            try:
                res = http_json("https://api.openverse.org/v1/audio/?" + urllib.parse.urlencode(q), headers=headers)
            except Exception as e:  # noqa: BLE001
                log(f"WARN openverse search {job['q']!r} failed: {e}")
                break
            results = res.get("results", [])
            if not results:
                break
            for t in results:
                if got >= job["count"]:
                    break
                lic = (t.get("license") or "").lower()
                if lic not in ("cc0", "by"):
                    continue
                tid = "ov-" + str(t.get("id", "")).replace("-", "")[:16]
                if tid in known or not t.get("url"):
                    continue
                secs = seconds_of(t)
                if secs is not None and not (MUSIC_MIN_S <= secs <= MUSIC_MAX_S):
                    continue
                raw = outdir / f"{tid}.src"
                name = f"music-{tid}.mp3"
                out = outdir / name
                try:
                    raw.write_bytes(http(t["url"], timeout=180))
                    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-vn",
                                    "-ar", "44100", "-ac", "2", "-codec:a", "libmp3lame", "-b:a", "192k", str(out)],
                                   check=True)
                except Exception as e:  # noqa: BLE001
                    log(f"WARN music {tid} failed: {e}")
                    out.unlink(missing_ok=True)
                    continue
                finally:
                    raw.unlink(missing_ok=True)
                secs = probe_seconds(out) or secs
                if not secs or not (MUSIC_MIN_S <= secs <= MUSIC_MAX_S):
                    out.unlink(missing_ok=True)
                    continue
                entry = {
                    "id": tid, "file": name, "kind": "music", "mood": job["mood"], "query": job["q"],
                    "title": t.get("title"), "creator": t.get("creator"), "creator_url": t.get("creator_url"),
                    "seconds": round(secs, 1), "bytes": out.stat().st_size, "sha256": sha256(out),
                    "source": f"openverse/{t.get('source') or t.get('provider')}",
                    "source_url": t.get("foreign_landing_url"), "license": lic,
                    "license_version": t.get("license_version"), "license_url": t.get("license_url"),
                    "credit": credit_line(t), "needs_credit": lic != "cc0",
                    "attribution": t.get("attribution"), "added": now(), "origin": job["source"],
                }
                added.append(entry)
                known.add(tid)
                got += 1
                log(f"mus + {name}  [{job['mood']}]  {entry['credit']}  {secs:.0f}s")
            page += 1
        if got < job["count"]:
            log(f"WARN {job['q']!r}: {got}/{job['count']} usable tracks")
    return added


# ---------------------------------------------------------------- publish

def publish(repo, files, manifest_path):
    if gh("release", "view", TAG, "--repo", repo, check=False).returncode != 0:
        gh("release", "create", TAG, "--repo", repo, "--title", "Klyp media library", "--notes",
           "Background photos (Pexels License) and music (CC0 / CC BY, see manifest.json for each credit) "
           "used by the klyp-reel skill. Filled by the klyp media library workflow.")
    for i in range(0, len(files), 20):
        chunk = [str(f) for f in files[i:i + 20]]
        gh("release", "upload", TAG, *chunk, "--repo", repo, "--clobber")
    gh("release", "upload", TAG, str(manifest_path), "--repo", repo, "--clobber")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("REPO"))
    ap.add_argument("--defaults", required=True)
    ap.add_argument("--request")
    ap.add_argument("--work", default="klyp-library-work")
    ap.add_argument("--prev", help="read the previous manifest from this file instead of the release (tests)")
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args()

    work = Path(a.work)
    outdir = work / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    defaults = json.loads(Path(a.defaults).read_text())
    request = None
    if a.request and Path(a.request).exists():
        txt = Path(a.request).read_text().strip()
        request = json.loads(txt) if txt else None
    manifest = load_prev(a.repo, work, a.prev)

    req_id, jobs_bg, jobs_music = plan(defaults, request, manifest)
    log(f"plan: {len(jobs_bg)} background queries, {len(jobs_music)} music queries, request {req_id or 'none'}")
    ov = openverse_headers() if (jobs_bg or jobs_music) else {}
    bgs = fetch_backgrounds(jobs_bg, manifest, outdir, ov)
    mus = fetch_music(jobs_music, manifest, outdir, ov)

    manifest["backgrounds"] += bgs
    manifest["music"] += mus
    if req_id:
        manifest["requests_done"].append(req_id)
    manifest["updated"] = now()
    manifest["runs"] = (manifest.get("runs") or [])[-19:] + [
        {"at": manifest["updated"], "request": req_id, "backgrounds": len(bgs), "music": len(mus)}]
    mpath = outdir / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

    summary = (f"Klyp library: +{len(bgs)} backgrounds, +{len(mus)} tracks "
               f"(total {len(manifest['backgrounds'])} / {len(manifest['music'])}), request {req_id or 'none'}")
    log(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(summary + "\n")
    if a.publish:
        if not a.repo:
            sys.exit("--repo (or REPO) is required to publish")
        publish(a.repo, [outdir / e["file"] for e in bgs + mus], mpath)
        log(f"published to https://github.com/{a.repo}/releases/tag/{TAG}")


if __name__ == "__main__":
    main()
