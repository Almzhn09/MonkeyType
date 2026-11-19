# Согласованный контракт интеграции

Форматы генератора и оценщика совпадают с [контрактом v1.0](../docs/CONTRACT.md). Для автоматической проверки используйте `python3 scripts/check.py`. Документация действий API, изоляции и награды находится в [описании устройства среды](../docs/DESIGN.md).

Полный контрольный эпизод A08 с дублем счёта и ошибкой распределения прогоняется командой `python3 -m audit_sim run fincheck/fixtures/a08 --policy reconcile`. Пример корректного JSON-ответа находится в [control_submission.json](../fincheck/fixtures/a08/control_submission.json).
