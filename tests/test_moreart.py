import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from beets.library import Item, Library

from beetsplug.moreart import MoreArtPlugin


@pytest.mark.parametrize(
    ("source", "response"),
    [
        ("fanarttv", []),
        ("musicbrainz", None),
        ("theaudiodb", "unexpected"),
        ("fanarttv", {"cdart": {"url": "https://example.test/disc.png"}}),
        ("fanarttv", {"cdart": [None]}),
        ("fanarttv", {"cdart": [{"url": 12}]}),
        ("musicbrainz", {"images": [None]}),
        ("musicbrainz", {"images": {}}),
        ("musicbrainz", {"images": [{"types": "Medium"}]}),
        (
            "musicbrainz",
            {"images": [{"types": ["Medium"], "image": "file:///tmp/not-an-image"}]},
        ),
        ("theaudiodb", {"album": [None]}),
        ("theaudiodb", {"album": {}}),
        (
            "theaudiodb",
            {"album": [{"strAlbumCDart": "file:///tmp/not-an-image"}]},
        ),
    ],
)
def test_provider_fetchers_reject_unexpected_response_shapes(source, response):
    plugin = MoreArtPlugin()
    plugin.config["fanarttv_key"].set("test-key")
    plugin.config["theaudiodb_key"].set("test-key")
    plugin._request_json = Mock(return_value=response)
    album = Mock(
        mb_albumid="test-mbid",
        albumartist="Artist",
        album="Album",
    )

    result = getattr(plugin, "_fetch_from_%s" % source)(album, "discart")

    assert result is None
    assert plugin._request_failures == 1


def test_image_format_recognizes_common_formats():
    assert MoreArtPlugin._image_format(b"\x89PNG\r\n\x1a\nrest") == "png"
    assert MoreArtPlugin._image_format(b"\xff\xd8\xffrest") == "jpg"
    assert MoreArtPlugin._image_format(b"GIF89arest") == "gif"
    assert MoreArtPlugin._image_format(b"RIFFxxxxWEBPrest") == "webp"
    assert MoreArtPlugin._image_format(b"not an image") is None


def test_plugin_uses_moreart_configuration_section():
    assert MoreArtPlugin().name == "moreart"


def test_save_image_preserves_original_format_without_imagemagick(tmp_path):
    plugin = MoreArtPlugin()
    plugin._magick_bin = None

    saved_path = plugin._save_image(
        str(tmp_path / "back.png"),
        b"\xff\xd8\xfftest image",
        "test",
        "back",
    )

    assert saved_path == str(tmp_path / "back.jpg")
    assert (tmp_path / "back.jpg").read_bytes() == b"\xff\xd8\xfftest image"
    assert not (tmp_path / "back.png").exists()


def test_save_image_converts_back_art_to_jpeg_when_imagemagick_is_available(tmp_path):
    plugin = MoreArtPlugin()
    plugin._magick_bin = "magick"

    def convert_to_jpeg(args):
        (tmp_path / "back.tmp.jpg").write_bytes(b"\xff\xd8\xffconverted")

    plugin._run_magick = convert_to_jpeg

    saved_path = plugin._save_image(
        str(tmp_path / "back.png"),
        b"\x89PNG\r\n\x1a\nsource image",
        "test",
        "back",
    )

    assert saved_path == str(tmp_path / "back.jpg")
    assert (tmp_path / "back.jpg").read_bytes() == b"\xff\xd8\xffconverted"
    assert not (tmp_path / "back.png").exists()


def test_save_image_keeps_discart_png_for_transparency(tmp_path):
    plugin = MoreArtPlugin()
    plugin._magick_bin = "magick"

    def convert_to_png(args):
        (tmp_path / "discart.tmp.png").write_bytes(b"\x89PNG\r\n\x1a\nconverted")

    plugin._run_magick = convert_to_png

    saved_path = plugin._save_image(
        str(tmp_path / "discart.jpg"),
        b"\xff\xd8\xffsource image",
        "test",
        "discart",
    )

    assert saved_path == str(tmp_path / "discart.png")


def test_art_exists_detects_numbered_disc_art(tmp_path):
    plugin = MoreArtPlugin()
    (tmp_path / "discart2.jpg").write_bytes(b"image")

    assert plugin._art_exists(str(tmp_path), "discart")
    assert not plugin._art_exists(str(tmp_path), "back")


def test_theaudiodb_search_uses_encoded_request_parameters():
    plugin = MoreArtPlugin()
    plugin.config["theaudiodb_key"].set("test-key")
    plugin.config["timeout"].set(9)
    album = Mock(albumartist="AC/DC & Friends", album="Back / Black")
    plugin._request_json = Mock(return_value={"album": []})

    result = plugin._fetch_from_theaudiodb(album, "back")

    assert result is None
    plugin._request_json.assert_called_once_with(
        "https://www.theaudiodb.com/api/v1/json/test-key/searchalbum.php",
        params={"s": "AC/DC & Friends", "a": "Back / Black"},
    )


