from datetime import datetime, timezone

from app.services.alarm_rules import RuleSnapshot, dedupe_key, rule_matches


def rule(**overrides):
    base = dict(
        id="rule-1",
        tenant_id="tenant-a",
        site_id="site-1",
        name="After-hours motion",
        event_types=frozenset({"motion"}),
        severities=frozenset(),
        camera_ids=frozenset(),
        alarm_severity="high",
        cooldown_seconds=60,
    )
    base.update(overrides)
    return RuleSnapshot(**base)


def event(**overrides):
    base = {
        "event_id": "event-1",
        "tenant_id": "tenant-a",
        "site_id": "site-1",
        "camera_id": "cam-1",
        "timestamp": datetime(2026, 9, 24, 10, 0, 5, tzinfo=timezone.utc).isoformat(),
        "event_type": "motion",
        "severity": "medium",
        "attributes": {},
    }
    base.update(overrides)
    return base


def test_rule_scope_and_event_matching():
    assert rule_matches(rule(), event())
    assert not rule_matches(rule(), event(tenant_id="tenant-b"))
    assert not rule_matches(rule(), event(site_id="site-2"))
    assert not rule_matches(rule(camera_ids=frozenset({"cam-2"})), event())


def test_initialized_property_does_not_open_alarm():
    assert not rule_matches(
        rule(),
        event(attributes={"property_operation": "Initialized"}),
    )


def test_cooldown_dedupe_is_stable_inside_bucket():
    first = event(event_id="a")
    second = event(
        event_id="b",
        timestamp=datetime(2026, 9, 24, 10, 0, 40, tzinfo=timezone.utc).isoformat(),
    )
    assert dedupe_key(rule(), first) == dedupe_key(rule(), second)


def test_zero_cooldown_uses_event_identity():
    r = rule(cooldown_seconds=0)
    assert dedupe_key(r, event(event_id="a")) != dedupe_key(r, event(event_id="b"))
