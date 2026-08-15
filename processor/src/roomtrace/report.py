from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .model import FrameQuality, ValidationReport


def write_quality_report(
    output_dir: Path,
    *,
    capture_label: str,
    validation: ValidationReport,
    frame_qualities: dict[int, FrameQuality],
    selected_ids: list[int],
    summary: dict[str, Any],
) -> tuple[Path, Path]:
    report = {
        "capture": capture_label,
        "validation": validation.as_json(),
        "summary": summary,
        "selected_frame_ids": selected_ids,
        "frames": [frame_qualities[key].as_json() for key in sorted(frame_qualities)],
    }
    json_path = output_dir / "quality_report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path = output_dir / "quality_report.html"
    html_path.write_text(_render_html(report), encoding="utf-8")
    return html_path, json_path


def _render_html(report: dict[str, Any]) -> str:
    validation = report["validation"]
    summary = report["summary"]
    issues = validation["issues"]
    issue_rows = "".join(
        f"<tr class='{html.escape(issue['severity'])}'><td>{html.escape(issue['severity'])}</td>"
        f"<td>{html.escape(issue['code'])}</td><td>{html.escape(issue['message'])}</td>"
        f"<td>{html.escape(str(issue.get('frame_id', '')))}</td></tr>"
        for issue in issues
    ) or "<tr><td colspan='4'>No validation issues</td></tr>"
    quality_rows = "".join(
        f"<tr><td>{item['frame_id']}</td><td>{item['quality_score']:.3f}</td>"
        f"<td>{item['blur_score']:.1f}</td><td>{item['brightness']:.3f}</td>"
        f"<td>{'yes' if item['usable'] else 'no'}</td><td>{html.escape(item['reason'])}</td></tr>"
        for item in report["frames"]
    )
    selected = ", ".join(str(item) for item in report["selected_frame_ids"])
    summary_rows = "".join(
        f"<tr><th>{html.escape(str(key))}</th><td>{html.escape(str(value))}</td></tr>"
        for key, value in summary.items()
    )
    return f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'><title>RoomTrace quality report</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;background:#f7f7f7;color:#222}}
section{{background:white;padding:1rem 1.25rem;margin:1rem 0;border-radius:.6rem;box-shadow:0 1px 4px #0002}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:.4rem .55rem;border-bottom:1px solid #ddd;text-align:left}}
tr.error{{background:#fee}}tr.warning{{background:#fff8dd}}code{{background:#eee;padding:.1rem .25rem}}
</style></head><body>
<h1>RoomTrace quality report</h1><p><code>{html.escape(report['capture'])}</code></p>
<section><h2>Summary</h2><table>{summary_rows}</table></section>
<section><h2>Validation</h2><p>Errors: {validation['errors']} · Warnings: {validation['warnings']} · Frames: {validation['frame_count']}</p>
<table><tr><th>Severity</th><th>Code</th><th>Message</th><th>Frame</th></tr>{issue_rows}</table></section>
<section><h2>Selected frames</h2><p>{html.escape(selected)}</p></section>
<section><h2>Frame quality</h2><table><tr><th>Frame</th><th>Score</th><th>Blur</th><th>Brightness</th><th>Usable</th><th>Reason</th></tr>{quality_rows}</table></section>
</body></html>"""

