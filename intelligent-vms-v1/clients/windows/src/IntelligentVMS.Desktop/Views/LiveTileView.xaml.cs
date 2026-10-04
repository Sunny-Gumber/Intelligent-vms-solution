using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace IntelligentVMS.Desktop;

public partial class LiveTileView : UserControl, IAsyncDisposable
{
    public int TileIndex{get;private set;}
    public ILiveMediaRenderer Renderer=>Media;
    public event EventHandler<int>? Selected;
    public event EventHandler<int>? ClearRequested;
    public event EventHandler<int>? FocusRequested;

    public LiveTileView()
    {
        InitializeComponent();
        PreviewMouseLeftButtonDown+=(_,_)=>Select();
        KeyDown+=(_,e)=>{if(e.Key is Key.Enter or Key.Space){Select();e.Handled=true;}};
    }

    public void Initialize(int index)
    {
        if(index is <0 or >=16)throw new ArgumentOutOfRangeException(nameof(index));
        TileIndex=index;
        AutomationProperties.SetName(this,$"Live tile {index+1}");
    }

    public void Render(LiveTileModel? model,bool selected,bool focused)
    {
        CameraText.Text=model?.HasAssignment==true
            ? $"{model.CameraName} · {model.SiteId}"
            : $"Tile {TileIndex+1} · Empty";
        var role=model?.ActualRole;
        if(string.IsNullOrWhiteSpace(role))role=model?.RequestedRole;
        RoleText.Text=string.IsNullOrWhiteSpace(role)?"":role.ToUpperInvariant();
        StatusText.Text=model?.State switch
        {
            LiveTileState.Loading=>"Requesting live access…",
            LiveTileState.Connecting=>"Connecting…",
            LiveTileState.Live=>"Live",
            LiveTileState.Offline=>"Camera offline",
            LiveTileState.Unauthorized=>"Session expired",
            LiveTileState.Failed=>"Live view unavailable",
            LiveTileState.Stopping=>"Stopping…",
            _=>model?.HasAssignment==true?"Stopped":"Select a camera"
        };
        TileBorder.BorderBrush=selected?Brushes.DodgerBlue:FindResource("PanelBorder") as Brush??Brushes.Gray;
        TileBorder.BorderThickness=selected?new Thickness(3):new Thickness(1);
        ClearButton.IsEnabled=model?.HasAssignment==true;
        FocusButton.IsEnabled=model?.HasAssignment==true;
        FocusButton.Content=focused?"Return":"Focus";
        ToolTip=model?.ErrorCategory is {Length:>0} error && error!="none"?$"State: {error}":null;
    }

    private void Select(){Focus();Selected?.Invoke(this,TileIndex);}
    private void Clear_Click(object sender,RoutedEventArgs e){Select();ClearRequested?.Invoke(this,TileIndex);}
    private void Focus_Click(object sender,RoutedEventArgs e){Select();FocusRequested?.Invoke(this,TileIndex);}
    public ValueTask DisposeAsync()=>Media.DisposeAsync();
}
