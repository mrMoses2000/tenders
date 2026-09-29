# Резервная копия и восстановление закупочного бота

## Состав копии и ограничения

Рабочая PostgreSQL база общая с другим проектом (`transcription_bot`), данные
бота находятся только в схеме `procurement_bot`. Каждый комплект содержит:

- custom dump только схемы `procurement_bot`;
- `var/media` и `var/results`;
- WAHA session data только если оператор настроил
  `PROCUREMENT_WAHA_SESSION_DIR` на читаемый absolute path со снимком (в архиве
  — `waha-sessions/`).

Production использует Docker named volume `procurement-waha-sessions`; нативный
user unit `procurement-waha.service` и его каталог отсутствуют, а у `moses` нет
доступа к rootful Docker daemon. Поэтому **текущие автоматические копии
исключают WhatsApp-сессию**. Sidecar JSON явно записывает
`waha_sessions: excluded_not_configured`, и вывод бэкапа тоже сообщает об этом.
Не трактуйте успешный DB/assets backup как защищённую WhatsApp-сессию.

После того как оператор подготовит читаемый snapshot каталога Docker volume и
настроит `PROCUREMENT_WAHA_SESSION_DIR`, скрипт запишет
`waha_sessions: included_live`. Это копирование файлов работающей сессии, не
согласованный snapshot; целостность WhatsApp-сессии не гарантируется. Будьте
готовы повторно авторизовать WhatsApp. Архив содержит авторизационные данные;
доступ ограничен владельцем системы (`umask 077`, каталог `0700`).

SHA-256 dump и tar проверяются перед публикацией и сохранены в sidecar. Daily
timer запускает копирование в 03:15, `Persistent=true` запускает пропущенный
цикл после загрузки. Unit ограничивает локальный набор 14 полными комплектами
и запрещает старт, если свободно менее 512 MiB. Допустимые настройки:
`PROCUREMENT_BACKUP_MAX_SETS` от 2 до 90 и
`PROCUREMENT_BACKUP_MIN_FREE_BYTES` от 0 до 107374182400. Удаляются только
старые комплекты с sidecar и совпавшими SHA-256; всегда сохраняются как минимум
два последних корректных комплекта. Неполные или повреждённые файлы скрипт не
удаляет автоматически. Ошибка очистки делает timer service неуспешным и
попадает в его журнал.

Docker volume path можно узнать с правами оператора:

```bash
sudo docker volume inspect procurement-waha-sessions --format '{{.Mountpoint}}'
```

Production user service не имеет Docker permission; не добавляйте `moses` в
группу `docker` ради бэкапа. Оператор должен подготовить отдельный защищённый
read-only snapshot/export, доступный пользователю сервиса, и настроить
`PROCUREMENT_WAHA_SESSION_DIR` в systemd drop-in для
`procurement-db-backup.service`. До этого WAHA session не попадает в архив.

Код приложения, Docker image/runtime, `.env` и `var/secrets/waha.env` в этих
backup-файлах не содержатся. Для полной потери диска отдельно нужны проверенный
release приложения, WAHA image/runtime и защищённое восстановление секретов.
Не используйте этот runbook как полную процедуру rebuild чистого сервера.

Локальные копии не защищают от потери диска/сервера. Нужна отдельная
зашифрованная off-host копия с собственным сроком хранения. Ключ шифрования
хранить вне сервера и отдельно от архива.

## Обычная проверка

```bash
cd /home/moses/tenders
.venv/bin/python scripts/backup_procurement_db.py
systemctl --user status procurement-db-backup.timer
systemctl --user list-timers procurement-db-backup.timer
journalctl --user -u procurement-db-backup.service -n 100 --no-pager
ls -l var/backups/
```

Не печатайте содержимое WAHA session tar или `.env` в терминал общего доступа.
Проверяйте sidecar и хэши, не имена/содержимое файлов сессии:

```bash
cd /home/moses/tenders
python3 - <<'PY'
import json
from pathlib import Path
p = max(Path("var/backups").glob("procurement_bot-*.json"), key=lambda x: x.stat().st_mtime)
m = json.loads(p.read_text())
print({k: m.get(k) for k in ("created_at_utc", "waha_sessions", "max_backup_sets", "minimum_free_bytes")})
print("dump:", m["sha256"], "assets:", m["assets_sha256"])
PY
```

