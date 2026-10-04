using IntelligentVMS.Desktop;

internal static class EventCenterTests
{
    public static async Task RunAllAsync()
    {
        await TestOrderingDedupeAndBounds();
        await TestPaginationAndUnknownType();
        await TestProfileClearAndMetadataSafety();
    }

    private static async Task TestOrderingDedupeAndBounds()
    {
        var provider=new FakeEvents();
        var now=DateTimeOffset.Parse("2026-10-04T10:00:00Z");
        provider.Pages.Enqueue(new EventHistoryPageDto{Items=Enumerable.Range(0,520).Select(i=>new EventRecord{
            EventId=$"e-{i:D4}",CameraId="c1",SiteId="s1",TenantId="t1",OccurredAt=now.AddSeconds(-i),EventType="motion",Source="camera",Severity="info"}).ToList()});
        await using var center=new EventCenterCoordinator(provider,new TestLogger());
        await center.LoadAsync(new EventQuery(now.AddHours(-1),now.AddMinutes(1),Limit=100));
        if(center.Events.Count!=EventCenterCoordinator.MaxDisplayedEvents||center.DroppedViewCount!=20)throw new InvalidOperationException("Event view bound failed.");
        var first=center.Events[0].EventId;
        provider.Pages.Enqueue(new EventHistoryPageDto{Items=[center.Events[0]]});
        await center.RefreshRecentAsync();
        if(center.Events.Count!=500||center.Events[0].EventId!=first)throw new InvalidOperationException("Event dedupe failed.");
    }

    private static async Task TestPaginationAndUnknownType()
    {
        var provider=new FakeEvents();var now=DateTimeOffset.Parse("2026-10-04T10:00:00Z");
        provider.Pages.Enqueue(new EventHistoryPageDto{Items=[Row("a",now,"vendor.future")],NextBefore=now,NextBeforeId="a"});
        provider.Pages.Enqueue(new EventHistoryPageDto{Items=[Row("b",now.AddSeconds(-1),"camera_offline")]});
        await using var center=new EventCenterCoordinator(provider,new TestLogger());
        await center.LoadAsync(new EventQuery(now.AddHours(-1),now.AddMinutes(1)));
        if(center.Events[0].DisplayType!="Other / Unknown")throw new InvalidOperationException("Unknown type hidden.");
        await center.LoadNextAsync();
        if(center.Events.Count!=2||center.Events[1].DisplayType!="Camera Offline")throw new InvalidOperationException("Pagination failed.");
    }

    private static async Task TestProfileClearAndMetadataSafety()
    {
        var provider=new FakeEvents();var now=DateTimeOffset.UtcNow;
        var row=Row("x",now,"motion");row.Attributes["html"]=System.Text.Json.JsonDocument.Parse("\"<script>alert(1)</script>\"").RootElement.Clone();
        provider.Pages.Enqueue(new EventHistoryPageDto{Items=[row]});
        await using var center=new EventCenterCoordinator(provider,new TestLogger());
        await center.LoadAsync(new EventQuery(now.AddHours(-1),now.AddMinutes(1)));
        var text=EventMetadataFormatter.Format(center.Events[0]);
        if(!text.Contains("<script>"))throw new InvalidOperationException("Metadata text missing.");
        center.Clear();if(center.Events.Count!=0||center.Query is not null||center.FeedState!=EventFeedState.Stopped)throw new InvalidOperationException("Profile cleanup failed.");
    }

    private static EventRecord Row(string id,DateTimeOffset time,string type)=>new(){EventId=id,TenantId="t",SiteId="s",CameraId="c",OccurredAt=time,EventType=type,Source="camera",Severity="info"};
    private sealed class FakeEvents:IEventProvider
    {
        public Queue<EventHistoryPageDto> Pages{get;}=new();
        public Task<EventHistoryPageDto> GetEventHistoryAsync(EventQuery query,CancellationToken cancellationToken=default)=>Task.FromResult(Pages.Count>0?Pages.Dequeue():new EventHistoryPageDto());
    }
    private sealed class TestLogger:IClientLogger{public void Info(string s,string m){}public void Warning(string s,string m){}public void LogError(string s,string m,Exception? e=null){}}
}
