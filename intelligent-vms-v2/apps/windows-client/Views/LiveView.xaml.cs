using System.Windows;
using System.Windows.Controls;
using IntelligentVms.Client.Models;
using IntelligentVms.Client.ViewModels;

namespace IntelligentVms.Client.Views;

public partial class LiveView : UserControl
{
    public LiveView() => InitializeComponent();

    private void SetLayout(object sender, RoutedEventArgs e)
    {
        if (DataContext is LiveViewModel model && sender is Button button &&
            int.TryParse(button.Tag?.ToString(), out var size))
        {
            model.Rows = size;
            model.Columns = size;
        }
    }

    private async void AssignCamera(object sender, RoutedEventArgs e)
    {
        if (DataContext is LiveViewModel model && CameraList.SelectedItem is CameraSummary camera &&
            TileList.SelectedItem is TileViewModel tile)
            await model.AssignAsync(tile.Index, camera.Id);
    }
}
