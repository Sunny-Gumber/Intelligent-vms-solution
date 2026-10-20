using System.Windows;
using IntelligentVms.Client.ViewModels;

namespace IntelligentVms.Client.Views;

public partial class MainWindow : Window
{
    public MainWindow(LiveViewModel model)
    {
        InitializeComponent();
        DataContext = model;
        Loaded += async (_, _) =>
        {
            try { await model.LoadCamerasAsync(); }
            catch (Exception ex) { MessageBox.Show(this, ex.Message, "Camera inventory unavailable"); }
        };
    }
}
