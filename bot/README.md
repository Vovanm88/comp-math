## PR benchmark bot

Бот:

- проверяет открытые PR в GitHub репозитории
- находит последний успешный GitHub Actions run для PR
- скачивает artifact с бинарником
- запускает проверку **в Docker** (без сети, read-only rootfs, ограничения CPU/RAM/PIDs)
- пишет результат в комментарий PR
- ведёт локальный лидерборд в JSON

### Быстрый старт

1) Скопировать конфиг:

```bash
cp bot/config.example.yaml bot/config.yaml
```

2) Экспортировать токен:

```bash
export GITHUB_TOKEN=...
```

Токену нужны права минимум на чтение actions artifacts и запись комментариев:
- `repo` (для приватных) или `public_repo` + `issues:write` (для публичных), зависит от модели доступа.

3) Собрать docker-образ раннера:

```bash
docker build -t comp-math-runner:latest bot/docker
```

4) Поставить зависимости бота:

```bash
python -m venv bot/.venv
source bot/.venv/bin/activate
pip install -r bot/requirements.txt
```

5) Запустить:

```bash
python bot/bot.py --config bot/config.yaml
```

### Закрытые тесты/бенчмарк

Если в `bot/config.yaml` указать `tests.secret_tests_dir`, бот примонтирует эту папку как `/tests/secret` в контейнер.

Опционально можно добавить файл `bench.yaml` в `secret_tests_dir`:

```yaml
cases:
  - name: small
    type: file
    path: /tests/secret/cases/small.in
    repeats: 3
  - name: gen1
    type: generator
    command: ["python", "/tests/secret/generators/gen.py", "--n", "1000"]
    repeats: 2
```

