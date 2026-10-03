import asyncio
from types import SimpleNamespace

import pytest

from app.services import coordination, placement, reconciler


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class SharedAdvisoryState:
    def __init__(self):
        self.held = False
        self.keys = []

    def session(self):
        state = self

        class Session:
            def get_bind(self):
                return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

            async def execute(self, _statement, params):
                state.keys.append(params["key"])
                if state.held:
                    return _Result(False)
                state.held = True
                return _Result(True)

        return Session()

    def release(self):
        self.held = False


def test_reconciler_and_placement_use_same_execution_fence_key():
    state = SharedAdvisoryState()
    reconcile_session = state.session()
    placement_session = state.session()

    assert asyncio.run(reconciler._leader_lock(reconcile_session)) is True
    assert asyncio.run(placement._leader_lock(placement_session)) is False
    assert state.keys == [
        coordination.PLACEMENT_EXECUTION_LOCK_KEY,
        coordination.PLACEMENT_EXECUTION_LOCK_KEY,
    ]


def test_placement_blocks_reconciler_from_starting_stale_generation_mutation():
    state = SharedAdvisoryState()
    placement_session = state.session()
    reconcile_session = state.session()

    assert asyncio.run(placement._leader_lock(placement_session)) is True
    assert asyncio.run(reconciler._leader_lock(reconcile_session)) is False

    state.release()

    # After the ownership transaction releases, the next reconciler may proceed
    # and will load the new assignment generation in its own transaction.
    assert asyncio.run(reconciler._leader_lock(reconcile_session)) is True


def test_route_mutation_gate_fails_closed_when_placement_transition_is_busy():
    state = SharedAdvisoryState()
    owner_session = state.session()
    mutation_session = state.session()

    assert asyncio.run(coordination.try_placement_execution_lock(owner_session)) is True

    with pytest.raises(coordination.PlacementExecutionBusy):
        asyncio.run(
            coordination.require_placement_execution_lock(mutation_session)
        )


def test_sqlite_dev_mode_keeps_existing_single_process_behavior():
    class SqliteSession:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    assert asyncio.run(
        coordination.try_placement_execution_lock(SqliteSession())
    ) is True
