#!/usr/bin/env python3
"""Jukebox downloader — wishlist to raw audio in the local staging folder.

    python main.py ingest albums_to_download.json

Downloads each wishlist album AS-IS (audio-only, no transcode, no
tagging) into ./new/<artist>/<album>/. That is where this tool ends.

Everything downstream is mlib's job — conversion to mp3 320 CBR, tags,
covers, folder/file layout, the move into the collection, the gonic
rescan:

    mlib catalog new --open        # staging folder → plan → browser editor
    mlib apply <plan> --validate   # converge the files (they live locally)
    mlib shelf <plan> --into /mnt/store/music/collection --rescan

Wishlist format (albums_to_download.json): hand-curated
[{"artist": …, "album": …, "id": "OLAK5uy_…"}] — id is a YT Music
playlist id.

.env — only YTDLP_BROWSER matters here: the logged-in browser whose
YouTube cookies downloads use (REQUIRED for explicit/age-gated tracks,
which the anonymous client gets no formats for).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import typer

app = typer.Typer()

YTM_URL = "https://music.youtube.com/watch?v=Fnvk1FfMpIw&list={playlist_id}"


def load_env(path: Path = Path(__file__).parent / ".env") -> dict[str, str]:
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
        # audio-only: avoids YouTube's video-CDN 403 wall entirely; mlib's
        # apply normalizes the container (mp3 320 CBR) on import.
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


@app.command()
def ingest(spec: str, output: str = "new"):
    """Download every wishlist album, as-is, into the staging folder."""
    download_music(Path(spec), Path(output))


@app.command()
def download(spec: str, output: str = "new"):
    """Alias of ingest."""
    download_music(Path(spec), Path(output))


if __name__ == "__main__":
    app()