## Проверка restore в отдельной базе

Никогда не проверяйте restore в `transcription_bot`: там есть схема и данные
другого проекта. Выберите sidecar и соответствующие `.dump` и `.assets.tar.gz`,
сверьте имена и SHA-256 с sidecar, затем восстановите dump в отдельную пустую
одноразовую БД. Для текущего хоста с локальным PostgreSQL/peer auth:

```bash
cd /home/moses/tenders
DUMP=var/backups/procurement_bot-YYYYMMDDTHHMMSSZ-PID.dump
ASSETS=var/backups/procurement_bot-YYYYMMDDTHHMMSSZ-PID.assets.tar.gz
pg_restore --list "$DUMP" >/dev/null
sha256sum "$DUMP" "$ASSETS"
createdb procurement_restore_check
pg_restore --exit-on-error --no-owner --no-acl \
  --dbname=procurement_restore_check "$DUMP"
psql -X -d procurement_restore_check -Atqc \
  'SELECT count(*) FROM procurement_bot.procurement_cases'
psql -X -d procurement_restore_check -Atqc \
  'SELECT count(*) FROM procurement_bot.request_items'
psql -X -d procurement_restore_check -Atqc \
  'SELECT count(*) FROM procurement_bot.offers'
psql -X -d procurement_restore_check -Atqc \
  'SELECT count(*) FROM procurement_bot.supplier_conversations'
psql -X -d procurement_restore_check -Atqc \
  'SELECT count(*) FROM procurement_bot.delivery_runs'
```

The counts should be compared with the production snapshot time, not necessarily
with the current live database. Test-extract the tar into a private temporary
directory and inspect only top-level names:

```bash
umask 077
RESTORE_TMP=$(mktemp -d /tmp/procurement-restore-check.XXXXXX)
tar -tzf "$ASSETS" | cut -d/ -f1 | sort -u
tar -xzf "$ASSETS" -C "$RESTORE_TMP"
test -d "$RESTORE_TMP/media" && test -d "$RESTORE_TMP/results"
```

If the sidecar says `included_live`, `waha-sessions/` should also exist.
Remove the temporary data and disposable DB after review:

```bash
rm -rf -- "$RESTORE_TMP"
dropdb procurement_restore_check
```

## Восстановление на рабочем сервере

Это аварийная операция на общей PostgreSQL базе. Используйте только после
согласования остановки закупочного бота и проверки, что путь БД указывает на
`transcription_bot`. Не удаляйте и не восстанавливайте всю эту БД: она также
принадлежит проекту транскрибации.

1. Выберите комплект и запишите имена файлов и поле `waha_sessions` из
   sidecar. Убедитесь, что dump и assets совпадают по SHA-256. Если сессия
   `excluded_not_configured`, заранее планируйте QR-повторную авторизацию.
2. Сделайте и проверьте свежий аварийный комплект текущего состояния:
   `.venv/bin/python scripts/backup_procurement_db.py`. Сохраните вывод.
   Это позволяет вернуться к состоянию до restore.
3. Остановите только сервисы этого приложения:

   ```bash
   systemctl --user stop procurement-agent-bot.service \
     procurement-agent-worker.service procurement-waha-webhook.service \
     procurement-mini-app-tunnel.service procurement-mini-app.service
   sudo docker stop procurement-waha
   ```

4. Восстановите **только схему** закупочного бота в общей БД. Команда ниже
   предполагает, что локальный PostgreSQL принимает peer auth для пользователя
   `moses`. Если production DSN указывает другой host/user, выполните её от
   PostgreSQL-оператора с теми же credentials, не помещая пароль в аргументы.
   `--clean` здесь применяется только к объектам schema из выбранного dump:

   ```bash
   pg_restore --exit-on-error --clean --if-exists --no-owner --no-acl \
     --schema=procurement_bot --dbname=transcription_bot "$DUMP"
   ```

   Если restore завершился ошибкой, не запускайте worker. Восстановите свежий
   аварийный dump тем же способом и выясните причину.
