# Комплект сдачи Genesis

[Архив genesis-submission.zip](genesis-submission.zip) содержит исходники, лицензию, Dockerfile, инструкции, 10 train- и 10 dev-эпизодов, 20 парных вариантов, результаты проверки и обучающие траектории. В архив также входят игровой режим (`playground/`), панель проверяющего (`dashboard/`) и файлы для публичного размещения (`webapp/`, `deploy/`, `render.yaml`). Архив собран 21 ноября командой `python3 scripts/check.py --output` и проходит эту же проверку после распаковки. Список проверок и измерения находятся в [validation.json](validation.json).

Онлайн-версия: https://monkeytype-audit.onrender.com (панель проверяющего — `/dashboard/`). Проверка работает на Linux, macOS и Windows.

После распаковки команда `python3 scripts/check.py` выполняет проверку. Для пересборки содержимого архива используйте `python3 scripts/check.py --output .audit-sim/submission` из корня репозитория.

[Сценарий выступления и текст для формы](../docs/DEMO.md). Техническое демо запускается через `python3 -m audit_sim demo`. Контрольный эпизод с двумя ошибками на контракте v1.0 можно отдельно показать командой `python3 -m audit_sim run fincheck/fixtures/a08 --policy reconcile`. Официальная отправка выполняется через канал сдачи организаторов.
