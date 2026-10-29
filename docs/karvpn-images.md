# Образы Bedolaga для KarVPN

GitHub Actions (`Build and Publish Docker Image`) собирает `linux/amd64` на
`ubuntu-24.04` и публикует приватный `ghcr.io/karvpn/remnawave-bedolaga-telegram-bot`.
VPS и локальный Mac в сборке не участвуют. Push в `main` обновляет `:main` и
`:sha-<полный SHA>`; pull request только собирает и проверяет образ.
Для обычной пересборки: Actions → Build and Publish Docker Image → Run workflow → main.
Сборка ветки вручную публикует SHA-тег, но не меняет `:main`.

## Версии и выпуск KarVPN

Единственный источник версии нашего образа — `karvpn-image-release.json`:
`upstream_version: 5.0.0` и `fork_revision: 1` дают `5.0.0-karvpn.1`.
Python-версия приложения в `pyproject.toml` остаётся upstream `5.0.0`.
За нашими изменениями следуют `5.0.0-karvpn.2`, `.3` и т. д.; обычный `fix`,
`feat` или breaking change не повышает базовую версию. При реальном обновлении
upstream измените `upstream_version` вслед за `pyproject.toml` и начните ревизию с 1.
`VERSION` и OCI labels Docker-образа содержат полную версию KarVPN.

В боте и кабинете действует единая схема:
**слияние изменений → автоматический релизный PR → его слияние → проверки,
публикация образа и GitHub Release → отдельное обновление VPS через Ansible**.
Полная инструкция для обоих приложений и плейбуков:
[релизы и обновление Bedolaga](https://github.com/KarVPN/vpn-infrastructure/blob/main/docs/BEDOLAGA_RELEASE_AND_DEPLOY.md).

`KarVPN Release` запускается на push в main. Job `release-pr` создаёт или обновляет
один PR `chore(main): release 5.0.0-karvpn.N`; новые изменения до его слияния
добавляются в тот же PR и не увеличивают номер ещё раз. Автоматизация меняет
`fork_revision` и `docs/releases/<версия>.md`, оставляя upstream-версию Python нетронутой.
Следующий номер считается по последнему опубликованному релизу KarVPN; при
обновлении основы счётчик начинается с 1. Первый выпуск использует подготовленные
заметки [5.0.0-karvpn.1](releases/5.0.0-karvpn.1.md), дополняя их новыми изменениями.
Последующие notes берутся только из новых изменений main после предыдущего тега;
импортированная история upstream и старый CHANGELOG в них не разворачиваются.

1. Слейте обычный PR в main и дождитесь автоматического релизного PR.
2. Проверьте его версию, короткие заметки и проверки. При запросе GitHub нажмите
   **Approve workflows to run**: PR создаётся встроенным GITHUB_TOKEN.
3. Слейте релизный PR. Автоматизация собирает **именно его merge-коммит**,
   публикует проверенный образ с тегом `v<версия>`, затем создаёт GitHub Release
   с notes и неизменяемым digest. Для нового релиза не нужны ручной тег,
   дополнительный PAT или переключатель publish.
4. После публикации в том же workflow проверяется, появились ли новые изменения
   main, пока шёл выпуск. Один релизный коммит без новых изменений следующий PR
   не создаёт. Повторный запуск не перезаписывает уже опубликованный релиз.

При сбое откройте Actions → KarVPN Release → Run workflow → main: незавершённый
выпуск из уже влитого релизного PR будет повторён с тем же коммитом и номером.
Если незавершённого выпуска нет, запуск только обновит следующий релизный PR.
`python3 scripts/karvpn_release_pr.py propose --dry-run` показывает план без записи
в GitHub (нужен доступ gh для чтения). Ветки релизов и служебную HTML-метку в
описании PR не переименовывайте: они связывают проверенные файлы с публикацией.

`karvpn-release-config.json` задаёт компонент и границу первой автоматической
выборки. `.release-please-manifest.json` и `release-please-config.json` сохранены
для синхронизации с upstream и выпуском бота не управляют. Унаследованные
release-please, Release PR Guard и Docker Hub остаются отключёнными.
Релиз вызывает общую сборку через workflow_call; обычные main/SHA-образы продолжают
собираться отдельно. Релизная сборка не передвигает плавающий тег main назад.

Dockerfile копирует явный список исходников и публичных ресурсов. `.env`, данные,
дампы и ключи исключены через `.dockerignore`. Серверные секреты, Ansible Vault,
Telegram-токен и пароли баз workflow не получает. `GITHUB_TOKEN` используется
для входа в GHCR после сборки и проверки, не передаётся Docker build.
Smoke test запускается без сети и без lifespan приложения: проверяет Python-код,
профиль KarVPN и `/health`, не включает polling, планировщики или базы.

Профиль сборки переносит прежние Ansible-патчи срока Telegram Mini App initData
в образ. Публичная Actions Variable `TELEGRAM_AUTH_MAX_AGE_DAYS` по умолчанию
равна `3650`; при изменении исходных мест проверки сборка останавливается.
Widget auth не меняется. Labels `io.karvpn.bedolaga.deploy-contract=1` и
`io.karvpn.bedolaga.telegram-auth-max-age-days` позволяют плейбуку проверить,
что образ соответствует настройкам inventory.

Ansible из `vpn-infrastructure` скачивает тег, получает digest именно этого
скачивания и записывает `image: ...@sha256:...` в отдельный Compose override.
Требуется Docker Compose 2.24.4+ (`!reset` / `!override`). Существующие Compose,
PostgreSQL, Redis, имена volumes и HA overrides сохраняются; приложение запускается
с `--no-build --no-deps --pull never`. Обновление образа не является миграцией
PostgreSQL на 18 из нового upstream Compose. Для возврата кода укажите предыдущий
digest в inventory; совместимость схемы БД при откате проверяется отдельно.

Доступ VPS к приватному GHCR берётся из общего Ansible Vault (`read:packages`).
Токен действует только на время скачивания и затем удаляется с VPS вместе с
временным Docker config. В образ и его Compose-файл он не записывается.
Необходимые секреты приложения передаются на сервер в `.env` с правами `0600`.
