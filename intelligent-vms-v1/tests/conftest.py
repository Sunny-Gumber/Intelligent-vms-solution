import sys
from pathlib import Path

CONTROL = Path(__file__).parents[1] / "services" / "control-api"
if str(CONTROL) not in sys.path:
    sys.path.insert(0, str(CONTROL))


def pytest_configure(config):
    """Register the VMS-FIX-033 reproduction marker."""
    config.addinivalue_line(
        "markers",
        "known_race: reproduced conditional race that asserts the correct outcome "
        "and fails until its follow-up fix. Excluded from the default suite; "
        "run with -m known_race. Not an xfail.",
    )


def pytest_collection_modifyitems(config, items):
    """Keep known_race reproductions out of the default suite.

    An explicit ``-m`` expression that names ``known_race`` opts in. Otherwise
    the reproductions are deselected, not skipped and not xfailed, so a red
    reproduction cannot fail CI.
    """
    markexpr = getattr(config.option, "markexpr", "") or ""
    if "known_race" in markexpr:
        return
    selected = []
    deselected = []
    for item in items:
        if item.get_closest_marker("known_race"):
            deselected.append(item)
        else:
            selected.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected
