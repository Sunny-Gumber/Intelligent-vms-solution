"""F29: rendered Grafana and Prometheus PromQL must keep a real 5xx matcher.

The overview dashboard is JSON inside a Helm YAML literal block. A quote in
that JSON needs one backslash. Three backslashes survive rendering as a
literal backslash, so ``status=~\\"5.."`` is not a PromQL string.

``promql-parser`` is pinned in ``tests/requirements.txt`` because CI does not
install promtool. The wheel is the Prometheus parser, has no transitive
dependencies, and is not part of the production dependency audit. Every
expression below is parsed with that parser.

The same scan covers Helm dashboards, Helm alert rules, compose files, and
Grafana provisioning under ``deploy/``. Chart defaults have one dashboard and
one PrometheusRule; both are rendered.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import promql_parser
import yaml

PRODUCT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ROOT = PRODUCT_ROOT / "deploy"
CHART_ROOT = DEPLOY_ROOT / "helm" / "intelligent-vms"
VALUES_PATH = CHART_ROOT / "values.yaml"
DATASOURCE_QUOTE = "{{ .Values.observability.grafana.datasourceUid | quote }}"
THRESHOLD_PREFIX = "{{ .Values.observability.prometheusRule.thresholds."
FIVE_XX_MATCHER = 'status=~"5.."'
DASHBOARD_FIVE_XX = (
    'sum(rate(intelligent_vms_http_requests_total{status=~"5.."}[5m]))'
)
# Three backslashes before a quote: the over-escaped JSON form \\\".
OVER_ESCAPED_JSON_QUOTE = "\\" * 3 + '"'


def _helm_number(value: object) -> str:
    """Format a chart number the way Helm prints it in a rendered template."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AssertionError(f"threshold is not a number: {value!r}")
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, float):
        return format(value, ".10g")
    return str(value)


def _load_values() -> dict:
    """Load chart defaults with alert rules forced on, matching CI's render."""
    values = yaml.safe_load(VALUES_PATH.read_text(encoding="utf-8"))
    if not isinstance(values, dict):
        raise AssertionError("chart values are not a mapping")
    values["observability"]["prometheusRule"]["enabled"] = True
    return values


def _deploy_text_files() -> list[Path]:
    return [
        path
        for path in sorted(DEPLOY_ROOT.rglob("*"))
        if path.is_file() and path.suffix in {".yaml", ".yml", ".json"}
    ]


def _is_observability_artifact(path: Path) -> bool:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    tokens = ("grafana", "dashboard", "alert", "prometheus", "compose", "provisioning")
    return any(token in name or token in parts for token in tokens)


def _should_scan(path: Path, text: str) -> bool:
    if _is_observability_artifact(path):
        return True
    if OVER_ESCAPED_JSON_QUOTE in text:
        return True
    if "kind: PrometheusRule" in text:
        return True
    return '"panels"' in text and '"expr"' in text


def _yaml_literal_blocks(text: str) -> list[tuple[str, str]]:
    """Return ``key: |-`` bodies with the literal indentation removed."""
    lines = text.splitlines()
    blocks: list[tuple[str, str]] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped.endswith(": |-"):
            index += 1
            continue
        key = stripped[: -len(": |-")].strip().strip("'\"")
        index += 1
        if index >= len(lines):
            break
        indent = len(lines[index]) - len(lines[index].lstrip(" "))
        body: list[str] = []
        while index < len(lines):
            current = lines[index]
            if current.strip() == "":
                body.append("")
                index += 1
                continue
            if len(current) - len(current.lstrip(" ")) < indent:
                break
            body.append(current[indent:])
            index += 1
        while body and body[-1] == "":
            body.pop()
        blocks.append((key, "\n".join(body)))
    return blocks


def _quote_datasource(uid: object) -> str:
    """Quote a simple datasource uid the way Helm's ``quote`` prints it."""
    if not isinstance(uid, str) or not uid.isascii() or not uid.replace("-", "").replace("_", "").isalnum():
        raise AssertionError("datasource uid needs Helm's quote; refusing a partial substitute")
    return json.dumps(uid)


def _walk_exprs(node: object, origin: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        expr = node.get("expr")
        if isinstance(expr, str) and expr.strip():
            found.append((origin, expr))
        for key, value in node.items():
            if key == "expr":
                continue
            found.extend(_walk_exprs(value, origin))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_exprs(item, origin))
    return found


def _render_dashboards(text: str, values: dict, origin: str) -> list[tuple[str, str]]:
    grafana = values["observability"]["grafana"]
    if not grafana.get("enabled", True):
        return []
    quoted_uid = _quote_datasource(grafana["datasourceUid"])
    found: list[tuple[str, str]] = []
    for key, body in _yaml_literal_blocks(text):
        if not key.endswith(".json"):
            continue
        rendered = body.replace(DATASOURCE_QUOTE, quoted_uid)
        if "{{" in rendered:
            raise AssertionError(f"{origin} dashboard JSON still contains a Helm action")
        dashboard = json.loads(rendered)
        if not isinstance(dashboard, dict) or "panels" not in dashboard:
            raise AssertionError(f"{origin} {key} is not a Grafana dashboard")
        found.extend(_walk_exprs(dashboard, f"{origin}:{key}"))
    return found


