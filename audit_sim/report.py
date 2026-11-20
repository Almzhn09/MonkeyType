"""An offline, self-contained HTML view of completed policy runs."""

from base64 import b64encode
from collections import Counter, defaultdict
from hashlib import sha256
from html import escape
import json
from math import isfinite
from pathlib import Path

from fincheck.load import fx_key

from .data import load_json
from .environment import STEP_LIMIT
from .replay import read_trajectory, replay_key
from .scoring import validate_submission

POLICY_LABELS = {
    "reconcile": "Сверка",
    "mark_all": "Массовая пометка",
    "random": "Случайный агент",
    "oracle": "Oracle оценщика",
    "interactive": "Ручная сессия",
}
ISSUE_LABELS = {
    "duplicate_invoice": "Дубль счёта",
    "payment_allocation": "Распределение платежа",
    "fx_conversion": "Конвертация валюты",
    "reporting_period": "Отчётный период",
    "wrong_period": "Отчётный период",
}
ACTION_LABELS = {
    "list_records": "Чтение таблицы",
    "get_record": "Чтение записи",
    "get_document": "Чтение документа",
    "calculator": "Расчёт",
    "submit": "Отправка выводов",
}
METRICS = ("tp", "fp", "fn", "precision", "recall", "f1", "reward", "steps", "duration_seconds")
COMPARE_METRICS = {
    "reward": "Награда", "f1": "F1", "precision": "Точность", "recall": "Полнота", "steps": "Число шагов",
}


def label(names, key):
    return names.get(key, key) if isinstance(key, str) else "Некорректное действие"


def display_number(value, digits=3):
    if type(value) is int:
        return str(value)
    return f"{value:.{digits}f}".rstrip("0").rstrip(".").replace(".", ",")


def validate_reports(reports):
    if not isinstance(reports, list) or not reports:
        raise ValueError("Reports must contain at least one run")
    keys = set()
    for report in reports:
        key = replay_key(report)
        if key in keys:
            raise ValueError("Duplicate report for an episode and policy")
        keys.add(key)
        values = (report.get(name) for name in METRICS)
        if any(type(value) is not int and (type(value) is not float or not isfinite(value)) for value in values):
            raise ValueError("Report is missing a finite evaluation metric")
        if not all(0 <= report[name] <= 1 for name in ("precision", "recall", "f1")) or not -1 <= report["reward"] <= 1:
            raise ValueError("Report metric is outside its valid range")
        if not all(type(report[name]) is int and report[name] >= 0 for name in ("tp", "fp", "fn", "steps")):
            raise ValueError("Report counts must be nonnegative integers")
        if report["steps"] > STEP_LIMIT or report["duration_seconds"] < 0:
            raise ValueError("Report has an invalid duration or step count")
        if key[1] != "oracle" and not validate_submission({
            "episode_id": key[0], "findings": report.get("submitted_findings", []),
        }, key[0]):
            raise ValueError("Report contains malformed submitted findings")
    return keys


def evidence_key(value):
    return value.strip().upper()


def recorded_evidence(observation):
    result = observation.get("result")
    for record in result if isinstance(result, list) else (result,):
        if not isinstance(record, dict):
            continue
        key = record.get("doc_id") or record.get("row_id")
        fx_fields = ("date", "source_currency", "target_currency")
        if key is None and all(isinstance(record.get(name), str) for name in fx_fields):
            key = fx_key(*(record[name] for name in fx_fields))
        if isinstance(key, str) and (canonical := evidence_key(key)):
            yield canonical, record


def trace_progress(trajectory, findings, expected_steps):
    records = {}
    cited = {evidence_key(key) for item in findings for key in item["evidence_ids"]}
    counts = [(0, 0)]
    for step, row in enumerate(trajectory, 1):
        if step > expected_steps or type(row.get("step")) is not int or row["step"] != step:
            raise ValueError("Trajectory steps must be consecutive and within the reported step count")
        observed = row.get("observation")
        if not isinstance(observed, dict):
            raise ValueError("Trajectory observation must be an object")
        for key, record in recorded_evidence(observed):
            records[key] = record
        counts.append((len(records), len(cited & records.keys())))
    return records, counts, len(cited)


