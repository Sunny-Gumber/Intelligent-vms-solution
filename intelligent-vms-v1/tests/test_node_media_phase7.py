import pytest

from app.services.node_media import NodeEndpointError, validate_node_endpoint


def test_node_endpoint_accepts_http_and_https():
    assert validate_node_endpoint("http://media-a:9997", field="api_url") == "http://media-a:9997"
    assert validate_node_endpoint("https://media.example.internal/base/", field="api_url") == "https://media.example.internal/base"


def test_node_endpoint_rejects_credentials_and_query():
    with pytest.raises(NodeEndpointError):
        validate_node_endpoint("http://user:secret@media-a:9997", field="api_url")
    with pytest.raises(NodeEndpointError):
        validate_node_endpoint("http://media-a:9997?token=secret", field="api_url")


def test_node_endpoint_rejects_non_http_scheme():
    with pytest.raises(NodeEndpointError):
        validate_node_endpoint("file:///etc/passwd", field="api_url")


def test_node_schema_rejects_unknown_endpoint_and_negative_capacity():
    from pydantic import ValidationError
    from app.models.placement_schemas import NodeUpsert

    with pytest.raises(ValidationError):
        NodeUpsert(
            name="bad",
            region_id="r1",
            roles=["media"],
            endpoints={"secret_url": "http://media-a:9997"},
        )
    with pytest.raises(ValidationError):
        NodeUpsert(
            name="bad",
            region_id="r1",
            roles=["media"],
            capacity={"max_sources": -1},
        )


def test_heartbeat_schema_does_not_accept_endpoint_mutation():
    from pydantic import ValidationError
    from app.models.placement_schemas import NodeHeartbeat

    with pytest.raises(ValidationError):
        NodeHeartbeat.model_validate({"endpoints": {"api_url": "http://media-b:9997"}})
