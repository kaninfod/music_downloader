# jukeplayer music downloader

Ingest pipeline into the jukebox collection, one command:

```sh
python main.py ingest albums_to_download.json
```

1. **download** — `yt-dlp` pulls each playlist's audio **as-is** (bestaudio, m4a
   preferred). No transcode: gonic serves m4a/opus natively and transcodes
   per-client on demand.
2. **tag** — always-on pre-tag: artist/album/title/tracknumber (+ optional
   spec `year`) + **genre**: one canonical genre per file via last.fm artist
   lookup (`.env` → `LASTFM_API_KEY`), fold-matched against the MusicBrainz
   genre vocabulary (`.cache/mb_vocab.json`, built once). gonic treats genre
   tags as opaque strings, so a single clean value is the format that works.
   Files that arrive in a non-taggable container (rare webm/wav) stay untagged
   and are handled by beets fingerprinting at import.
3. **push+import** — `rsync` the batch to
   `store:/mnt/store/music/import_staging/<batch>/` and run
   `ssh store 'beet import -q'` — beets matches MusicBrainz, writes the
   authoritative tags incl. all MusicBrainz IDs, embeds cover art
   (`fetchart`/`embedart` on the server config) and **moves the files directly
   into the collection** (`paths: $artist/$album`, relative → the beets
   `directory`). gonic's inotify watch then rescans by itself.

Beets deliberately has no genre plugin: the pre-tag genre survives the import.

## Staging safety

- Unmatched files stay in the staging dir and are reported — inspect
  (`ssh store 'beet import -t <dir>'`) before removing the batch directory.

## Commands

- `ingest` / `all` — the full pipeline
- `download` — download only
- `tag` — pre-tag only (use before importing older downloads manually)

## Overrides (in `.env`)

```
LASTFM_API_KEY=...        # the shared last.fm key (also used by gonic)
STORE_SSH=store           # ssh target for push/import
STORE_STAGING=/mnt/store/music/import_staging
```

## Requirements

- yt-dlp + ffmpeg (in PATH; ffmpeg only as yt-dlp's remuxer dependency)
- ssh access to `store` (key-based)
- python venv with: typer, mutagen, yt-dlp