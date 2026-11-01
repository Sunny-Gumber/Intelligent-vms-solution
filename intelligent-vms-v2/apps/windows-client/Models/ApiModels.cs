namespace IntelligentVms.Client.Models;

public sealed record CameraSummary(string Id, string Name, string State);
public sealed record CameraPage(CameraSummary[] Items);
public sealed record LiveSession(
    string SessionId, string CameraId, string? WhepUrl, string? HlsUrl,
    string PlayToken, DateTimeOffset ExpiresAt);
