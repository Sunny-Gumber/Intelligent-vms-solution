// Video rendering via LibVLCSharp.
//
// STATUS: Windows-targeted .NET build passes; runtime playback is NOT verified.
// Hardware-accelerated decoding (D3D11/DXVA) is NOT implemented; see
// ADR-0009 and docs/LIMITATIONS.md. VLC's own decoder selection applies.

using System;
using System.ComponentModel;
using System.Windows.Controls;
using System.Windows;
using IntelligentVms.Client.ViewModels;
using LibVLCSharp.Shared;

namespace IntelligentVms.Client.Controls;

public partial class VideoTile : UserControl, IDisposable
{
    private LibVLC? _library;
    private MediaPlayer? _player;
    private TileViewModel? _model;

    public VideoTile()
    {
        InitializeComponent();
        DataContextChanged += OnDataContextChanged;
        Unloaded += (_, _) => Dispose();
    }

    private static LibVLC CreateLibrary(string sessionId, string playToken)
    {
        Core.Initialize();
        // Low latency matters more than smoothness on a monitoring wall.
        var library = new LibVLC("--network-caching=300", "--live-caching=300",
                                 "--rtsp-tcp", "--no-audio");
        try
        {
            // LibVLCSharp 3.8.2 exposes login through the native dialog callback.
            // Keep this instance tile-scoped so concurrent tiles cannot exchange
            // their session credentials. Never persist the short-lived token.
            library.SetDialogHandlers(
                (_, _) => Task.CompletedTask,
                (dialog, _, _, _, _, cancellationToken) =>
                {
                    if (cancellationToken.IsCancellationRequested)
                    {
                        dialog.Dismiss();
                        return Task.CompletedTask;
                    }

                    dialog.PostLogin(sessionId, playToken, store: false);
                    return Task.CompletedTask;
                },
                (dialog, _, _, _, _, _, _, _) =>
                {
                    dialog.Dismiss();
                    return Task.CompletedTask;
                },
                (dialog, _, _, _, _, _, _) =>
                {
                    dialog.Dismiss();
                    return Task.CompletedTask;
                },
                (_, _, _) => Task.CompletedTask);
            return library;
        }
        catch
        {
            library.Dispose();
            throw;
        }
    }

    private void OnDataContextChanged(object sender, DependencyPropertyChangedEventArgs e)
    {
        if (_model is not null) _model.PropertyChanged -= OnModelChanged;
        _model = DataContext as TileViewModel;
        if (_model is not null) _model.PropertyChanged += OnModelChanged;
        UpdateStream();
    }

    private void OnModelChanged(object? sender, PropertyChangedEventArgs e)
    {
        if (e.PropertyName == nameof(TileViewModel.StreamUrl))
            Dispatcher.Invoke(UpdateStream);
    }

    private void UpdateStream()
    {
        StopPlayer();
        var url = _model?.StreamUrl;
        if (string.IsNullOrWhiteSpace(url)) return;

        var sessionId = _model?.SessionId;
        var playToken = _model?.PlayToken;
        if (string.IsNullOrWhiteSpace(sessionId) || string.IsNullOrWhiteSpace(playToken))
        {
            _model?.SetError("Playback authorization is unavailable.");
            return;
        }

        if (!Uri.TryCreate(url, UriKind.Absolute, out var mediaUri) ||
            (mediaUri.Scheme != Uri.UriSchemeHttp &&
             mediaUri.Scheme != Uri.UriSchemeHttps) ||
            !string.IsNullOrEmpty(mediaUri.UserInfo) ||
            !string.IsNullOrEmpty(mediaUri.Query) ||
            !string.IsNullOrEmpty(mediaUri.Fragment))
        {
            _model?.SetError("Playback URL is invalid.");
            return;
        }

        if (!IsPlaybackTransportAllowed(mediaUri))
        {
            _model?.SetError("Playback requires HTTPS except for loopback development URLs.");
            return;
        }

        try
        {
            _library = CreateLibrary(sessionId, playToken);
            _player = new MediaPlayer(_library);
            Player.MediaPlayer = _player;

            // The session id and token are supplied only in the HTTP Basic
            // challenge response; the media URL contains no credentials.
            using var media = new Media(_library, mediaUri);
            _player.Play(media);
        }
        catch (Exception ex)
        {
            StopPlayer();
            _model?.SetError($"Playback failed: {ex.Message}");
        }
    }

    private static bool IsPlaybackTransportAllowed(Uri mediaUri) =>
        mediaUri.Scheme == Uri.UriSchemeHttps ||
        (mediaUri.Scheme == Uri.UriSchemeHttp && mediaUri.IsLoopback);

    private void StopPlayer()
    {
        if (_player is not null)
        {
            try { _player.Stop(); } catch { /* teardown must not throw */ }
            _player.Dispose();
        }
        _player = null;
        Player.MediaPlayer = null;

        if (_library is not null)
        {
            _library.UnsetDialogHandlers();
            _library.Dispose();
            _library = null;
        }
    }

    public void Dispose()
    {
        StopPlayer();
        GC.SuppressFinalize(this);
    }
}
