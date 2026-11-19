# Контракт данных эпизода — v1.0 (A01–A02)

Единый источник правды для генератора (Алан) и среды/оценщика (Алимжан). Любое изменение —
через повышение `schema_version` и правку этого файла. Требования кейса — в
[`docs/spec/genesis-excerpt.md`](spec/genesis-excerpt.md).

## 1. Раскладка файлов (A02)

```
episodes/
  public/<split>/<episode_id>/      # открытый набор: доступен агенту на чтение через API
    manifest.json  policy.json  ledger.csv  invoices.jsonl  payments.jsonl  fx.csv
  private/<split>/<episode_id>/     # закрытый набор: только процесс оценщика
    truth.json  provenance.json  [id_map.json — только у парных эпизодов]
configs/<split>.json                # открытые конфигурации и seed train/dev (не для API)
```

- Запускатель передаёт API путь `episodes/public/<split>/<episode_id>` и больше ничего.
  Путь `episodes/private/...` обрабатывает доверенный оценщик. Политика получает данные по
  JSON-каналу. Ядро блокирует у процесса политики открытие файлов и сокетов.
- Имена файлов и каталогов не зависят от наличия, типа или числа ошибок.
- `split ∈ {train, dev}`; закрытый прогон жюри использует свои split/seed тем же генератором.

## 2. Общие правила значений

- **Кодировка:** UTF-8, перевод строки `\n`. JSON — `sort_keys`, без NaN. JSONL — один
  объект на строку. CSV — заголовок в первой строке, разделитель `,`, RFC 4180.
- **Время:** ISO-8601 со смещением (`2026-03-31T23:40:00+05:00`). Смещение в документах
  может быть как локальным для компании, так и `+00:00` — сравнивать можно только после
  перевода в часовой пояс компании. Строки `ledger.csv` всегда в смещении компании.
- **Часовой пояс компании:** IANA-имя из списка без перехода на летнее время, плюс явное
  смещение в `manifest.utc_offset`: `Asia/Almaty +05:00`, `Asia/Dubai +04:00`,
  `Asia/Kolkata +05:30`, `Asia/Tokyo +09:00`, `America/Bogota -05:00`, `Etc/UTC +00:00`.
  Локальная дата = дата момента в `utc_offset`.
- **Деньги:** целые минимальные единицы (`*_minor`), без дробей. Разрядность — в
  `manifest.currency_exponents` (`KZT 2, USD 2, EUR 2, GBP 2, CNY 2, JPY 0, KWD 3`).
- **FX:** `rate` — десятичная строка (до 6 знаков), единиц `target_currency` за 1 единицу
  `source_currency`. `target_currency` всегда базовая валюта эпизода.
- **Отсутствующие ссылки:** `null` в JSON, пустое поле в CSV. Загрузчик читает пустое поле
  CSV как `null`. Ссылки не угадываются: если документа нет, ссылки нет.
- **Пространство ID** (всё в верхнем регистре, без пробелов):
  - `row_id` — строка книги; `entry_id` — проводка (группа строк);
  - `doc_id` — документ (счёт или платёж);
  - `invoice_id`, `payment_id` — бизнес-номера документов;
  - `entity_id` — контрагент;
  - FX-ключ: `FX:<YYYY-MM-DD>:<SOURCE>:<TARGET>`, например `FX:2026-03-31:EUR:KZT`.
  ID непрозрачны: случайные токены из RNG эпизода, префиксы меняются между эпизодами.
  Ни ID, ни порядок строк не выдают внесённые ошибки: внесённые строки получают ID из того
  же распределения, строки сортируются по `(timestamp, row_id)`, документы — по `doc_id`.
- **Порядок не несёт смысла.** Генератор пишет строки `ledger.csv` по `(timestamp, row_id)`,
  документы — по `doc_id`, JSON — с `sort_keys`. Но читатели (API, оценщик, fincheck)
  **обязаны** принимать любой порядок строк книги, строк `fx.csv`, документов в JSONL и
  ключей в JSON-объектах: жюри и парные эпизоды (A18, §6 `id_map.json`) переставляют их.
  Нарушение канонического порядка — предупреждение, не ошибка. Исключения, где порядок
  смысловой: `invoice_refs` (остаток округления — в последний ref, §4) и `line_items`.

