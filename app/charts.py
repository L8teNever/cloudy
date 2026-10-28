"""Diagramme als schlichtes HTML/CSS (ohne JavaScript-Bibliothek, funktioniert offline und auf dem Handy).

Farben kommen aus CSS-Variablen (--s1 Download, --s2 Upload, --s3 Aufrufe), geprüft auf Farbenblindheit.
Jede Markierung hat data-tip: Das kleine Skript in base.html zeigt den genauen Wert beim Darüberfahren.
"""

from collections.abc import Callable, Sequence

from markupsafe import Markup, escape

Series = Sequence[tuple[str, str, str]]  # (Schlüssel, Bezeichnung, CSS-Klasse s1/s2/s3)

TRAFFIC: Series = (("bytes_down", "Download", "s1"), ("bytes_up", "Upload", "s2"))
HITS: Series = (("hits", "Aufrufe", "s3"),)


def legend(series: Series) -> Markup:
    if len(series) < 2:
        return Markup("")
    items = "".join(
        f'<span class="lg"><span class="sw {cls}"></span>{escape(label)}</span>' for _, label, cls in series
    )
    return Markup(f'<div class="legend">{items}</div>')


def _tip(title: str, row: dict, series: Series, fmt: Callable[[int], str]) -> str:
    parts = [str(escape(title))] + [f"{escape(label)}: {escape(fmt(row.get(key) or 0))}" for key, label, _ in series]
    return " · ".join(parts)


def hbars(
    rows: list[dict],
    series: Series,
    fmt: Callable[[int], str],
    label: Callable[[dict], str] | str,
    href: Callable[[dict], str] | str | None = None,
    empty: str = "Noch keine Daten in diesem Zeitraum.",
) -> Markup:
    """Liegende Balken (eine Zeile pro Eintrag), bei mehreren Werten gestapelt. Längster Balken = 100 %."""
    if not rows:
        return Markup(f'<p class="muted">{escape(empty)}</p>')
    if label == "device":
        label_fn = lambda r: str(r.get("hostname") or r.get("ip") or "unbekannt")
        href_fn = (lambda r: f"/geraete/{r.get('ip')}") if href is None or href == "device" else href
    elif isinstance(label, str):
        col = label
        label_fn = lambda r: str(r.get(col) or "")
        href_fn = href
    else:
        label_fn = label
        href_fn = href

    if isinstance(href_fn, str):
        prefix = href_fn
        href_fn = lambda r: f"{prefix}{r.get('ip') or r.get('domain') or ''}"

    totals = [sum(int(r.get(k) or 0) for k, _, _ in series) for r in rows]
    peak = max(totals) or 1
    out = [str(legend(series)), '<div class="hbars">']
    for row, total in zip(rows, totals):
        name = label_fn(row)
        name_html = escape(name)
        if href_fn:
            name_html = Markup(f'<a href="{escape(href_fn(row))}" data-nav>{name_html}</a>')
        segs = []
        for key, _, cls in series:
            v = int(row.get(key) or 0)
            if v > 0:
                segs.append(f'<span class="seg {cls}" style="flex:{v}"></span>')
        width = max(total / peak * 100, 0.6 if total else 0)
        out.append(
            f'<div class="hb" data-tip="{escape(_tip(name, row, series, fmt))}">'
            f'<div class="hb-label" title="{escape(name)}">{name_html}</div>'
            f'<div class="hb-track"><div class="hb-bar" style="width:{width:.2f}%">{"".join(segs)}</div></div>'
            f'<div class="hb-value">{escape(fmt(total))}</div></div>'
        )
    out.append("</div>")
    return Markup("".join(out))


def columns(
    buckets: list[dict],
    series: Series,
    fmt: Callable[[int], str],
    empty: str = "Noch keine Daten in diesem Zeitraum.",
) -> Markup:
    """Stehende Säulen über die Zeit (stündlich/täglich), bei mehreren Werten gestapelt."""
    totals = [sum(int(b.get(k) or 0) for k, _, _ in series) for b in buckets]
    if not buckets or not any(totals):
        return Markup(f'<p class="muted">{escape(empty)}</p>')
    peak = max(totals)
    every = max(1, -(-len(buckets) // 8))  # höchstens ~8 Beschriftungen auf der X-Achse
    cols = []
    for i, (b, total) in enumerate(zip(buckets, totals)):
        segs = []
        for key, _, cls in reversed(series):  # erster Wert unten
            v = int(b.get(key) or 0)
            if v > 0:
                segs.append(f'<span class="seg {cls}" style="flex:{v}"></span>')
        height = total / peak * 100 if total else 0
        tick = escape(b["label"]) if i % every == 0 else ""
        cols.append(
            f'<div class="col" data-tip="{escape(_tip(b["label"], b, series, fmt))}">'
            f'<div class="col-area"><div class="col-bar" style="height:{height:.2f}%">{"".join(segs)}</div></div>'
            f'<div class="col-tick">{tick}</div></div>'
        )
    return Markup(
        f'{legend(series)}<div class="cols">'
        f'<div class="cols-axis"><span>{escape(fmt(peak))}</span><span>{escape(fmt(peak // 2))}</span><span>0</span></div>'
        f'<div class="cols-plot">{"".join(cols)}</div></div>'
    )
