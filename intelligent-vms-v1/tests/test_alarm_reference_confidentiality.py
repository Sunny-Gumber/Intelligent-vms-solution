"""Unknown and inaccessible references have identical public denial contracts."""

import asyncio

import pytest

from tests.test_alarm_write_scope import _api, _payload, _principal, _rule, _state
from app.models.entities import AlarmRuleEntity


@pytest.mark.parametrize("action", ["create", "proposed", "current-metadata", "current-enabled", "delete"])
@pytest.mark.parametrize("mixed", [False, True])
def test_reference_denials_are_indistinguishable_and_preserve_rows(action, mixed):
    """Compare exact responses and all persisted columns across three reference classes."""
    async def scenario():
        responses = []
        for reference in ("unknown", "cam-other", "cam-b"):
            cameras = (["cam-a"] if mixed else []) + [reference]
            current = cameras if action.startswith("current") or action == "delete" else ["cam-a"]
            async with _api(_principal("admin"), [_rule(cameras=current)]) as (client, sessions):
                async with sessions() as session:
                    before = _state(await session.get(AlarmRuleEntity, "rule"))
                if action == "create":
                    result = await client.post("/api/v1/alarms/rules", json=_payload(camera_ids=cameras))
                elif action == "delete":
                    result = await client.delete("/api/v1/alarms/rules/rule")
                else:
                    changes = {"camera_ids": cameras} if action == "proposed" else (
                        {"enabled": False} if action == "current-enabled" else {"name": "Denied"})
                    result = await client.patch("/api/v1/alarms/rules/rule", json=changes)
                responses.append((result.status_code, result.json()))
                async with sessions() as session:
                    assert _state(await session.get(AlarmRuleEntity, "rule")) == before
                    from sqlalchemy import select
                    assert len((await session.execute(select(AlarmRuleEntity))).scalars().all()) == 1
        assert responses == [(404, {"detail": "Resource not found"})] * 3, responses
    asyncio.run(scenario())