## 3. Открытые файлы

### manifest.json
```json
{
  "schema_version": "1.0",
  "episode_id": "EP-…",
  "company": {"name": "…"},
  "base_currency": "KZT",
  "currency_exponents": {"EUR": 2, "KZT": 2},
  "timezone": "Asia/Almaty",
  "utc_offset": "+05:00",
  "period_start": "2026-03-01T00:00:00+05:00",
  "period_end":   "2026-04-01T00:00:00+05:00",
  "extract_start": "2026-02-19T00:00:00+05:00",
  "extract_end":   "2026-04-11T00:00:00+05:00",
  "counts": {"ledger_rows": 0, "entries": 0, "invoices": 0, "payments": 0, "documents": 0, "fx_rates": 0},
  "rounding": {"mode": "half_up", "scope": "per_conversion"},
  "files": {"ledger.csv": "<sha256>", "…": "…"}
}
```
`period_end` и `extract_end` — исключающие границы. Выгрузка охватывает период и окно
отсечения вокруг него (`extract_*`), чтобы были видны записи соседних периодов.
`documents = invoices + payments`. `files` — sha256 остальных открытых файлов.

### policy.json
```json
{
  "schema_version": "1.0",
  "episode_id": "EP-…",
  "base_currency": "KZT",
  "period": {"start": "…", "end": "…", "end_exclusive": true, "timezone": "Asia/Almaty", "utc_offset": "+05:00"},
  "fx": {"date_rule": "event_local_date", "target_currency": "KZT", "rate_direction": "target_per_source"},
  "rounding": {"mode": "half_up", "scope": "per_conversion"},
  "recognition": {"invoice": "issued_at_local_date", "payment": "timestamp_local_date"},
  "allocation": {"rule": "per_invoice_refs", "rounding_remainder": "last_ref", "unapplied_account_role": "unapplied"},
  "accounts": {"ar": "…", "revenue": "…", "bank": "…", "unapplied": "…", "expense": ["…"]}
}
```
Значения `period`, `rounding`, `base_currency` совпадают с manifest (проверяется тестом).
`accounts` сопоставляет роли с кодами счетов эпизода (коды и названия меняются).

### ledger.csv
Колонки: `row_id,entry_id,timestamp,account,debit_minor,credit_minor,currency,entity_id,invoice_id,payment_id`
(поля §3 кейса + `entry_id`). В строке ровно одно из `debit_minor`/`credit_minor` > 0,
другое `0`. `currency` — всегда базовая валюта (книга ведётся в базовой валюте).
Каждая проводка (`entry_id`) сбалансирована: Σdebit = Σcredit; все строки проводки имеют
один `timestamp`.

### invoices.jsonl
`{"doc_id","invoice_id","entity_id","issued_at","due_at","currency","total_minor","line_items":[{"line_id","description","quantity","unit_price_minor","amount_minor"}]}`
— Σ`amount_minor` = `total_minor`, `amount_minor = quantity × unit_price_minor`.

### payments.jsonl
`{"doc_id","payment_id","entity_id","timestamp","currency","amount_minor","invoice_refs":[{"invoice_id","amount_minor"}]}`
— `amount_minor > 0` — поступление; `< 0` — возврат клиенту. Суммы `invoice_refs` в валюте
платежа, Σrefs ≤ |amount|. Остаток (`amount − Σrefs`) законно уходит на счёт роли
`unapplied` (аванс). `entity_id` — дополнительное поле.

### fx.csv
`date,source_currency,target_currency,rate` — курс на каждую локальную дату окна выгрузки для
каждой иностранной валюты эпизода, без пропусков.

## 4. Правила учёта (эталонная история до ошибок)

