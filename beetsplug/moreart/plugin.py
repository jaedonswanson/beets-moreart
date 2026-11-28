"""Fetch additional album artwork from Fanart.tv, MusicBrainz, and TheAudioDB."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from beets.plugins import BeetsPlugin
from beets.util import syspath


class _Colors:
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


class ArtworkStats:
    """Counts outcomes from a manual artwork-fetch run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.albums_checked = 0
        self.saved = 0
        self.already_present = 0
        self.no_image = 0
        self.would_fetch = 0
        self.missing_path = 0
        self.cached = 0

    def add(self, field: str, amount: int = 1) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + amount)

    def print_summary(self, use_color: bool = False) -> None:
        colors = (
            _Colors
            if use_color
            else type(
                "_NoColors",
                (),
                {key: "" for key in vars(_Colors) if not key.startswith("_")},
            )
        )
        with self._lock:
            values = {
                "albums_checked": self.albums_checked,
                "saved": self.saved,
                "already_present": self.already_present,
                "cached": self.cached,
                "no_image": self.no_image,
                "would_fetch": self.would_fetch,
                "missing_path": self.missing_path,
            }
        divider = f"{colors.BOLD}{'─' * 48}{colors.RESET}"
        lines = [
            "",
            divider,
            f"  {colors.BOLD}MoreArt summary{colors.RESET}",
            f"  {colors.BOLD}{'Albums checked:':<21}{colors.RESET} {values['albums_checked']}",
            f"  {colors.GREEN}{'Artwork saved:':<21}{colors.RESET} {values['saved']}",
            f"  {'Already present:':<21} {values['already_present']}",
            f"  {'Cached no-image:':<21} {values['cached']}",
            f"  {colors.YELLOW}{'No image saved:':<21}{colors.RESET} {values['no_image']}",
            f"  {'Would fetch (pretend):':<21} {values['would_fetch']}",
            f"  {'Albums missing path:':<21} {values['missing_path']}",
            divider,
        ]
        print("\n".join(lines))


class ArtworkProgress:
    """Prints a thread-safe progress line after each album is checked."""

    def __init__(self, total: int, use_color: bool) -> None:
        self.total = total
        self.current = 0
        self.use_color = use_color
        self._lock = threading.Lock()
        self._start_time = time.monotonic()

    def report(self, album: Any, saved: int) -> None:
        with self._lock:
            self.current += 1
            current = self.current
            filled = max(1, int(current / self.total * 12)) if self.total else 12
            percent = int(current / self.total * 100) if self.total else 100
            elapsed = int(time.monotonic() - self._start_time)
            minutes, seconds = divmod(elapsed, 60)
            bar = f"{'█' * filled}{'─' * (12 - filled)}"
            artist = album.albumartist or "Unknown artist"
            title = album.album or "Unknown album"
            color = (
                (_Colors.GREEN if saved else _Colors.YELLOW) if self.use_color else ""
            )
            reset = _Colors.RESET if self.use_color else ""
            status = f"Saved {saved}" if saved else "Checked"
            print(
                f"[{current:04d}/{self.total:04d}] [{bar}] "
                f"{percent:3d}% {minutes:02d}:{seconds:02d} "
                f"{color}{status}{reset}: {artist} - {title}",
                flush=True,
            )


