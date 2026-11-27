"""Generate docker/grafana/dashboards/documind.json (the provisioned dashboard).

    python -m scripts.build_grafana_dashboard

The dashboard is code: panels are defined here and the JSON is regenerated,
instead of hand-editing hundreds of lines of Grafana JSON.
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docker" / "grafana" / "dashboards" / "documind.json"
DS = {"type": "prometheus", "uid": "prometheus"}
API = 'route=~"/api/.*"'  # application endpoints only (not /health, /)


def target(expr: str, legend: str = "", ref: str = "A") -> dict:
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}


def stat(title: str, expr: str, x: int, y: int, unit: str = "short", w: int = 4, decimals: int | None = None) -> dict:
    options = {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
               "colorMode": "value", "graphMode": "area", "textMode": "auto"}
    defaults: dict = {"unit": unit, "color": {"mode": "thresholds"},
                      "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "stat", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": 4},
            "targets": [target(expr)], "options": options, "fieldConfig": {"defaults": defaults, "overrides": []}}


def series(title: str, targets: list[dict], x: int, y: int, unit: str = "short", w: int = 12, stack: bool = False) -> dict:
    custom = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 15 if stack else 5, "showPoints": "never",
              "stacking": {"mode": "normal" if stack else "none", "group": "A"}}
    return {"type": "timeseries", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": 8},
            "targets": targets,
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
            "fieldConfig": {"defaults": {"unit": unit, "custom": custom}, "overrides": []}}


def row(title: str, y: int) -> dict:
    return {"type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}


def build() -> dict:
    p95 = "histogram_quantile(0.95, sum by (le{by}) (rate({metric}_bucket{sel}[5m])))"
    panels = [
        row("Traffic", 0),
        stat("Requests / s", f"sum(rate(documind_http_requests_total{{{API}}}[5m]))", 0, 1, "reqps", decimals=2),
        stat("5xx error rate", f'sum(rate(documind_http_requests_total{{{API},status=~"5.."}}[5m])) '
             f"/ clamp_min(sum(rate(documind_http_requests_total{{{API}}}[5m])), 1e-9)", 4, 1, "percentunit", decimals=2),
        stat("p95 latency", p95.format(by="", metric="documind_http_request_duration_seconds", sel=f"{{{API}}}"), 8, 1, "s", decimals=2),
        stat("In progress", "sum(documind_http_requests_in_progress)", 12, 1),
        stat("Users", "max(documind_users)", 16, 1),
        stat("Queue length", "max(documind_processing_queue_length)", 20, 1),
        series("Requests / s by route", [target(f"sum by (route) (rate(documind_http_requests_total{{{API}}}[5m]))", "{{route}}")], 0, 5, "reqps"),
        series("p95 latency by route", [target(p95.format(by=", route", metric="documind_http_request_duration_seconds", sel=f"{{{API}}}"), "{{route}}")], 12, 5, "s"),
        series("Responses by status", [target(f"sum by (status) (rate(documind_http_requests_total{{{API}}}[5m]))", "{{status}}")], 0, 13, "reqps", stack=True),
        series("Rate-limited requests / s", [target("sum by (scope) (rate(documind_rate_limited_requests_total[5m]))", "{{scope}}")], 12, 13, "reqps"),
        row("Answers (RAG)", 21),
        series("p95 duration by stage", [target(p95.format(by=", stage", metric="documind_rag_stage_duration_seconds", sel=""), "{{stage}}")], 0, 22, "s"),
        series("Answers / min by mode", [target('sum by (mode, found) (rate(documind_rag_answers_total[5m])) * 60', "{{mode}} found={{found}}")], 12, 22, "short"),
        stat("Answer cache hit ratio", 'sum(rate(documind_cache_events_total{kind="answer",result="hit"}[1h])) '
             '/ clamp_min(sum(rate(documind_cache_events_total{kind="answer"}[1h])), 1e-9)', 0, 30, "percentunit", w=6, decimals=1),
        stat("Semantic cache hit ratio", 'sum(rate(documind_cache_events_total{kind="semantic",result="hit"}[1h])) '
             '/ clamp_min(sum(rate(documind_cache_events_total{kind="semantic"}[1h])), 1e-9)', 6, 30, "percentunit", w=6, decimals=1),
        stat("Embedding cache hit ratio", 'sum(rate(documind_cache_events_total{kind="embedding",result="hit"}[1h])) '
             '/ clamp_min(sum(rate(documind_cache_events_total{kind="embedding"}[1h])), 1e-9)', 12, 30, "percentunit", w=6, decimals=1),
        stat("Answers (last 1 h)", "sum(increase(documind_rag_answers_total[1h]))", 18, 30, w=6, decimals=0),
        row("Documents & workers", 34),
        series("Documents by status", [target("sum by (status) (documind_documents)", "{{status}}")], 0, 35, "short", stack=True),
        series("Processed / min", [target("sum by (status) (rate(documind_documents_processed_total[5m])) * 60", "{{status}}")], 12, 35, "short"),
        stat("Avg processing time", "sum(rate(documind_document_processing_seconds_total[30m])) "
             "/ clamp_min(sum(rate(documind_documents_processed_total[30m])), 1e-9)", 0, 43, "s", w=6, decimals=1),
        stat("Pages processed (24 h)", "increase(documind_document_pages_processed_total[24h])", 6, 43, w=6, decimals=0),
        stat("Database up", "min(documind_database_up)", 12, 43, w=6),
        stat("Redis up", "min(documind_redis_up)", 18, 43, w=6),
    ]
    for i, panel in enumerate(panels, start=1):
        panel["id"] = i
    return {
        "uid": "documind-overview", "title": "DocuMind AI - overview", "tags": ["documind"],
        "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": False,
        "refresh": "15s", "time": {"from": "now-1h", "to": "now"},
        "templating": {"list": []}, "annotations": {"list": []}, "panels": panels,
    }


if __name__ == "__main__":
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