def test_missing_art_is_cached_on_album_and_force_retries(tmp_path):
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    library = Library(
        path=str(tmp_path / "library.db"),
        directory=str(tmp_path),
    )
    item = Item(
        title="Track",
        artist="Artist",
        album="Album",
        albumartist="Artist",
        path=os.fsencode(str(album_dir / "track.flac")),
    )
    album = library.add_album([item])

    plugin = MoreArtPlugin()
    plugin.config["sources"].set(["musicbrainz"])
    plugin.config["types"].set(["back"])
    plugin._fetch_from_source = Mock(return_value=None)

    def network_error(*_args):
        plugin._request_failures += 1
        return None

    plugin._fetch_from_source.side_effect = network_error
    plugin._process_album(album)
    assert album.get("moreart_cache") is None

    plugin._fetch_from_source.side_effect = None
    plugin._process_album(album)
    stored_album = library.get_album(album.id)
    assert stored_album.get("moreart_cache")

    plugin._process_album(stored_album)
    assert plugin._fetch_from_source.call_count == 2

    plugin._process_album(stored_album, force=True)
    assert plugin._fetch_from_source.call_count == 3

    plugin.config["sources"].set(["theaudiodb"])
    plugin._process_album(stored_album)
    assert plugin._fetch_from_source.call_count == 4

    plugin.config["recheck_days"].set(0)
    plugin._process_album(stored_album)
    assert plugin._fetch_from_source.call_count == 5


def test_request_json_uses_standard_library_and_encodes_parameters():
    plugin = MoreArtPlugin()
    plugin.config["timeout"].set(9)
    with patch("beetsplug.moreart.plugin.urlopen") as open_url:
        open_url.return_value.__enter__.return_value.read.return_value = b'{"ok": true}'

        data = plugin._request_json(
            "https://example.test/search",
            params={"artist": "AC/DC & Friends"},
        )

    request = open_url.call_args.args[0]
    assert data == {"ok": True}
    assert request.full_url == (
        "https://example.test/search?artist=AC%2FDC+%26+Friends"
    )
    assert open_url.call_args.kwargs["timeout"] == 9


def test_invalid_image_response_is_not_written(tmp_path):
    plugin = MoreArtPlugin()
    plugin._magick_bin = None

    assert (
        plugin._save_image(
            str(tmp_path / "back.png"),
            b"<html>not an image</html>",
            "test",
            "back",
        )
        is None
    )
    assert list(tmp_path.iterdir()) == []


def test_process_album_fetches_from_theaudiodb_without_release_id(tmp_path):
    plugin = MoreArtPlugin()
    plugin._magick_bin = None
    plugin.config["sources"].set(["theaudiodb"])
    plugin.config["types"].set(["back"])
    plugin.config["resize"]["enabled"].set(False)
    plugin.config["theaudiodb_key"].set("test-key")
    plugin._request_json = Mock(
        return_value={"album": [{"strAlbumThumbBack": "https://example.test/back.jpg"}]}
    )
    plugin._download_image = Mock(return_value=b"\xff\xd8\xfftest image")
    album = Mock(
        path=str(tmp_path),
        mb_albumid=None,
        albumartist="AC/DC",
        album="Back / Black",
    )

    plugin._process_album(album)

    assert (tmp_path / "back.jpg").read_bytes() == b"\xff\xd8\xfftest image"
    plugin._request_json.assert_called_once_with(
        "https://www.theaudiodb.com/api/v1/json/test-key/searchalbum.php",
        params={"s": "AC/DC", "a": "Back / Black"},
    )


def test_manual_command_prints_summary(capsys, tmp_path):
    plugin = MoreArtPlugin()
    plugin._magick_bin = None
    plugin.config["types"].set(["back"])
    plugin.config["resize"]["enabled"].set(False)
    plugin._fetch_from_source = Mock(return_value=[b"\xff\xd8\xfftest image"])
    album = Mock(
        path=str(tmp_path),
        mb_albumid="test-mbid",
        albumartist="Artist",
        album="Album",
    )
    library = Mock()
    library.albums.return_value = [album]

    command = plugin.commands()[0]
    command.func(library, SimpleNamespace(pretend=False, force=False), [])

    output = capsys.readouterr().out
    assert "Albums checked:       1" in output
    assert "Artwork saved:        1" in output
    assert (tmp_path / "back.jpg").exists()
    library.albums.assert_called_once_with([])
