#!/usr/bin/env python3
"""Music ingester for the jukeplayer collection.

One command, end to end:

    python main.py ingest albums_to_download.json

  1. download   — yt-dlp pulls each playlist's audio AS-IS (bestaudio, m4a
                  preferred; no transcode — gonic serves m4a/opus natively and
                  transcodes for clients on demand).
  2. tag        — always-on pre-tag: artist/album/title/tracknumber (+ year
                  from the spec when present) + GENRE: one canonical genre per
                  file via last.fm artist lookup, fold-matched against the
                  MusicBrainz genre vocabulary (the same canon the collection's
                  bulk enrichment used). gonic treats a genre tag as an opaque
                  string, so everything writes a single clean value.
  3. push       — rsync the batch to store:/mnt/store/music/import_staging/
                  and STOP. You run beets manually on the store (interactive,
                  seeing the matches), then 'python main.py scan' rescans
                  gonic when done.

Beets has no genre plugin here, so the pre-tag genre SURVIVES the import.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import typer
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, TCON
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from mutagen.oggopus import OggOpus as OpusFile
from mutagen.oggvorbis import OggVorbis

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = None

app = typer.Typer()

YTM_URL = "https://music.youtube.com/watch?v=Fnvk1FfMpIw&list={playlist_id}"
MB_API = "https://musicbrainz.org/ws/2"
LASTFM_API = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "jukeplayer-genre-enricher/1.0 (jukeplayer home jukebox)"
STORE_HOST = os.getenv("STORE_SSH", "store")
STORE_STAGING = os.getenv("STORE_STAGING", "/mnt/store/music/import_staging")

# ---------------------------------------------------------------- helpers


def load_env(path: Path = Path(__file__).parent / ".env"):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


ENV = load_env()

# The repo venv's yt-dlp is the one we upgrade; the PATH's global binary can
# be an old build whose extractors are behind YouTube's current protections.
_ytdlp_local = Path(__file__).parent / "venv" / "bin" / "yt-dlp"
YTDLP_BIN = str(_ytdlp_local) if _ytdlp_local.exists() else "yt-dlp"


def _fold(name: str) -> str:
    return "".join(c for c in (name or "").lower() if c.isalnum())


def _cache_path() -> Path:
    d = Path(os.getenv("DOWNLOADER_CACHE", Path(__file__).parent / ".cache"))
    d.mkdir(exist_ok=True)
    return d


class RateLimitedClient:
    """Serialized HTTP JSON fetcher: ~1 req/s, one retry on 429/503."""

    def __init__(self):
        self._last = 0.0

    def get_json(self, url: str) -> dict:
        for attempt in range(3):
            wait = 1.05 - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                if e.code in (429, 503) and attempt < 2:
                    time.sleep(2 * (attempt + 5))
                    continue
                if e.code == 404:
                    return {}
                raise
        return {}


def mb_genre_vocabulary(client: RateLimitedClient) -> set:
    """The full MusicBrainz genre list (paginated), cached on disk."""
    cache_file = _cache_path() / "mb_vocab.json"
    if cache_file.exists():
        with open(cache_file) as fh:
            return set(json.load(fh))
    names, offset, page = [], 0, 100
    while True:
        data = client.get_json(f"{MB_API}/genre/all?fmt=json&limit={page}&offset={offset}")
        batch = data.get("genres", [])
        if not batch:
            break
        names += [g["name"].strip().lower() for g in batch if g.get("name")]
        if len(batch) < page or offset > 5000:
            break
        offset += page
    vocab = {n for n in names if n}
    with open(cache_file, "w") as fh:
        json.dump(sorted(vocab), fh)
    return vocab


def lastfm_artist_genre(client: RateLimitedClient, artist: str, key: str, vocab: set) -> str | None:
    """ONE canonical genre: top artist tag whose folded name is in the MB
    vocabulary. None when nothing matches (file stays untagged; beets import
    does not touch genre either way)."""
    folded_vocab = {_fold(v) for v in vocab}
    params = urllib.parse.urlencode(
        {"method": "artist.getinfo", "artist": artist, "api_key": key, "format": "json"}
    )
    try:
        data = client.get_json(f"{LASTFM_API}?{params}")
        tags = (data.get("artist") or {}).get("tags", {}).get("tag") or []
        if isinstance(tags, dict):
            tags = [tags]
        for tag in tags:
            name = (tag.get("name") or "").strip()
            if name and _fold(name) in folded_vocab:
                return name.title()
    except Exception as e:
        typer.echo(f"  ✗ lastfm lookup failed for {artist!r}: {e}")
    return None


# ---------------------------------------------------------------- pipeline


def download_music(spec_path: Path, output_dir: Path):
    spec = json.loads(spec_path.read_text())
    for item in spec:
        artist, album, playlist_id = item.get("artist"), item.get("album"), item.get("id")
        if not all([artist, album, playlist_id]):
            typer.echo(f"✗ bad spec entry (need artist/album/id): {item}")
            continue
        target = output_dir / artist / album
        target.mkdir(parents=True, exist_ok=True)
        typer.echo(f"⬇ downloading: {artist} - {album} (per-file resume; already-downloaded files skipped by yt-dlp)")
        # audio-only: avoids YouTube's video-CDN 403 wall entirely; the
        # convert step normalizes the container afterwards.
        # player_client=visionos,android_vr: visionos+HLS is the route that
        # survives YouTube's current bot-blocks (measured); android_vr kept
        # as fallback.
        # YTDLP_BROWSER (in .env): download with that browser's logged-in
        # youtube cookies — REQUIRED for explicit/age-gated tracks, which the
        # anonymous client gets no formats for.
        browser = ENV.get("YTDLP_BROWSER", "").strip()
        cmd = [
            YTDLP_BIN,
            "-i",
            "--no-overwrites",
            "-f", "bestaudio/best",
        ]
        if browser:
            # cookies route: the default cookie-supporting clients + the EJS
            # challenge solver (deno/node present on this Mac) — the web
            # client's signature/n-challenges make formats disappear otherwise
            cmd += ["--cookies-from-browser", browser, "--remote-components", "ejs:github"]
        else:
            # anonymous route: force the measured-working clients (both are
            # skipped when cookies are on, so this forcing is conditional)
            cmd += ["--extractor-args", "youtube:player_client=visionos,android_vr"]
        cmd += [
            "-o", f"{output_dir}/{artist}/{album}/%(track_number,playlist_index)02d - %(title)s.%(ext)s",
            YTM_URL.format(playlist_id=playlist_id),
        ]
        subprocess.run(cmd, check=False)


def convert_to_mp3(output_dir: Path):
    """Everything non-mp3 -> mp3 @320k (the established convert flow), original
    deleted on success. Includes m4a/m4b audio containers."""
    for ext in ("webm", "mkv", "mp4", "m4a", "m4b", "opus", "ogg", "wav", "flac"):
        for file in sorted(output_dir.rglob(f"*.{ext}")):
            mp3_file = file.with_suffix(".mp3")
            typer.echo(f"⇉ converting {file.name} -> mp3 @320k")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(file), "-vn", "-ab", "320k", "-f", "mp3", str(mp3_file)],
                    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                file.unlink()
            except Exception as e:
                typer.echo(f"  ✗ convert failed {file.name}: {e}")


def tag_file(path: Path, tags: dict):
    """Write one canonical set of tags; format-specific backends."""
    ext = path.suffix.lower()
    if ext == ".mp3":
        try:
            audio = MP3(path, ID3=EasyID3)
            audio["artist"] = tags["artist"]
            audio["album"] = tags["album"]
            audio["title"] = tags["title"]
            audio["tracknumber"] = tags["tracknumber"]
            if tags.get("year"):
                audio["date"] = tags["year"]
            audio.save()
            full = ID3(path)
            full.delall("TCON")
            full.add(TCON(encoding=1, text=[tags["genre"]]))
            full.save(path, v2_version=3)
            return True
        except Exception as e:
            typer.echo(f"  ✗ tag failed {path.name}: {e}")
            return False
    if ext == ".m4a":
        audio = MP4(path)
        audio.tags["\xa9nam"] = [tags["title"]]
        audio.tags["\xa9ART"] = [tags["artist"]]
        audio.tags["\xa9alb"] = [tags["album"]]
        audio.tags["trkn"] = [(int(tags["tracknumber"]), 0)]
        if tags.get("genre"):
            audio.tags["\xa9gen"] = [tags["genre"]]
        if tags.get("year"):
            audio.tags["\xa9day"] = [tags["year"]]
        audio.save()
        return True
    if ext in (".ogg", ".opus"):
        meta = OggVorbis(path) if ext == ".ogg" else OpusFile(path)
        meta["artist"] = [tags["artist"]]
        meta["album"] = [tags["album"]]
        meta["title"] = [tags["title"]]
        meta["tracknumber"] = [tags["tracknumber"]]
        if tags.get("genre"):
            meta["genre"] = [tags["genre"]]
        if tags.get("year"):
            meta["date"] = [tags["year"]]
        meta.save()
        return True
    typer.echo(f"  ⊘ not taggable here ({ext}): {path.name} — beets will fingerprint it")
    return False


def parse_track_file(path: Path) -> tuple[str, str] | None:
    """'NN - Title.ext' -> (track_number, title)."""
    m = re.match(r"(\d+)\s+-\s+(.*)", path.stem)
    if not m:
        return None
    return m.group(1), m.group(2)


def tag_music(spec_path: Path, output_dir: Path):
    """Pre-tag everything downloaded; genre per ARTIST (one last.fm call)."""
    api_key = ENV.get("LASTFM_API_KEY", "").strip()
    vocab = mb_genre_vocabulary(RateLimitedClient())
    client = RateLimitedClient()
    genre_cache = {}
    spec = json.loads(spec_path.read_text())
    spec_years = {(p.get("artist"), p.get("album")): p.get("year") for p in spec}

    albums = set()
    for f in output_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in (".mp3", ".m4a", ".ogg", ".opus"):
            albums.add(f.parent)
    for album_dir in sorted(albums):
        files = sorted(album_dir.iterdir())
        artist = album_dir.parent.name
        album = album_dir.name
        if artist not in genre_cache:
            genre_cache[artist] = lastfm_artist_genre(client, artist, api_key, vocab) if api_key else None
        genre = genre_cache[artist]
        if genre:
            typer.echo(f"  ♪ {artist}: genre={genre}")
        year = str(spec_years.get((artist, album)) or "")
        tagged = 0
        for f in files:
            parsed = parse_track_file(f)
            if not parsed:
                typer.echo(f"  ✗ no 'NN - Title' name: {f.name}")
                continue
            tracknumber, title = parsed
            tags = {"artist": artist, "album": album, "title": title,
                    "tracknumber": tracknumber, "year": year, "genre": genre or ""}
            if tag_file(f, tags):
                tagged += 1
        typer.echo(f" tagged {tagged}/{len(files)} in {artist}/{album}")


def push_to_store(output_dir: Path, spec_path: Path) -> bool:
    """Push the batch to the store staging dir and STOP. The beets import
    (and its interactive decisions) is yours: run on the store:
        ssh store 'beet import -t <printed staging dir>'
    then 'python main.py scan' triggers the gonic rescan."""
    batch = time.strftime("batch_%Y%m%d_%H%M%S")
    remote_dir = f"{STORE_STAGING}/{batch}"
    typer.echo(f"⇪ pushing {output_dir} -> {STORE_HOST}:{remote_dir}")
    subprocess.run(["ssh", STORE_HOST, "mkdir", "-p", remote_dir], check=True)
    subprocess.run(["rsync", "-a", f"{output_dir}/", f"{STORE_HOST}:{remote_dir}/"], check=True)
    typer.echo("✓ pushed. Automation ends here — now, in control:")
    typer.echo(f"    ssh {STORE_HOST} 'beet import -t {remote_dir}'   # interactive: you see the matches")
    typer.echo(f"    python main.py scan                              # rescan gonic afterwards")
    return True


def trigger_scan() -> bool:
    """Ask gonic to rescan so the new albums appear. Needs GONIC_USER/GONIC_PASS
    (a gonic user allowed to scan) in .env; otherwise prints the manual step."""
    user = ENV.get("GONIC_USER", "").strip()
    pw = ENV.get("GONIC_PASS", "").strip()
    host = ENV.get("GONIC_HOST", "http://192.168.68.102:4747").strip()
    if not (user and pw):
        typer.echo("⚠ no GONIC_USER/GONIC_PASS in .env — click Rescan in the gonic UI once")
        return False
    import hashlib
    salt = os.urandom(8).hex()[:8]
    token = hashlib.md5((pw + salt).encode()).hexdigest()
    qs = urllib.parse.urlencode({"u": user, "t": token, "s": salt, "c": "jukebox", "v": "1.16.1", "f": "json"})
    try:
        with urllib.request.urlopen(f"{host}/rest/startScan?{qs}", timeout=30) as resp:
            data = json.loads(resp.read().decode())
        ok = data.get("subsonic-response", {}).get("status") == "ok"
        typer.echo("⟳ gonic rescan triggered") if ok else typer.echo(
            "⚠ gonic rejected the scan (user may lack scan rights) — click Rescan in the UI once")
        return ok
    except Exception as e:
        typer.echo(f"⚠ scan trigger failed ({e}) — click Rescan in the gonic UI once")
        return False


@app.command()
def scan():
    """Trigger a gonic rescan (admin creds from .env: GONIC_USER/GONIC_PASS)."""
    ok = trigger_scan()
    sys.exit(0 if ok else 2)


@app.command("all")
def all_command(spec: str, output: str = "new"):
    """download + tag + push + beets import (alias of ingest)."""
    ingest(spec, output)


@app.command()
def ingest(spec: str, output: str = "new"):
    """download + convert + tag + push. Beets import stays manual (you)."""
    spec_path, output_dir = Path(spec), Path(output)
    download_music(spec_path, output_dir)
    convert_to_mp3(output_dir)
    tag_music(spec_path, output_dir)
    # push_to_store(output_dir, spec_path)


@app.command()
def download(spec: str, output: str = "new"):
    """download only"""
    download_music(Path(spec), Path(output))


@app.command()
def tag(spec: str, output: str = "new"):
    """pre-tag only (downloads present but not yet ingested)"""
    tag_music(Path(spec), Path(output))


if __name__ == "__main__":
    app()