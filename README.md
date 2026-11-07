# jukeplayer music downloader

The acquisition slice of the jukebox pipeline: **wishlist → raw audio in the
local staging folder. Download only** — every downstream step lives in
[`mlib`](../mlib/): conversion to mp3 320 CBR, tags, covers, folder/file
layout, the move into the collection, the gonic rescan.

## Use

```sh
python main.py ingest albums_to_download.json
```

yt-dlp pulls each playlist's audio **as-is** (bestaudio, m4a preferred; no
transcode — mlib's `apply` does the one mp3-320 normalization on import)
into `new/<artist>/<album>/NN - Title.ext`.

## Hand-off to mlib

Files stay on this machine; mlib's `shelf` does the transfer into the
collection (cross-machine rsync+verify) and the gonic rescan:

```sh
mlib catalog new --open          # staging folder → plan → browser editor
mlib apply <plan> --validate     # convert/tag/cover/rename — locally, files live here
mlib shelf <plan> --into /mnt/store/music/collection --rescan   # travels into the collection
```

The loop (catalog → edit → apply → shelf) and its `--host` semantics are
mlib's documented contract (see the mlib README / notes `02 Requirements`
D11–D14, `07 Environment variables`).

## Wishlist (albums_to_download.json)

Hand-edited `[{"artist": …, "album": …, "id": "OLAK5uy_…"}]` — `id` is a
YT Music playlist id. yt-dlp resumes per file: re-running only fetches
what is missing.

## Commands

- `ingest` — download (the whole tool)
- `download` — alias of ingest

## Requirements

- yt-dlp (the repo `venv/bin/yt-dlp` is preferred — it is the one kept
  upgraded; PATH binaries rot) + ffmpeg on PATH (yt-dlp's remuxer helper)
- python venv with: typer

## `.env`

```
YTDLP_BROWSER=firefox    # optional: logged-in browser whose YouTube cookies
                         # downloads use — REQUIRED for explicit/age-gated
                         # tracks (the anonymous client gets no formats for them)
```