def findings_html(findings, records):
    if not findings:
        return '<p class="empty">Пустой ответ</p>'
    maxima = defaultdict(int)
    counts = Counter(item["currency"] for item in findings)
    for item in findings:
        currency = item["currency"]
        maxima[currency] = max(maxima[currency], abs(item["claimed_amount_minor"]))
    sections = []
    for item in findings:
        kind = escape(label(ISSUE_LABELS, item["type"]))
        ids = " · ".join(escape(value) for value in item["affected_ids"])
        amount = escape(f'{item["claimed_amount_minor"]:,}'.replace(",", "\u202f"))
        currency = item["currency"]
        explanation = escape(item["explanation"])
        share = abs(item["claimed_amount_minor"]) / maxima[currency] if maxima[currency] else 0
        bar = f'<div class="amount-track" aria-hidden="true"><span style="width:{share:.2%}"></span></div>' if counts[currency] > 1 else ""
        evidence = []
        for key in item["evidence_ids"]:
            observed = records.get(evidence_key(key))
            if observed is not None:
                record = escape(json.dumps(observed, ensure_ascii=False, indent=2))
                evidence.append(f'<details class="evidence"><summary><code>{escape(key)}</code></summary><pre>{record}</pre></details>')
            else:
                evidence.append(f'<span class="evidence missing"><code>{escape(key)}</code> · вне журнала</span>')
        sections.append(
            f'<article class="finding"><h4>{kind}</h4><code>{ids}</code>'
            f'<p class="amount">{amount} {currency} <small>в минимальных единицах</small></p>'
            f'{bar}'
            f'<p class="explanation">{explanation}</p>'
            f'<div class="proof">{"".join(evidence)}</div></article>'
        )
    return "".join(sections)


def timeline_html(trajectory):
    rows = []
    for item in trajectory:
        action = item.get("action")
        event = label(ACTION_LABELS, action.get("action") if isinstance(action, dict) else None)
        subject = ""
        if isinstance(action, dict):
            subject = " · ".join(action[key] for key in ("table", "id", "doc_id", "expression")
                                 if isinstance(action.get(key), str))
        result = item["observation"].get("error")
        description = "Запрос"
        if result:
            description = f"Ошибка · {result}"
        if item.get("terminated") or item.get("truncated"):
            description = "Эпизод завершён" if item.get("terminated") else "Эпизод прерван"
        rows.append(
            f'<li><span class="step">{int(item["step"]):02d}</span>'
            f'<span>{escape(event)} <code>{escape(subject[:100])}</code></span>'
            f'<small>{escape(description)}</small></li>'
        )
    return f'<details class="timeline"><summary>Ход расследования · {len(rows)} шагов</summary><ol>{"".join(rows)}</ol></details>'


def investigation_html(trajectory, counts, cited, total_steps):
    missing = f'<p class="empty">Записано {len(trajectory)} из {total_steps} шагов.</p>' if len(trajectory) < total_steps else ""
    if not trajectory:
        return missing
    points = escape(json.dumps(counts, separators=(",", ":")), quote=True)
    records, observed = counts[-1]
    selector = (
        '<label class="trace-selector">Показатель<select class="trace-series">'
        '<option value="0">Уникальные записи</option><option value="1">ID из ответа</option>'
        '</select></label>'
    ) if cited else ""
    proof = f'<span>ID из ответа <strong class="trace-proof">{observed} из {cited}</strong></span>' if cited else ""
    return (
        f'<figure class="investigation" data-progress="{points}" data-cited="{cited}">'
        f'<div class="trace-head"><figcaption>Динамика расследования</figcaption>{selector}</div>{missing}'
        '<canvas class="trace-graph" aria-hidden="true"></canvas>'
        '<div class="trace-controls">'
        f'<label>Шаг<input class="trace-step" type="range" min="0" max="{len(trajectory)}" value="{len(trajectory)}"></label>'
        f'<output class="trace-values" aria-live="polite"><span>Шаг <strong class="trace-number">{len(trajectory)}</strong></span>'
        f'<span>Записи <strong class="trace-records">{records}</strong></span>{proof}</output></div>'
        f'{timeline_html(trajectory)}</figure>'
    )


