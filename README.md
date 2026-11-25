# beets-moreart

A [beets](https://beets.io) plugin that fetches extra album artwork from
Fanart.tv, MusicBrainz Cover Art Archive, and TheAudioDB.

## Quick Start

Install from the MoreArt project directory for local testing:

```bash
pipx inject beets .
```

Enable the plugin in your Beets configuration:

```yaml
plugins:
  - moreart
```

Fetch artwork for your library:

```bash
beet moreart
```

Artwork is saved in each album directory:

```text
Artist/
└── Album/
    ├── back.jpg
    ├── discart.png
    └── spine.jpg
```

## What does this do?

Beets' built-in `fetchart` plugin fetches front-cover artwork. **beets-moreart**
looks for additional artwork: disc art, spine images, and back covers. It searches Fanart.tv, MusicBrainz Cover Art Archive, and TheAudioDB, then saves the results beside the album's music files.

MusicBrainz lookups use the album's release ID. Fanart.tv and TheAudioDB credentials are optional; sources without a configured key are skipped.

## Installation

### With pip

After the package has been published to PyPI:

```bash
pip install beets-moreart
```

### With pipx

After the package has been published to PyPI:

```bash
pipx inject beets beets-moreart
```

### From this checkout

Run these commands from the MoreArt project directory. Use the `pipx` command
when Beets is installed in a pipx-managed environment; otherwise use pip with
the same Python environment as Beets.

```bash
pipx inject beets .
# Or, when Beets and the plugin share a Python environment:
python -m pip install .
```

## Configuration

Add the plugin to your Beets configuration file (`config.yaml`):

```yaml
plugins:
  - moreart

moreart:
  fanarttv_key: YOUR_API_KEY
  theaudiodb_key: YOUR_API_KEY
  sources: [fanarttv, theaudiodb, musicbrainz]
  types: [discart, spine, back]
  run_on_import: yes
  workers: 4
  progress: yes
  timeout: 15
  cache_results: yes
  recheck_days: 30
  resize:
    enabled: yes
    discart: ["1000", "1000"]
    back: ["750", "750"]
    spine: ["35", "700"]
  fuzz: 15%
  background:
    multi_corner: yes
```

Adjust options as desired:

| Parameter | Default | Description |
| :--- | :--- | :--- |
| `fanarttv_key` | `null` | Optional Fanart.tv API key |
| `theaudiodb_key` | `null` | Optional TheAudioDB API key |
| `sources` | `[fanarttv, theaudiodb, musicbrainz]` | Sources to try, in order |
| `types` | `[discart, spine, back]` | Artwork types to fetch |
| `run_on_import` | `yes` | Fetch artwork after an album is imported |
| `workers` | `4` | Albums to process concurrently (maximum `16`) |
| `progress` | `yes` | Show a per-album progress bar during manual runs |
| `timeout` | `15` | Seconds to wait for each HTTP request |
| `cache_results` | `yes` | Remember artwork types that had no usable result |
| `recheck_days` | `30` | Days to wait before retrying cached no-artwork results |
| `resize.enabled` | `yes` | Resize fetched images when ImageMagick is available |
| `resize.discart` | `["1000", "1000"]` | Disc art dimensions |
| `resize.back` | `["750", "750"]` | Back-cover dimensions |
| `resize.spine` | `["35", "700"]` | Spine dimensions |
| `fuzz` | `15%` | ImageMagick color tolerance for disc-art background removal |
| `background.multi_corner` | `yes` | Remove the background from multiple disc-art edges |

## Usage

### Automatic (during import)

When `run_on_import` is enabled, MoreArt checks for artwork after an album is
imported. Existing artwork is left alone.

### Manual (on demand)

| Command | Description |
| :--- | :--- |
| `beet moreart` | Fetch configured artwork for all albums |
| `beet moreart beatles` | Fetch artwork for matching albums |
| `beet moreart --pretend` | Show planned source/type attempts without downloading or saving |
| `beet moreart --force` | Ignore cached no-artwork results and search again |
| `beet moreart -w 8` | Process up to 8 albums concurrently |
| `beet moreart --quiet` | Hide the progress display |

Only negative results are cached. Existing artwork is checked on disk each
time, and changing the configured sources, API keys, or album metadata triggers
a fresh lookup. `--force` bypasses cached misses; it does not overwrite artwork
that already exists.

After a manual run, MoreArt prints a summary:

```text
[0001/0014] [█───────────]   7% 00:01 Checked: Artist - Album
[0002/0014] [██──────────]  14% 00:02 Saved 1: Artist - Another Album
────────────────────────────────────────────────
  MoreArt summary
  Albums checked:      14
  Artwork saved:       18
  Already present:     14
  Cached no-image:      2
  No image saved:       4
  Would fetch (pretend): 0
  Albums missing path:  0
────────────────────────────────────────────────
```

## How it Works

1. Resolves the album directory, skips missing directories, and leaves existing artwork alone.
2. Uses configured sources in order, falling back when no usable image is returned.
3. Saves valid images beside the album files.
4. Caches missing-artwork searches on the album record and retries after the configured interval.
5. Converts back and spine artwork to JPEG when ImageMagick is installed.
6. Keeps disc art as PNG when ImageMagick is installed so transparency is preserved.

## Requirements

- Beets 2.0.0 or newer
- Python 3.9 or later
- Internet access

The plugin uses Python's standard library for HTTP requests and adds no
third-party Python runtime dependency beyond Beets. ImageMagick (`magick` or
`convert`) is optional; it enables conversion, resizing, and disc-art background
removal. Without it, images are saved in their original format.
