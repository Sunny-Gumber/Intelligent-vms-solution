from urllib.parse import quote


def build_rtsp_uri(host: str, port: int, path: str, username: str | None, password: str | None) -> str:
    """Build an RTSP URI with percent-encoded optional credentials.

    Args:
        host: Camera hostname or IP address.
        port: RTSP port.
        path: Camera stream path, including any query string.
        username: Optional camera username.
        password: Optional camera password.

    Returns:
        RTSP URI suitable for internal media-node use.
    """
    auth = ""
    if username is not None:
        user = quote(username, safe="")
        pwd = quote(password or "", safe="")
        auth = f"{user}:{pwd}@"
    return f"rtsp://{auth}{host}:{port}{path}"