def _render_prometheus_rule(text: str, values: dict, origin: str) -> list[tuple[str, str]]:
    rendered = text
    thresholds = values["observability"]["prometheusRule"]["thresholds"]
    for name, value in thresholds.items():
        rendered = rendered.replace(f"{THRESHOLD_PREFIX}{name} }}}}", _helm_number(value))
    stripped = "\n".join(line for line in rendered.splitlines() if "{{" not in line) + "\n"
    document = yaml.safe_load(stripped)
    if not isinstance(document, dict) or document.get("kind") != "PrometheusRule":
        raise AssertionError(f"{origin} did not render to a PrometheusRule")
    return _walk_exprs(document, origin)


def _embedded_json_exprs(path: Path, text: str) -> list[tuple[str, str]]:
    if '"expr"' not in text:
        return []
    decoder = json.JSONDecoder()
    origin = str(path.relative_to(PRODUCT_ROOT))
    found: list[tuple[str, str]] = []
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            break
        try:
            obj, consumed = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        found.extend(_walk_exprs(obj, origin))
        index = start + max(consumed, 1)
    return found


def _faithful_expressions() -> list[tuple[str, str]]:
    """Render deploy dashboards and alert rules without requiring the Helm binary."""
    values = _load_values()
    found: list[tuple[str, str]] = []
    for path in _deploy_text_files():
        text = path.read_text(encoding="utf-8")
        if not _should_scan(path, text):
            continue
        origin = str(path.relative_to(PRODUCT_ROOT))
        if path.suffix == ".json":
            if '"expr"' in text:
                found.extend(_walk_exprs(json.loads(text), origin))
            continue
        if "kind: PrometheusRule" in text:
            found.extend(_render_prometheus_rule(text, values, origin))
            continue
        if any(key.endswith(".json") for key, _body in _yaml_literal_blocks(text)):
            found.extend(_render_dashboards(text, values, origin))
            continue
        name = path.name.lower()
        parts = {part.lower() for part in path.parts}
        if "compose" in name:
            found.extend(_embedded_json_exprs(path, text))
            continue
        if "provisioning" in parts or "grafana" in name or "alert" in name:
            if "{{" in text and "expr" in text:
                raise AssertionError(f"unrendered expressions in {origin}")
            if "expr:" in text:
                document = yaml.safe_load(text)
                found.extend(_walk_exprs(document, origin))
        elif OVER_ESCAPED_JSON_QUOTE in text or ('"panels"' in text and '"expr"' in text):
            raise AssertionError(f"over-escaped or embedded dashboard was not rendered: {origin}")
    return found


def _helm_expressions() -> list[tuple[str, str]]:
    """Render the chart the way CI does and collect dashboard plus alert expressions."""
    helm = shutil.which("helm")
    if helm is None:
        raise AssertionError("helm is not on PATH")
    result = subprocess.run(
        [
            helm,
            "template",
            "vms-observe",
            str(CHART_ROOT),
            "--set",
            "observability.prometheusRule.enabled=true",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip() or "helm template failed")
    found: list[tuple[str, str]] = []
    for document in yaml.safe_load_all(result.stdout):
        if not isinstance(document, dict):
            continue
        kind = document.get("kind")
        if kind == "ConfigMap":
            data = document.get("data") or {}
            for key, value in data.items():
                if isinstance(key, str) and key.endswith(".json") and isinstance(value, str):
                    found.extend(_walk_exprs(json.loads(value), f"helm:{key}"))
        elif kind == "PrometheusRule":
            found.extend(_walk_exprs(document, "helm:PrometheusRule"))
    return found


def _assert_promql(origin: str, expr: str) -> None:
    try:
        promql_parser.parse(expr)
    except ValueError as exc:
        raise AssertionError(f"{origin} is not valid PromQL: {expr!r}") from exc
    if "5.." not in expr:
        return
    if "\\" in expr or FIVE_XX_MATCHER not in expr:
        raise AssertionError(f"{origin} 5xx matcher is not a PromQL string: {expr!r}")


def test_rendered_dashboard_and_alert_promql_keeps_a_real_5xx_matcher():
    faithful = _faithful_expressions()
    assert faithful, "expected rendered dashboard and alert expressions under deploy/"
    for path in _deploy_text_files():
        text = path.read_text(encoding="utf-8")
        if OVER_ESCAPED_JSON_QUOTE not in text:
            continue
        origin = str(path.relative_to(PRODUCT_ROOT))
        related = [expr for label, expr in faithful if origin in label]
        assert related, f"{origin} contains an over-escaped quote but produced no expression"
        assert any("\\" in expr for expr in related), (
            f"{origin} over-escaped quote did not reach a checked expression"
        )

    if shutil.which("helm"):
        helmed = _helm_expressions()
        assert sorted(expr.strip() for _origin, expr in helmed) == sorted(
            expr.strip() for _origin, expr in faithful
        )
        selected = helmed
    else:
        selected = faithful

    assert sum("5.." in expr for _origin, expr in selected) >= 2
    for origin, expr in selected:
        _assert_promql(origin, expr)
    assert any(expr.strip() == DASHBOARD_FIVE_XX for _origin, expr in selected)
