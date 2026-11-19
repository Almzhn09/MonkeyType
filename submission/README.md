# Комплект сдачи Genesis

[Архив genesis-submission.zip](genesis-submission.zip) содержит исходники, лицензию, Dockerfile, инструкции, 10 train- и 10 dev-эпизодов, 20 парных вариантов, результаты проверки и обучающие траектории. Список проверок и измерения находятся в [validation.json](validation.json).

После распаковки команда `python3 scripts/check.py` выполняет проверку. Для пересборки содержимого архива используйте `python3 scripts/check.py --output .audit-sim/submission` из корня репозитория.

[Сценарий выступления и текст для формы](../docs/DEMO.md). Техническое демо запускается через `python3 -m audit_sim demo`. Контрольный эпизод с двумя ошибками на контракте v1.0 можно отдельно показать командой `python3 -m audit_sim run fincheck/fixtures/a08 --policy reconcile`. Официальная отправка выполняется через канал сдачи организаторов.
