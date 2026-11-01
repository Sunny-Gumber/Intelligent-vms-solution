"""Regression guard for the native Windows client's HLS auth contract."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIDEO_TILE = ROOT / "apps/windows-client/Controls/VideoTile.xaml.cs"
TILE_VIEW_MODEL = ROOT / "apps/windows-client/ViewModels/TileViewModel.cs"


def test_windows_client_uses_tile_scoped_basic_auth_without_url_credentials():
    video_tile = VIDEO_TILE.read_text(encoding="utf-8")
    tile_view_model = TILE_VIEW_MODEL.read_text(encoding="utf-8")

    assert "library.SetDialogHandlers(" in video_tile
    assert "dialog.PostLogin(sessionId, playToken, store: false);" in video_tile
    assert "private LibVLC? _library;" in video_tile
    assert "new Media(_library, mediaUri)" in video_tile
    assert "mediaUri.UserInfo" in video_tile
    assert "mediaUri.Query" in video_tile
    assert "mediaUri.Fragment" in video_tile
    assert 'new Uri($"{url}?token=' not in video_tile
    assert "public string? SessionId => _session?.SessionId;" in tile_view_model


def test_windows_client_requires_https_except_for_loopback_before_auth_handlers():
    video_tile = VIDEO_TILE.read_text(encoding="utf-8")

    assert "mediaUri.Scheme == Uri.UriSchemeHttps" in video_tile
    assert (
        "mediaUri.Scheme == Uri.UriSchemeHttp && mediaUri.IsLoopback"
        in video_tile
    )
    transport_guard = video_tile.index("if (!IsPlaybackTransportAllowed(mediaUri))")
    install_handlers = video_tile.index("_library = CreateLibrary(sessionId, playToken);")
    assert transport_guard < install_handlers
