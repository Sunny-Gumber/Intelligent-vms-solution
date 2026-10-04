using System.Text.Json;
using System.Text.Json.Serialization;

namespace IntelligentVMS.Desktop;

public enum EventCenterState { Idle, Loading, Ready, Refreshing, Failed, Disconnected }
public enum EventFeedState { Stopped, Connected, BackingOff, Failed }

public sealed class EventRecord
{
    [JsonPropertyName("event_id")] public string EventId { get; set; }="";
    [JsonPropertyName("tenant_id")] public string TenantId { get; set; }="";
    [JsonPropertyName("site_id")] public string SiteId { get; set; }="";
    [JsonPropertyName("camera_id")] public string CameraId { get; set; }="";
    [JsonPropertyName("timestamp")] public DateTimeOffset OccurredAt { get; set; }
    [JsonPropertyName("event_type")] public string EventType { get; set; }="";
    [JsonPropertyName("object_type")] public string? ObjectType { get; set; }
    [JsonPropertyName("source")] public string Source { get; set; }="";
    [JsonPropertyName("confidence")] public double? Confidence { get; set; }
    [JsonPropertyName("zone_id")] public string? ZoneId { get; set; }
    [JsonPropertyName("severity")] public string Severity { get; set; }="info";
    [JsonPropertyName("recording_start")] public DateTimeOffset? RecordingStart { get; set; }
    [JsonPropertyName("recording_end")] public DateTimeOffset? RecordingEnd { get; set; }
    [JsonPropertyName("attributes")] public Dictionary<string,JsonElement> Attributes { get; set; }=[];
    [JsonPropertyName("ingested_at")] public DateTimeOffset? ReceivedAt { get; set; }
    public string CameraName { get; set; }="";
    public string DisplayType=>EventTypeNormalizer.Display(EventType);
    public string DisplayTime=>OccurredAt.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss");
}

public sealed class EventHistoryPageDto
{
    [JsonPropertyName("items")] public List<EventRecord> Items { get; set; }=[];
    [JsonPropertyName("next_before")] public DateTimeOffset? NextBefore { get; set; }
    [JsonPropertyName("next_before_id")] public string? NextBeforeId { get; set; }
}

public sealed record EventQuery(
    DateTimeOffset Start, DateTimeOffset End, string? SiteId=null, string? CameraId=null,
    string? EventType=null, string? Severity=null, DateTimeOffset? Before=null, string? BeforeId=null, int Limit=100);

public interface IEventProvider
{
    Task<EventHistoryPageDto> GetEventHistoryAsync(EventQuery query,CancellationToken cancellationToken=default);
}

public static class EventTypeNormalizer
{
    public static string Display(string value)=>value.ToLowerInvariant() switch
    {
        "motion"=>"Motion",
        "tamper"=>"Video Tamper",
        "tripwire"=>"Tripwire",
        "intrusion"=>"Intrusion",
        "anpr"=>"ANPR",
        "face"=>"Face Detection",
        "human"=>"Human",
        "vehicle"=>"Vehicle",
        "object_count"=>"Object Count",
        "camera_offline"=>"Camera Offline",
        "camera_recovered"=>"Camera Online",
        _=>"Other / Unknown"
    };
}

public static class EventMetadataFormatter
{
    public static string Format(EventRecord row)
    {
        var lines=new List<string>();
        foreach(var pair in row.Attributes.OrderBy(x=>x.Key,StringComparer.Ordinal).Take(12))
        {
            string? value=pair.Value.ValueKind switch
            {
                JsonValueKind.String=>pair.Value.GetString(),
                JsonValueKind.Number=>pair.Value.GetRawText(),
                JsonValueKind.True=>"true",
                JsonValueKind.False=>"false",
                _=>null
            };
            if(value is null)continue;
            var key=Safe(pair.Key,64);value=Safe(value,160);
            lines.Add($"{key}: {value}");
        }
        return string.Join(Environment.NewLine,lines);
    }
    private static string Safe(string value,int max)=>new(value.Where(ch=>!char.IsControl(ch)).Take(max).ToArray());
}

public sealed class EventCenterCoordinator:IAsyncDisposable
{
    public const int MaxDisplayedEvents=500;
    private readonly IEventProvider _provider; private readonly IClientLogger _logger;
    private readonly Dictionary<string,EventRecord> _rows=new(StringComparer.Ordinal);
    private CancellationTokenSource? _pollCts; private int _generation; private bool _disposed;
    public EventCenterState State{get;private set;}=EventCenterState.Idle;
    public EventFeedState FeedState{get;private set;}=EventFeedState.Stopped;
    public string ErrorCategory{get;private set;}="none";
    public DateTimeOffset? LastReceivedAt{get;private set;}
    public EventQuery? Query{get;private set;}
    public DateTimeOffset? NextBefore{get;private set;}
    public string? NextBeforeId{get;private set;}
    public int DroppedViewCount{get;private set;}
    public IReadOnlyList<EventRecord> Events=>_rows.Values
        .OrderByDescending(x=>x.OccurredAt).ThenByDescending(x=>x.EventId,StringComparer.Ordinal).ToArray();
    public event EventHandler? Changed;
    public EventCenterCoordinator(IEventProvider provider,IClientLogger logger){_provider=provider;_logger=logger;}

