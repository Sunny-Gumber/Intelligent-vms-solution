// Live monitoring view model.
//
// STATUS: this project has never been compiled — no .NET SDK was available in
// the build environment. Expect to resolve build issues on first compile.
//
// The behaviour this file exists to guarantee: ONE TILE FAILING MUST NOT
// AFFECT ANY OTHER TILE. Each tile owns its own session, its own error state
// and its own retry schedule. There is no shared "reload everything" path,
// because that is how a single dead camera freezes an operator's whole wall.

using System;
using System.Collections.ObjectModel;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using IntelligentVms.Client.Models;
using IntelligentVms.Client.Services;

namespace IntelligentVms.Client.ViewModels;

public sealed class LiveViewModel : ViewModelBase, IDisposable
{
    private readonly ApiClient _api;
    private readonly CancellationTokenSource _cts = new();
    private int _rows = 2;
    private int _columns = 2;

    public ObservableCollection<TileViewModel> Tiles { get; } = new();
    public ObservableCollection<CameraSummary> Cameras { get; } = new();

    public int Rows
    {
        get => _rows;
        set { if (SetField(ref _rows, value)) ResizeGrid(); }
    }

    public int Columns
    {
        get => _columns;
        set { if (SetField(ref _columns, value)) ResizeGrid(); }
    }

    public LiveViewModel(ApiClient api)
    {
        _api = api;
        ResizeGrid();
        _ = StartKeepAliveLoopAsync(_cts.Token);
    }

    public async Task LoadCamerasAsync()
    {
        var page = await _api.ListCamerasAsync();
        Cameras.Clear();
        foreach (var camera in page.Items.OrderBy(c => c.Name))
            Cameras.Add(camera);
    }

    /// Assign a camera to a tile. Only this tile is touched.
    public async Task AssignAsync(int tileIndex, string cameraId)
    {
        if (tileIndex < 0 || tileIndex >= Tiles.Count) return;
        var tile = Tiles[tileIndex];
        await tile.StopAsync();
        await tile.StartAsync(cameraId);
    }

    private void ResizeGrid()
    {
        var required = Math.Max(1, Rows * Columns);

        while (Tiles.Count > required)
        {
            var tile = Tiles[^1];
            Tiles.RemoveAt(Tiles.Count - 1);
            _ = tile.StopAsync();          // fire and forget; never block the UI
        }

        while (Tiles.Count < required)
            Tiles.Add(new TileViewModel(_api, Tiles.Count));
    }

    /// Live sessions expire server-side. Each tile refreshes its own session;
    /// one failing refresh must not cancel the others, so failures are
    /// swallowed per tile and surfaced on that tile only.
    private async Task StartKeepAliveLoopAsync(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            try
            {
                await Task.Delay(TimeSpan.FromSeconds(30), token);
            }
            catch (TaskCanceledException)
            {
                return;
            }

            foreach (var tile in Tiles.ToList())
            {
                try
                {
                    await tile.KeepAliveAsync();
                }
                catch (Exception ex)
                {
                    tile.SetError($"Session refresh failed: {ex.Message}");
                }
            }
        }
    }

    public void Dispose()
    {
        _cts.Cancel();
        foreach (var tile in Tiles) _ = tile.StopAsync();
        _cts.Dispose();
    }
}