5. Извлеките assets во временный каталог с приватными правами, затем замените
   каталоги приложения. Сначала убедитесь, что в tar есть `media/` и
   `results/`; не извлекайте архив от root и не используйте `--absolute-names`.

   ```bash
   umask 077
   RESTORE_TMP=$(mktemp -d /tmp/procurement-restore.XXXXXX)
   tar -xzf "$ASSETS" -C "$RESTORE_TMP"
   test -d "$RESTORE_TMP/media" && test -d "$RESTORE_TMP/results"
   rsync -a --delete "$RESTORE_TMP/media/" var/media/
   rsync -a --delete "$RESTORE_TMP/results/" var/results/
   ```

6. Восстановление WAHA-сессии — best effort, потому что `included_live` не
   гарантирует согласованность копии. Только при этом значении и наличии
   каталога в архиве замените данные Docker named volume. Команды требуют
   полномочий оператора Docker:

   ```bash
   if test -d "$RESTORE_TMP/waha-sessions"; then
     WAHA_VOLUME=$(sudo docker volume inspect procurement-waha-sessions --format '{{.Mountpoint}}')
     sudo rsync -a --delete "$RESTORE_TMP/waha-sessions/" "$WAHA_VOLUME/"
   fi
   rm -rf -- "$RESTORE_TMP"
   ```

   Если WAHA не поднимает `default` session, не повторяйте отправки из outbox.
   Верните предыдущий session directory из аварийной копии или пройдите
   штатную привязку WhatsApp через WAHA/QR, затем отдельно сверяйте ledger.
7. Запускайте WAHA, дождитесь готовности, затем webhook/worker/bot/Mini App и
   туннель. Проверьте миграции и здоровье приложения:

   ```bash
   sudo systemctl start docker.service
   sudo docker start procurement-waha
   sudo docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' procurement-waha
   systemctl --user start procurement-waha-webhook.service \
     procurement-agent-worker.service procurement-agent-bot.service \
     procurement-mini-app.service procurement-mini-app-tunnel.service
   systemctl --user --failed
   curl --fail --silent http://127.0.0.1:8082/api/health
   .venv/bin/procurement-bot health
   ```

   Убедитесь, что `procurement-bot migrate` не сообщает незапущенных
   миграций, а Telegram-кнопка ведёт на новый Quick Tunnel URL. Идентификатор
   URL меняется при пересоздании Quick Tunnel; старые прямые ссылки не
   восстановить. Не запускайте повторно outbox-сообщения, пока не сверили их
   с WAHA после восстановления.

## Проверка холодной загрузки после перезагрузки Ubuntu

Это ручная проверка в согласованное окно обслуживания. До перезагрузки:

```bash
loginctl show-user moses -p Linger
systemctl --user is-enabled procurement-agent-bot.service \
  procurement-agent-worker.service procurement-waha-webhook.service \
  procurement-mini-app.service \
  procurement-mini-app-tunnel.service procurement-db-backup.timer
sudo systemctl is-enabled docker.service
systemctl --user --failed
```

Выполните контролируемый reboot с консоли или администраторской SSH-сессии.
После возвращения сервера дождитесь пользовательского systemd manager и
проверьте:

```bash
loginctl show-user moses -p Linger
systemctl --user is-active procurement-agent-bot.service \
  procurement-agent-worker.service procurement-waha-webhook.service \
  procurement-mini-app.service \
  procurement-mini-app-tunnel.service
sudo systemctl is-active docker.service
sudo docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' procurement-waha
systemctl --user --failed
systemctl --user list-timers procurement-db-backup.timer
curl --fail --silent http://127.0.0.1:8082/api/health
.venv/bin/procurement-bot health
journalctl --user -u procurement-mini-app-tunnel.service -b -n 100 --no-pager
```

Затем откройте кнопку меню бота в Telegram и проверьте новую HTTPS-ссылку и
WAHA container/session state. Один только `is-active` не доказывает, что Mini App доступна
извне или что WhatsApp авторизован. До фактического cold boot этот критерий
остаётся непроверенным.