class MoreArtPlugin(BeetsPlugin):
    """Fetch disc, spine, and back artwork for albums."""

    ART_TYPES = ("discart", "spine", "back")
    IMAGE_EXTENSIONS = (
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".bmp",
        ".tif",
        ".tiff",
    )
    MAX_WORKERS = 16

    def __init__(self) -> None:
        super().__init__(name="moreart")
        self.config.add(
            {
                "fanarttv_key": None,
                "theaudiodb_key": None,
                "sources": ["fanarttv", "theaudiodb", "musicbrainz"],
                "types": ["discart", "spine", "back"],
                "run_on_import": True,
                "workers": 4,
                "progress": True,
                "timeout": 15,
                "cache_results": True,
                "recheck_days": 30,
                "resize": {
                    "enabled": True,
                    "discart": ["1000", "1000"],
                    "back": ["750", "750"],
                    "spine": ["35", "700"],
                },
                "fuzz": "15%",
                "background": {"multi_corner": True},
            }
        )

        self._magick_bin = shutil.which("magick") or shutil.which("convert")
        self._request_state = threading.local()
        self._request_failures = 0
        self._use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        if self.config["run_on_import"].get(bool):
            self.import_stages = [self.import_stage]

    @property
    def _request_failures(self) -> int:
        return getattr(self._request_state, "failures", 0)

    @_request_failures.setter
    def _request_failures(self, value: int) -> None:
        self._request_state.failures = value

    def _validate_workers(self, workers: int) -> int:
        if workers < 1:
            self._log.warning(f"Worker count {workers} is below 1; using 1.")
            return 1
        if workers > self.MAX_WORKERS:
            self._log.warning(
                f"Worker count {workers} exceeds {self.MAX_WORKERS}; "
                f"using {self.MAX_WORKERS}."
            )
            return self.MAX_WORKERS
        return workers

    def import_stage(self, _session: Any, task: Any) -> None:
        album = task.album
        if album:
            self._process_album(album)

    @staticmethod
    def _album_directory(album: Any) -> str:
        album_dir = os.fsdecode(album.path)  # no syspath yet
        if not os.path.isabs(album_dir):
            library_dir = getattr(getattr(album, "_db", None), "directory", None)
            if library_dir:
                album_dir = os.path.join(os.fsdecode(library_dir), album_dir)
        return syspath(os.path.abspath(album_dir))

    def commands(self) -> list[Any]:
        from beets.ui import Subcommand

        cmd = Subcommand(
            "moreart",
            help="fetch extra artworks (discart, spine, back)",
        )
        cmd.parser.add_option(
            "--pretend",
            action="store_true",
            default=False,
            help="show what would be done, but don't download or save",
        )
        cmd.parser.add_option(
            "--force",
            action="store_true",
            default=False,
            help="ignore cached no-artwork results and search again",
        )
        cmd.parser.add_option(
            "-w",
            "--workers",
            type="int",
            dest="workers",
            help="number of albums to process concurrently",
        )
        cmd.parser.add_option(
            "-q",
            "--quiet",
            action="store_true",
            default=False,
            help="suppress the progress display",
        )

        def func(lib: Any, opts: Any, args: Sequence[str]) -> None:
            albums = list(lib.albums(args))
            if not albums:
                return

            workers = getattr(opts, "workers", None)
            if workers is None:
                workers = self.config["workers"].get(int)
            workers = self._validate_workers(workers)
            stats = ArtworkStats()
            show_progress = self.config["progress"].get(bool) and not getattr(
                opts, "quiet", False
            )
            progress = (
                ArtworkProgress(len(albums), bool(self._use_color))
                if show_progress
                else None
            )

            def process(album: Any) -> None:
                try:
                    saved = self._process_album(
                        album,
                        pretend=opts.pretend,
                        force=opts.force,
                        stats=stats,
                    )
                except FileNotFoundError as error:
                    self._log.warning(
                        f"Album path disappeared while processing {album}: {error}"
                    )
                    stats.add("missing_path")
                    saved = 0
                if progress:
                    progress.report(album, saved)

            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    list(executor.map(process, albums))
            else:
                for album in albums:
                    process(album)
            stats.print_summary(use_color=bool(self._use_color))

        cmd.func = func
        return [cmd]

    def _process_album(
        self,
        album: Any,
        pretend: bool = False,
        force: bool = False,
        stats: ArtworkStats | None = None,
    ) -> int:
        if stats:
            stats.add("albums_checked")
        if not album.path:
            self._log.warning("Album has no path; skipping extra artwork fetch.")
            if stats:
                stats.add("missing_path")
            return 0

        album_dir = self._album_directory(album)
        if not os.path.isdir(album_dir):
            self._log.warning(
                f"Album directory does not exist; skipping {album}: {album_dir}"
            )
            if stats:
                stats.add("missing_path")
            return 0
        sources = self.config["sources"].as_str_seq()
        art_types = self.config["types"].as_str_seq()
        album_saved = 0

        for art_type in art_types:
            if art_type not in self.ART_TYPES:
                self._log.warning(f"Unsupported artwork type {art_type!r}; skipping.")
                continue
            if self._art_exists(album_dir, art_type):
                self._log.debug(f"{art_type} already exists for {album}")
                if stats:
                    stats.add("already_present")
                continue

            if pretend:
                if stats:
                    stats.add("would_fetch")
                for source in sources:
                    self._log.info(
                        f"[pretend] Would try {art_type} from {source} for {album}"
                    )
                continue

            cache_key = self._cache_key(album, art_type, sources)
            if self._should_skip_cached(album, cache_key, force):
                if stats:
                    stats.add("cached")
                self._log.debug(
                    f"Skipping cached no-artwork lookup for {album} ({art_type})"
                )
                continue

            failure_count = self._request_failures
            for source in sources:
                images = self._fetch_from_source(source, album, art_type)
                if not images:
                    continue

                saved = 0
                for index, image_bytes in enumerate(images, start=1):
                    if art_type == "discart" and len(images) > 1:
                        filename = f"discart {index}.jpg"
                    else:
                        filename = f"{art_type}.jpg"
                    path = os.path.join(album_dir, filename)
                    saved_path = self._save_image(path, image_bytes, source, art_type)
                    if not saved_path:
                        continue

                    if art_type == "discart":
                        self._fix_discart_background(saved_path)
                    if self.config["resize"]["enabled"].get(bool):
                        resize_config = self.config["resize"][art_type].as_str_seq()
                        if len(resize_config) == 2:
                            try:
                                width, height = (int(value) for value in resize_config)
                            except ValueError:
                                self._log.warning(
                                    f"Invalid resize dimensions for {art_type}: "
                                    f"{resize_config!r}"
                                )
                            else:
                                self._resize_image(saved_path, width, height)

                    saved += 1
                    self._log.info(
                        f"{os.path.basename(saved_path)} ready for {album} "
                        f"(from {source})"
                    )

                if saved:
                    self._clear_cached_negative(album, cache_key)
                    album_saved += saved
                    if stats:
                        stats.add("saved", saved)
                    break
            else:
                if self._request_failures == failure_count:
                    self._cache_negative(album, cache_key)
                if stats:
                    stats.add("no_image")
                self._log.warning(f"No valid {art_type} found for {album}")
        return album_saved

    def _cache_key(self, album: Any, art_type: str, sources: Sequence[str]) -> str:
        def secret_digest(value: Any) -> str:
            if not value:
                return ""
            return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

        payload = {
            "art_type": art_type,
            "sources": list(sources),
            "mb_albumid": getattr(album, "mb_albumid", None),
            "albumartist": getattr(album, "albumartist", None),
            "album": getattr(album, "album", None),
            "fanarttv_key": secret_digest(self.config["fanarttv_key"].get()),
            "theaudiodb_key": secret_digest(self.config["theaudiodb_key"].get()),
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _read_album_cache(album: Any) -> dict[str, str]:
        value = album.get("moreart_cache")
        if not isinstance(value, str) or not value:
            return {}
        try:
            cache = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return cache if isinstance(cache, dict) else {}

    def _should_skip_cached(self, album: Any, cache_key: str, force: bool) -> bool:
        if force or not self.config["cache_results"].get(bool):
            return False

        checked_at = self._read_album_cache(album).get(cache_key)
        if not isinstance(checked_at, str):
            return False
        try:
            checked = datetime.fromisoformat(checked_at)
        except ValueError:
            return False

        recheck_days = max(0, self.config["recheck_days"].get(int))
        if datetime.now() - checked < timedelta(days=recheck_days):
            return True
        return False

    def _write_album_cache(self, album: Any, cache: dict[str, str]) -> None:
        album["moreart_cache"] = json.dumps(cache, sort_keys=True)
        album.store(inherit=False)

    def _cache_negative(self, album: Any, cache_key: str) -> None:
        if not self.config["cache_results"].get(bool):
            return

        cache = self._read_album_cache(album)
        cutoff = datetime.now() - timedelta(
            days=max(0, self.config["recheck_days"].get(int))
        )
        for key, checked_at in list(cache.items()):
            try:
                checked = datetime.fromisoformat(checked_at)
            except (TypeError, ValueError):
                del cache[key]
                continue
            if checked < cutoff:
                del cache[key]
        cache[cache_key] = datetime.now().isoformat()
        self._write_album_cache(album, cache)

    def _clear_cached_negative(self, album: Any, cache_key: str) -> None:
        if not self.config["cache_results"].get(bool):
            return
        cache = self._read_album_cache(album)
        if cache_key in cache:
            del cache[cache_key]
            self._write_album_cache(album, cache)

    def _fetch_from_source(
        self, source: str, album: Any, art_type: str
    ) -> list[bytes] | None:
        fetchers = {
            "fanarttv": self._fetch_from_fanarttv,
            "musicbrainz": self._fetch_from_musicbrainz,
            "theaudiodb": self._fetch_from_theaudiodb,
        }
        fetcher = fetchers.get(source)
        if fetcher is None:
            self._log.warning(f"Unknown artwork source {source!r}; skipping.")
            return None
        return fetcher(album, art_type)

    def _art_exists(self, album_dir: str, art_type: str) -> bool:
        try:
            filenames = os.listdir(album_dir)
        except FileNotFoundError:
            return False

        stem_pattern = re.compile(
            r"^%s(?:\d+)?$" % re.escape(art_type),
            flags=re.IGNORECASE,
        )
        return any(
            stem_pattern.fullmatch(os.path.splitext(filename)[0])
            and os.path.splitext(filename)[1].lower() in self.IMAGE_EXTENSIONS
            for filename in filenames
        )

    @staticmethod
    def _image_format(image_bytes: bytes) -> str | None:
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if image_bytes.startswith((b"GIF87a", b"GIF89a")):
            return "gif"
        if (
            len(image_bytes) >= 12
            and image_bytes[:4] == b"RIFF"
            and image_bytes[8:12] == b"WEBP"
        ):
            return "webp"
        if image_bytes.startswith(b"BM"):
            return "bmp"
        if image_bytes.startswith((b"II*\x00", b"MM\x00*")):
            return "tif"
        return None

    @staticmethod
    def _file_checksum(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as image_file:
            for chunk in iter(lambda: image_file.read(8192), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _save_image(
        self, path: str, image_bytes: bytes, source: str, art_type: str
    ) -> str | None:
        image_format = self._image_format(image_bytes)
        if image_format is None:
            self._request_failures += 1
            self._log.warning(f"{source} did not return a valid {art_type} image.")
            return None

        base, _ = os.path.splitext(path)
        if self._magick_bin:
            output_format = "png" if art_type == "discart" else "jpg"
        else:
            output_format = image_format
        output_path = f"{base}.{output_format}"
        if os.path.exists(output_path):
            if (
                self._file_checksum(output_path)
                == hashlib.sha256(image_bytes).hexdigest()
            ):
                self._log.debug(f"Skipping identical {art_type} image: {output_path}")
                return None

        download_path = f"{base}.download.{image_format}"
        converted_path = f"{base}.tmp.{output_format}"
        temporary_path = f"{base}.tmp.{image_format}"
        try:
            with open(download_path, "wb") as image_file:
                image_file.write(image_bytes)

            if self._magick_bin:
                try:
                    self._run_magick([download_path, converted_path])
                    if (
                        not os.path.isfile(converted_path)
                        or os.path.getsize(converted_path) == 0
                    ):
                        raise OSError("ImageMagick produced no output")
                    os.replace(converted_path, output_path)
                    return output_path
                except (OSError, subprocess.CalledProcessError) as error:
                    self._log.warning(
                        f"Could not convert {art_type} image with ImageMagick: "
                        f"{error}; saving original."
                    )
                    output_path = f"{base}.{image_format}"

            with open(temporary_path, "wb") as image_file:
                image_file.write(image_bytes)
            os.replace(temporary_path, output_path)
            return output_path
        finally:
            for path_to_clean in (download_path, converted_path, temporary_path):
                if os.path.exists(path_to_clean):
                    os.remove(path_to_clean)

    def _run_magick(self, args: Sequence[str]) -> None:
        if not self._magick_bin:
            raise FileNotFoundError("ImageMagick (magick or convert) is not installed")
        subprocess.run(
            [self._magick_bin, *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _edit_image(self, path: str, options: Sequence[str]) -> None:
        if not self._magick_bin:
            self._log.warning(
                f"ImageMagick is not installed; leaving artwork unchanged: {path}"
            )
            return

        base, extension = os.path.splitext(path)
        temporary_path = f"{base}.processing{extension}"
        try:
            self._run_magick([path, *options, temporary_path])
            if (
                not os.path.isfile(temporary_path)
                or os.path.getsize(temporary_path) == 0
            ):
                raise OSError("ImageMagick produced no output")
            os.replace(temporary_path, path)
        except (OSError, subprocess.CalledProcessError) as error:
            self._log.warning(f"Image processing failed for {path}: {error}")
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)

    def _fix_discart_background(self, path: str) -> None:
        fuzz = self.config["fuzz"].get(str) or "15%"
        options = ["-alpha", "set", "-fuzz", fuzz, "-fill", "none"]
        if self.config["background"]["multi_corner"].get(bool):
            options.extend(
                [
                    "-draw",
                    "alpha 0,0 floodfill",
                    "-draw",
                    "alpha 0,%[fx:h-1] floodfill",
                    "-draw",
                    "alpha %[fx:w-1],0 floodfill",
                    "-draw",
                    "alpha %[fx:w-1],%[fx:h-1] floodfill",
                    "-draw",
                    "alpha %[fx:w/2],%[fx:h/2] floodfill",
                ]
            )
        else:
            options.extend(["-draw", "alpha 0,0 floodfill"])
        self._edit_image(path, options)

    def _resize_image(self, path: str, width: int, height: int) -> None:
        self._edit_image(path, ["-resize", "%dx%d!" % (width, height)])

    def _request_bytes(self, url: str, params: dict[str, str] | None = None) -> bytes:
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "beets-moreart"})
        try:
            with urlopen(request, timeout=self.config["timeout"].get(int)) as response:
                return response.read()
        except HTTPError as error:
            if error.code != 404:
                self._request_failures += 1
            raise
        except OSError:
            self._request_failures += 1
            raise

    def _request_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        try:
            return json.loads(self._request_bytes(url, params))
        except ValueError:
            self._request_failures += 1
            raise

    def _response_mapping(self, data: Any, source: str) -> dict[str, Any] | None:
        if isinstance(data, dict):
            return data
        self._request_failures += 1
        self._log.warning(f"{source} returned an unexpected response format.")
        return None

    def _invalid_response(self, source: str, detail: str) -> None:
        self._request_failures += 1
        self._log.warning(f"{source} returned an invalid {detail} response.")

    def _response_url(self, value: Any, source: str) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            self._invalid_response(source, "image URL")
            return None
        try:
            parsed_url = urlsplit(value)
        except ValueError:
            self._invalid_response(source, "image URL")
            return None
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            self._invalid_response(source, "image URL")
            return None
        return value

    def _download_image(self, url: str) -> bytes | None:
        return self._request_bytes(url)

    def _fetch_from_fanarttv(self, album: Any, art_type: str) -> list[bytes] | None:
        api_key = self.config["fanarttv_key"].get()
        mbid = album.mb_albumid
        if not api_key or not mbid:
            return None

        fields = {
            "discart": "cdart",
            "spine": "albumspine",
            "back": "albumcoverback",
        }
        try:
            data = self._request_json(
                "https://webservice.fanart.tv/v3/music/%s" % mbid,
                params={"api_key": api_key},
            )
            data = self._response_mapping(data, "Fanart.tv")
            if data is None:
                return None
            assets = data.get(fields[art_type], [])
            if not isinstance(assets, list):
                self._invalid_response("Fanart.tv", "artwork list")
                return None
            if art_type != "discart":
                assets = assets[:1]
            images = []
            for asset in assets:
                if not isinstance(asset, dict):
                    self._invalid_response("Fanart.tv", "artwork entry")
                    continue
                image_url = self._response_url(asset.get("url"), "Fanart.tv")
                if image_url:
                    try:
                        image = self._download_image(image_url)
                    except (OSError, URLError) as error:
                        self._log.warning(f"Fanart.tv image request failed: {error}")
                        continue
                    if image:
                        images.append(image)
            return images or None
        except (OSError, ValueError) as error:
            self._log.warning(f"Fanart.tv request failed for {mbid}: {error}")
            return None

    def _fetch_from_musicbrainz(self, album: Any, art_type: str) -> list[bytes] | None:
        mbid = album.mb_albumid
        if not mbid or art_type == "spine":
            return None

        try:
            data = self._request_json("https://coverartarchive.org/release/%s" % mbid)
            data = self._response_mapping(data, "MusicBrainz")
            if data is None:
                return None
            entries = data.get("images", [])
            if not isinstance(entries, list):
                self._invalid_response("MusicBrainz", "image list")
                return None
            images = []
            for entry in entries:
                if not isinstance(entry, dict):
                    self._invalid_response("MusicBrainz", "image entry")
                    continue
                image_types = entry.get("types", [])
                if not isinstance(image_types, list):
                    self._invalid_response("MusicBrainz", "image types")
                    continue
                if (
                    art_type == "back"
                    and "Back" in image_types
                    or art_type == "discart"
                    and "Medium" in image_types
                ):
                    image_url = self._response_url(entry.get("image"), "MusicBrainz")
                    if image_url:
                        try:
                            image = self._download_image(image_url)
                        except (OSError, URLError) as error:
                            self._log.warning(
                                f"MusicBrainz image request failed: {error}"
                            )
                            continue
                        if image:
                            images.append(image)
            if art_type != "discart":
                images = images[:1]
            return images or None
        except (OSError, ValueError) as error:
            self._log.warning(f"MusicBrainz request failed for {mbid}: {error}")
            return None

    def _fetch_from_theaudiodb(self, album: Any, art_type: str) -> list[bytes] | None:
        api_key = self.config["theaudiodb_key"].get()
        artist = album.albumartist
        title = album.album
        if not api_key or not artist or not title:
            return None

        fields = {
            "discart": "strAlbumCDart",
            "spine": "strAlbumSpine",
            "back": "strAlbumThumbBack",
        }
        try:
            data = self._request_json(
                "https://www.theaudiodb.com/api/v1/json/%s/searchalbum.php" % api_key,
                params={"s": artist, "a": title},
            )
            data = self._response_mapping(data, "TheAudioDB")
            if data is None:
                return None
            albums = data.get("album")
            if albums is None:
                return None
            if not isinstance(albums, list):
                self._invalid_response("TheAudioDB", "album list")
                return None
            if not albums:
                return None
            if not isinstance(albums[0], dict):
                self._invalid_response("TheAudioDB", "album entry")
                return None
            image_url = self._response_url(
                albums[0].get(fields[art_type]), "TheAudioDB"
            )
            if not image_url:
                return None
            image = self._download_image(image_url)
            return [image] if image else None
        except (OSError, ValueError) as error:
            self._log.warning(
                f"TheAudioDB request failed for {artist} - {title}: {error}"
            )
            return None