- **Конвертация:** `base = round(amount_minor × rate × 10^(exp_base − exp_src))` по
  `rounding.mode` (`half_up` | `half_even`), к целому, отдельно для каждой конвертации.
  Курс — из `fx.csv` на **локальную дату события** (`issued_at` счёта, `timestamp` платежа).
  Документ в базовой валюте не конвертируется.
- **Счёт:** одна проводка на локальную дату `issued_at`:
  Dr `ar` = base(total), Cr `revenue` = base(total); строки несут `entity_id`, `invoice_id`,
  `payment_id = null`.
- **Поступление:** одна проводка на локальную дату `timestamp`: Dr `bank` = base(amount);
  на каждый ref — Cr `ar` = base(ref) с `invoice_id` ref и `payment_id`; если Σrefs = amount,
  разница округления уходит в последний ref (по порядку `invoice_refs`); если Σrefs < amount —
  Cr `unapplied` = base(amount) − Σbase(refs), `invoice_id = null`.
- **Возврат:** Dr `unapplied`, Cr `bank` на base(|amount|), `invoice_refs = []`.
- **Прочие операции** (расходы, комиссии): Dr `expense*`, Cr `bank`; `invoice_id` и
  `payment_id` пустые. Нужны для объёма книги 200–2 000 строк.
- Курсовые разницы по расчётам не начисляются: единица сверки — документ, а не сальдо AR.

## 5. Типы расхождений и заявляемые суммы

Все суммы заявляются **в базовой валюте**, в минимальных единицах, как неотрицательное
целое. `affected_ids` — ровно `[doc_id]` затронутого документа. Канонизация ID: `strip()`,
`upper()`, удаление дублей, сортировка.

| `type` | Что сравниваем | `expected_amount_minor` | Допуск |
|---|---|---|---|
| `duplicate_invoice` | Счёт в invoices.jsonl ↔ проводки книги с его `invoice_id` без `payment_id`: должна быть одна, найдено две. | Завышение: Dr `ar` лишней проводки. | 0 |
| `payment_allocation` | Платёж (`invoice_refs`) ↔ строки Cr `ar` проводки платежа. Распределение пропущено полностью или частично; недостающая часть ушла на `unapplied`. | Нераспределённая часть по записанным значениям книги: Dr `bank` платежа − Σ Cr `ar` строк платежа = Σ Cr `unapplied` строк платежа. Ошибка вносится только в платежи с Σrefs = amount (законного аванса нет). | 0 |
| `fx_conversion` | Документ в иностранной валюте ↔ его проводка: курс взят не на ту дату (UTC-дата вместо локальной, соседний день) или неверный. | `|записано − ожидается|` по Dr `ar` (счёт) или Dr `bank` (платёж), где ожидается = конвертация по policy. | 1 |
| `wrong_period` | Счёт: локальная дата `issued_at` ↔ `timestamp` его проводки. Проводка отнесена к другому периоду (`[period_start, period_end)`), в т. ч. из-за UTC-даты. | Сумма Dr `ar` неверно отнесённой проводки. | 0 |

Ошибки затрагивают только документы, событие или проводка которых лежит в периоде.
Ошибка `wrong_period` всегда пересекает границу периода; смещение внутри периода
не генерируется.

### Допустимые наборы доказательств (`evidence_sets`)

Вывод засчитывается, если `evidence_ids` содержит **все** ID хотя бы одного набора; лишние
ID допустимы, если существуют и общее число ≤ 10. Несуществующий ID делает вывод неверным.

| `type` | Наборы |
|---|---|
| `duplicate_invoice` | `[doc, ar_row(1), ar_row(2)]` или `[doc, revenue_row(1), revenue_row(2)]` |
| `payment_allocation` | `[doc, unapplied_row…]` или `[doc, bank_row, ar_rows_of_payment…]` (если ≤10) |
| `fx_conversion` | `[doc, ar_row|bank_row, FX-ключ правильной даты]` или с `revenue_row` вместо `ar_row` |
| `wrong_period` | `[doc, ar_row]` или `[doc, revenue_row]` |

## 6. Закрытые файлы

