"""An offline, self-contained HTML view of completed policy runs."""

from base64 import b64encode
from collections import defaultdict
from hashlib import sha256
from html import escape
import json
from math import isfinite
from pathlib import Path
from statistics import mean

from fincheck.load import fx_key

from .data import load_json
from .replay import read_trajectory, replay_key

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
    return keys


def listed_records(trajectory):
    records = {}
    for row in trajectory:
        observed = row.get("observation")
        if not isinstance(observed, dict):
            raise ValueError("Trajectory observation must be an object")
        result = observed.get("result")
        entries = result if isinstance(result, list) else [result]
        for record in entries:
            if not isinstance(record, dict):
                continue
            key = record.get("doc_id") or record.get("row_id")
            if key is None and {"date", "source_currency", "target_currency"} <= record.keys():
                key = fx_key(record["date"], record["source_currency"], record["target_currency"])
            if isinstance(key, str):
                records[key] = record
    return records


def findings_html(findings, records):
    if not findings:
        return '<p class="empty">Пустой ответ</p>'
    sections = []
    for item in findings:
        kind = escape(label(ISSUE_LABELS, item["type"]))
        ids = " · ".join(escape(value) for value in item["affected_ids"])
        amount = escape(f'{item["claimed_amount_minor"]:,}'.replace(",", "\u202f"))
        currency = escape(item["currency"])
        explanation = escape(item["explanation"])
        evidence = []
        for key in item["evidence_ids"]:
            if key in records:
                record = escape(json.dumps(records[key], ensure_ascii=False, indent=2))
                evidence.append(f'<details class="evidence"><summary><code>{escape(key)}</code></summary><pre>{record}</pre></details>')
            else:
                evidence.append(f'<span class="evidence missing"><code>{escape(key)}</code> · вне журнала</span>')
        sections.append(
            f'<article class="finding"><h4>{kind}</h4><code>{ids}</code>'
            f'<p class="amount">{amount} {currency} <small>в минимальных единицах</small></p>'
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
            subject = next((action[key] for key in ("table", "doc_id", "expression")
                            if isinstance(action.get(key), str)), "")
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


def session_html(report, trajectory, *, opened):
    episode_id, policy = replay_key(report)
    episode_label, policy_name = escape(episode_id), escape(label(POLICY_LABELS, policy))
    mode = {"train": "Обучение", "closed": "Закрытая проверка"}.get(report.get("mode"), "Прогон")
    metrics = (
        ("TP / FP / FN", f'{report["tp"]} / {report["fp"]} / {report["fn"]}'),
        ("Точность", display_number(report["precision"])),
        ("Полнота", display_number(report["recall"])),
        ("Время", f'{display_number(report["duration_seconds"])} с'),
    )
    metric_summary = "".join(f"<div><dt>{name}</dt><dd>{value}</dd></div>" for name, value in metrics)
    if policy == "oracle":
        findings = '<p class="empty">Контроль оценщика</p>'
    elif report.get("invalid_submission"):
        findings = '<p class="empty">Неверный формат ответа</p>'
    elif report.get("truncated"):
        findings = '<p class="empty">Эпизод прерван до отправки</p>'
    else:
        findings = findings_html(report.get("submitted_findings", []), listed_records(trajectory))
    return (
        f'<details class="session" data-policy="{escape(policy)}" data-episode="{episode_label}"'
        f'{" open" if opened else ""}><summary>'
        f'<span class="run-name"><strong>{episode_label}</strong><small>{policy_name} · {mode}</small></span>'
        f'<span class="score"><small>F1</small>{display_number(report["f1"])}</span>'
        f'<span class="score"><small>Награда</small>{display_number(report["reward"])}</span>'
        f'<span class="score run-steps"><small>Шаги</small>{report["steps"]}</span>'
        f'<span class="chevron" aria-hidden="true"></span></summary>'
        f'<div class="session-body"><dl>{metric_summary}</dl><h3>Выводы</h3>'
        f'<div class="findings">{findings}</div>{timeline_html(trajectory) if trajectory else ""}</div></details>'
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
    cards = []
    for policy in policies:
        runs = [report for report in reports if report["policy"] == policy]
        f1 = mean(report["f1"] for report in runs)
        reward = mean(report["reward"] for report in runs)
        cards.append(
            f'<article class="policy"><h3>{escape(label(POLICY_LABELS, policy))}</h3>'
            f'<div><strong>{display_number(f1)}</strong><span>Средний F1</span></div>'
            f'<meter min="0" max="1" value="{f1}" aria-label="Средний F1"></meter>'
            f'<p><span>Средняя награда</span><code>{display_number(reward)}</code></p></article>'
        )
    options = "".join(f'<option value="{escape(key)}">{escape(label(POLICY_LABELS, key))}</option>' for key in policies)
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
<section aria-labelledby="comparison"><h2 id="comparison">Сравнение политик</h2><div class="policies">{"".join(cards)}</div></section>
<section aria-labelledby="runs"><div class="runs-head"><h2 id="runs">Прогоны</h2>
<div class="filters"><label>Политика<select id="policy"><option value="">Все политики</option>{options}</select></label>
<label>Поиск по ID<input id="episode" type="search" placeholder="Например EP-CHK"></label></div></div>
<p id="visible-count" aria-live="polite">Показано {len(reports)} из {len(reports)}</p>{sessions}</section>
</main><script>{script}</script></body></html>
"""


def write_report(path, reports, trajectories):
    html = render_report(reports, trajectories)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8", errors="xmlcharrefreplace")


def report_directory(directory, output):
    write_report(output, load_json(directory / "reports.json"), read_trajectory(directory / "trajectories.jsonl"))
