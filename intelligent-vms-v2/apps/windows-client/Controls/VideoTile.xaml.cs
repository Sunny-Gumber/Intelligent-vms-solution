// Video rendering via LibVLCSharp.
//
// STATUS: written against the LibVLCSharp API but NEVER COMPILED OR RUN.
// Hardware-accelerated decoding (D3D11/DXVA) is NOT implemented; see
// ADR-0009 and docs/LIMITATIONS.md. VLC's own decoder selection applies.

using System;
using System.ComponentModel;
using System.Windows.Controls;
using IntelligentVms.Client.ViewModels;
using LibVLCSharp.Shared;

namespace IntelligentVms.Client.Controls;

public partial class VideoTile : UserControl, IDisposable
{
    private static readonly LibVLC Library = CreateLibrary();
    private MediaPlayer? _player;
    private TileViewModel? _model;

    public VideoTile()
    {
        InitializeComponent();
        DataContextChanged += OnDataContextChanged;
        Unloaded += (_, _) => Dispose();
    }

    private static LibVLC CreateLibrary()
    {
        Core.Initialize();
        // Low latency matters more than smoothness on a monitoring wall.
        return new LibVLC("--network-caching=300", "--live-caching=300",
                          "--rtsp-tcp", "--no-audio");
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

        try
        {
            _player = new MediaPlayer(Library);
            Player.MediaPlayer = _player;

            // The token authorises this viewer at the gateway. The camera's
            // own RTSP URL and credentials are never known to this client.
            using var media = new Media(Library, new Uri($"{url}?token={_model!.PlayToken}"));
            _player.Play(media);
        }
        catch (Exception ex)
        {
            _model?.SetError($"Playback failed: {ex.Message}");
        }
    }

    private void StopPlayer()
    {
        if (_player is null) return;
        try { _player.Stop(); } catch { /* teardown must not throw */ }
        _player.Dispose();
        _player = null;
        Player.MediaPlayer = null;
    }

    public void Dispose()
    {
        StopPlayer();
        GC.SuppressFinalize(this);
    }
}