### truth.json
```json
{"schema_version": "1.0", "episode_id": "EP-…", "base_currency": "KZT",
 "issues": [{"issue_id": "ISS-…", "type": "fx_conversion", "affected_ids": ["DOC-…"],
             "expected_amount_minor": 1234, "currency": "KZT", "tolerance_minor": 1,
             "evidence_sets": [["DOC-…", "R-…", "FX:2026-03-31:EUR:KZT"]]}]}
```
Чистый эпизод — `"issues": []`. Порядок `issues` — по `issue_id`.

### provenance.json
`{"schema_version","episode_id","generator_version","config","config_sha256","seed","baseline_sha256":{файл: sha256 до ошибок},"changes":[{"issue_id","type","order","op","target","before","after","cause"}],"distractors":[…]}`
— `op ∈ {add_row, modify_row, delete_row}`, `before`/`after` — полные строки (или `null`).
Обратное применение `changes` в обратном `order` восстанавливает эталонную книгу.
`baseline_sha256["ledger.csv"]` — sha256 эталонной книги в **каноническом виде** (формат
§2, строки по `(timestamp, row_id)`), поэтому он определён и для переставленной книги;
остальные файлы ошибками не меняются, их baseline-хеш равен опубликованному.

### id_map.json (парные эпизоды, A18)
`{"source_episode_id","episode_id","ids":{"старый ID":"новый ID"}}`.
Парный эпизод (`python3.11 -m fingen pair`, `fingen/transform.py`): все непрозрачные ID
(`row_id`, `entry_id`, `doc_id`, `invoice_id`, `payment_id`, `entity_id`, `line_id`,
`issue_id`, `episode_id`) заменены биекцией на свежие ID другого стиля (префикс/разделитель)
во всех открытых и закрытых файлах; FX-ключи не меняются. Строки `ledger.csv`, `fx.csv` и
документы JSONL переставлены seed-ом преобразования; порядок `line_items` и `invoice_refs`
сохранён. Суммы, валюты и время не меняются. `ids` содержит и `episode_id`. В
`provenance.config.transform` записано `{kind:"pair", seed, permute, rename}`
(`config_sha256` пересчитан); старые ID упоминает только `id_map.json`. Ответ oracle исходного эпизода, переименованный
через `ids`, обязан давать на паре ту же награду.

## 7. Формат ответа агента (сторона Алимжана, для справки)

```json
{"episode_id": "EP-…", "findings": [{"type": "duplicate_invoice", "affected_ids": ["DOC-…"],
  "claimed_amount_minor": 50000, "currency": "KZT", "evidence_ids": ["DOC-…", "R-…", "R-…"],
  "explanation": "…"}]}
```
Совпадение с эталоном (TP): тот же `type`, канонические `affected_ids` равны, та же
`currency`, `|claimed − expected| ≤ tolerance_minor`, выполнено правило наборов
доказательств. Сопоставление один-к-одному; несопоставленные и повторные выводы — FP.

## 8. Конфигурация генератора (A03)

`configs/<split>.json` — список конфигураций; одна конфигурация:
```json
{"config_version": "1", "schema_version": "1.0", "seed": 1001, "split": "train",
 "size": {"invoices": 60, "background_entries": 120},
 "period": {"start_month": "2026-03", "months": 1}, "timezone": "Asia/Almaty",
 "base_currency": "KZT", "foreign_currencies": ["EUR", "USD"], "rounding_mode": "half_up",
 "errors": {"duplicate_invoice": 1, "payment_allocation": 1, "fx_conversion": 0, "wrong_period": 0},
 "combos": {"fx_partial_payment_boundary": 0, "duplicate_plus_period": 0},
 "distractors": {"split_payments": 2, "refunds": 1, "repeated_amounts": 2, "utc_boundary": 2, "advances": 1},
 "transform": null}
```
Отдельный `random.Random` на эпизод, подпотоки — от хеша `(seed, имя компонента)`.
Одинаковые конфигурация и seed → побайтно одинаковые файлы (без времени запуска и путей).