def session_html(report, trajectory, *, opened):
    episode_id, policy = replay_key(report)
    episode_label, policy_name = escape(episode_id), escape(label(POLICY_LABELS, policy))
    raw_mode = report.get("mode")
    mode = {"train": "Обучение", "closed": "Закрытая проверка"}.get(raw_mode, "Прогон") if isinstance(raw_mode, str) else "Прогон"
    metrics = (
        ("TP / FP / FN", f'{report["tp"]} / {report["fp"]} / {report["fn"]}'),
        ("Точность", display_number(report["precision"])),
        ("Полнота", display_number(report["recall"])),
        ("Время", f'{display_number(report["duration_seconds"])} с'),
    )
    metric_summary = "".join(f"<div><dt>{name}</dt><dd>{value}</dd></div>" for name, value in metrics)
    submitted = [] if policy == "oracle" else report.get("submitted_findings", [])
    records, counts, cited = trace_progress(trajectory, submitted, report["steps"])
    if policy == "oracle":
        if trajectory:
            raise ValueError("Oracle reports do not have public trajectory steps")
        findings = '<p class="empty">Контроль оценщика</p>'
    elif report.get("invalid_submission"):
        findings = '<p class="empty">Неверный формат ответа</p>'
    elif report.get("truncated"):
        findings = '<p class="empty">Эпизод прерван до отправки</p>'
    else:
        findings = findings_html(submitted, records)
    comparable = len({item["currency"] for item in submitted}) < len(submitted)
    amounts_key = '<p class="chart-key">Полосы сравнивают модуль заявленных сумм в пределах одной валюты.</p>' if comparable else ""
    investigation = "" if policy == "oracle" else investigation_html(trajectory, counts, cited, report["steps"])
    compare_data = " ".join(f'data-{key}="{report[key]}"' for key in COMPARE_METRICS)
    return (
        f'<details class="session" data-policy="{escape(policy)}" data-episode="{episode_label}" {compare_data}'
        f'{" open" if opened else ""}><summary>'
        f'<span class="run-name"><strong>{episode_label}</strong><small>{policy_name} · {mode}</small></span>'
        f'<span class="score"><small>F1</small>{display_number(report["f1"])}</span>'
        f'<span class="score"><small>Награда</small>{display_number(report["reward"])}</span>'
        f'<span class="score run-steps"><small>Шаги</small>{report["steps"]}</span>'
        f'<span class="chevron" aria-hidden="true"></span></summary>'
        f'<div class="session-body"><dl>{metric_summary}</dl><h3>Выводы</h3>'
        f'{amounts_key}<div class="findings">{findings}</div>{investigation}</div></details>'
    )


def render_report(reports, trajectories):
    keys = validate_reports(reports)
    groups = defaultdict(list)
    for row in trajectories:
        key = replay_key(row)
        if key not in keys:
            raise ValueError("Trajectory has no matching report")
        groups[key].append(row)
    policies = sorted({key[1] for key in keys}, key=lambda key: (key != "reconcile", key))
    options = "".join(f'<option value="{escape(key)}">{escape(label(POLICY_LABELS, key))}</option>' for key in policies)
    metrics = "".join(f'<option value="{key}">{name}</option>' for key, name in COMPARE_METRICS.items())
    sessions = "".join(session_html(item, groups[replay_key(item)], opened=index == 0)
                       for index, item in enumerate(reports))
    asset = Path(__file__)
    css = asset.with_suffix(".css").read_text(encoding="utf-8")
    script = asset.with_suffix(".js").read_text(encoding="utf-8")
    digest = b64encode(sha256(script.encode("utf-8")).digest()).decode("ascii")
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-{digest}'; base-uri 'none'; form-action 'none'">
<title>MonkeyType — отчёт аудита</title><style>{css}</style></head><body>
<main>
<header><div class="brand">MonkeyType <span> / </span> Genesis FinTech</div><h1>Отчёт аудита</h1>
<p>Эпизоды {len({episode for episode, _ in keys})} · Прогоны {len(reports)} · Действия {sum(len(items) for items in groups.values())}</p></header>
<section aria-labelledby="comparison"><div class="runs-head"><h2 id="comparison">Сравнение политик</h2>
<div class="filters"><label>Политика<select id="policy"><option value="">Все политики</option>{options}</select></label>
<label>Поиск по ID<input id="episode" type="search" placeholder="Например EP-CHK"></label></div></div>
<figure class="comparison"><div class="comparison-head"><figcaption>Среднее по выбранным прогонам</figcaption>
<label class="metric-selector">Показатель<select id="metric">{metrics}</select></label></div>
<div id="comparison-axis" class="comparison-axis" aria-hidden="true"></div><ul id="policy-chart" class="policy-chart" data-step-limit="{STEP_LIMIT}"></ul>
<p id="comparison-empty" class="empty" hidden>Нет прогонов для выбранных фильтров.</p></figure></section>
<section aria-labelledby="runs"><h2 id="runs">Прогоны</h2>
<p id="visible-count" aria-live="polite">Показано {len(reports)} из {len(reports)}</p>{sessions}</section>
</main><script>{script}</script></body></html>
"""


def write_report(path, reports, trajectories):
    html = render_report(reports, trajectories)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8", errors="xmlcharrefreplace")


def report_directory(directory, output):
    write_report(output, load_json(directory / "reports.json"), read_trajectory(directory / "trajectories.jsonl"))