    public async Task LoadAsync(EventQuery query,CancellationToken cancellationToken=default)
    {
        ThrowIfDisposed(); Validate(query); StopPolling(); var generation=Interlocked.Increment(ref _generation);
        State=EventCenterState.Loading;ErrorCategory="none";Notify();
        try{
            var page=await _provider.GetEventHistoryAsync(query,cancellationToken);
            if(generation!=_generation)return;
            _rows.Clear();Add(page.Items);Query=query with{Before=null,BeforeId=null};NextBefore=page.NextBefore;NextBeforeId=page.NextBeforeId;
            State=EventCenterState.Ready;ErrorCategory="none";
        }catch(SessionExpiredException){if(generation==_generation){State=EventCenterState.Disconnected;ErrorCategory="authentication";}throw;}
        catch(OperationCanceledException) when(cancellationToken.IsCancellationRequested){throw;}
        catch(Exception ex){if(generation==_generation){State=EventCenterState.Failed;ErrorCategory="history";}_logger.LogError("event-center","event history load failed",ex);}
        finally{Notify();}
    }

    public async Task LoadNextAsync(CancellationToken cancellationToken=default)
    {
        if(Query is null||NextBefore is null||string.IsNullOrWhiteSpace(NextBeforeId))return;
        State=EventCenterState.Refreshing;Notify();
        try{
            var page=await _provider.GetEventHistoryAsync(Query with{Before=NextBefore,BeforeId=NextBeforeId},cancellationToken);
            Add(page.Items);NextBefore=page.NextBefore;NextBeforeId=page.NextBeforeId;State=EventCenterState.Ready;ErrorCategory="none";
        }catch(SessionExpiredException){State=EventCenterState.Disconnected;ErrorCategory="authentication";throw;}
        catch(Exception ex){State=EventCenterState.Failed;ErrorCategory="pagination";_logger.LogError("event-center","event history page failed",ex);}
        finally{Notify();}
    }

    public async Task RefreshRecentAsync(CancellationToken cancellationToken=default)
    {
        if(Query is null)return; var end=DateTimeOffset.UtcNow;
        var start=end-TimeSpan.FromMinutes(5); if(start<Query.Start)start=Query.Start;
        try{
            var page=await _provider.GetEventHistoryAsync(Query with{Start=start,End=end,Before=null,BeforeId=null,Limit=100},cancellationToken);
            Add(page.Items);if(page.Items.Count>0)LastReceivedAt=DateTimeOffset.UtcNow;
            State=EventCenterState.Ready;ErrorCategory="none";
        }catch(SessionExpiredException){State=EventCenterState.Disconnected;ErrorCategory="authentication";throw;}
        catch(Exception ex){ErrorCategory="feed";_logger.LogError("event-center","recent event refresh failed",ex);throw;}
        finally{Notify();}
    }

    public void StartPolling()
    {
        ThrowIfDisposed(); if(_pollCts is not null)return;
        _pollCts=new CancellationTokenSource();var token=_pollCts.Token;var generation=_generation;
        _=Task.Run(async()=>{
            var delay=TimeSpan.FromSeconds(5);
            while(!token.IsCancellationRequested){
                try{await Task.Delay(delay,token);if(generation!=_generation)break;await RefreshRecentAsync(token);FeedState=EventFeedState.Connected;delay=TimeSpan.FromSeconds(5);}
                catch(OperationCanceledException) when(token.IsCancellationRequested){break;}
                catch(SessionExpiredException){FeedState=EventFeedState.Failed;break;}
                catch{FeedState=EventFeedState.BackingOff;delay=TimeSpan.FromSeconds(Math.Min(30,Math.Max(10,delay.TotalSeconds*2)));Notify();}
            }
        },token);
        FeedState=EventFeedState.Connected;Notify();
    }

    public void StopPolling(){var cts=_pollCts;_pollCts=null;if(cts is not null){cts.Cancel();cts.Dispose();}FeedState=EventFeedState.Stopped;Notify();}
    public void Clear(){StopPolling();Interlocked.Increment(ref _generation);_rows.Clear();Query=null;NextBefore=null;NextBeforeId=null;LastReceivedAt=null;DroppedViewCount=0;State=EventCenterState.Idle;ErrorCategory="none";Notify();}

    private void Add(IEnumerable<EventRecord> rows)
    {
        foreach(var row in rows){
            if(string.IsNullOrWhiteSpace(row.EventId)||string.IsNullOrWhiteSpace(row.CameraId))continue;
            _rows[row.EventId]=row;
        }
        if(_rows.Count<=MaxDisplayedEvents)return;
        var remove=_rows.Values.OrderByDescending(x=>x.OccurredAt).ThenByDescending(x=>x.EventId,StringComparer.Ordinal).Skip(MaxDisplayedEvents).Select(x=>x.EventId).ToArray();
        foreach(var id in remove)_rows.Remove(id);DroppedViewCount+=remove.Length;
    }
    private static void Validate(EventQuery q){if(q.End<=q.Start||q.End-q.Start>TimeSpan.FromDays(7)||q.Limit is <1 or >200)throw new ArgumentOutOfRangeException(nameof(q));}
    private void Notify()=>Changed?.Invoke(this,EventArgs.Empty);
    private void ThrowIfDisposed()=>ObjectDisposedException.ThrowIf(_disposed,this);
    public ValueTask DisposeAsync(){if(!_disposed){Clear();_disposed=true;}return ValueTask.CompletedTask;}
}
