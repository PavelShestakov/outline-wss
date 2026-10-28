# outline-wss — Shadowsocks over WebSocket + Cloudflare Tunnel + REST API

Неофициальный проект для установки `outline-ss-server` с WebSocket TCP/UDP и собственным API управления ключами, совместимым с основными маршрутами Outline Shadowbox. Устанавливается в Docker. Не является официальным продуктом Outline или Cloudflare.

> **Статус:** предварительная версия для тестирования на **чистом VPS**. Локальные unit/API-тесты пройдены; полный сценарий установки Docker и смены туннеля на удалённом VPS не проверялся. **Не запускайте поверх действующей ноды без плана миграции.**

## Компоненты

| Контейнер | Что делает | Публичные порты |
|---|---|---|
| `node` | `outline-ss-server` v1.9.2, FastAPI, YAML-выдача, SQLite | нет |
| `cloudflared` | Постоянный исходящий Cloudflare Tunnel | нет |
| `api-tls` | Nginx, HTTPS, ограничение по IP контроллера | указанный API TCP-порт |

Внутри сети Docker:

- Outline WebSocket: `http://node:<LOCAL_WSS_PORT>` (значение установщика, например `636`).
- Выдача динамических YAML: `http://node:18081`.
- FastAPI: `http://node:18443`, напрямую наружу не опубликован.
- Prometheus: `127.0.0.1:9091` **только внутри контейнера `node`**.

Клиентские WSS URL используют `wss://<WSS_HOST>/<secret>/tcp` и `/udp`. Публичный порт для Cloudflare WSS — 443 независимо от внутреннего порта Outline.

## Требования

- Чистый VPS с Ubuntu 22.04/24.04/26.04 или поддерживаемым Debian, amd64/arm64, права root, доступ в Интернет.
- Cloudflare-аккаунт, делегированный домен, **созданный remotely-managed Tunnel** и его токен.
- Свободный **внешний TCP порт API**, например 18443. Не требуется открывать порты Outline/keys.
- Адрес управляющего сервера IPv4 (будет единственным разрешённым адресом для доступа к REST API).
- Клиент Outline с поддержкой WebSocket (проверять совместимость конкретной ОС и версии).

## Быстрая установка

Сначала создайте публичный репозиторий с именем `outline-wss` и опубликуйте файлы из этого архива. **Один раз замените** `YOUR_GITHUB_USERNAME/outline-wss` в `install.sh` на фактический путь репозитория.

Для установки из опубликованного репозитория:

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/<GITHUB_USERNAME>/outline-wss/main/install.sh)"
```

`<GITHUB_USERNAME>` нужно заменить на владельца реального репозитория. Нельзя запускать команду выше, пока репозиторий не опубликован.

**Безопаснее предварительно проверить код:**

```bash
curl -fsSLo install.sh https://raw.githubusercontent.com/<GITHUB_USERNAME>/outline-wss/main/install.sh
less install.sh
sudo bash install.sh
```

Из распакованного архива (до публикации):

```bash
cd outline-wss
sudo bash install.sh
```

Интерактивный установщик запросит:

1. Домен WSS (например `srv.example.org`).
2. Домен конфигураций (например `conn.example.org`).
3. Внутренний порт WebSocket (по умолчанию 636).
4. Внешний TCP-порт HTTPS API (по умолчанию 18443).
5. Публичный IPv4 или DNS-имя для HTTPS-сертификата API.
6. Разрешённый IPv4 управляющего сервера (можно `/32`).
7. Папку установки (по умолчанию `/opt/outline-wss-docker`).
8. Токен Cloudflare Tunnel (ввод скрыт).

Docker Engine / Compose при отсутствии устанавливаются из **официального APT-репозитория Docker**. На сервере с уже работающим Docker установщик не удаляет конфликтующие пакеты и не переписывает существующие контейнеры.

## Настройка Cloudflare после установки

В Cloudflare Dashboard → Zero Trust → Networks → Connectors → Cloudflare Tunnels → выбранный Tunnel → Published applications / Public Hostnames добавьте:

| Hostname | Service type | Service URL |
|---|---|---|
| `srv.example.org` | HTTP | `http://node:636` (или выбранный WSS-порт) |
| `conn.example.org` | HTTP | `http://node:18081` |

**Важно:** внутри Docker контейнера `cloudflared` адрес `127.0.0.1` означает сам контейнер Cloudflare, а не `node`; маршруты обязаны использовать имя сервиса `node`.

Не включайте Cloudflare Access (login) / CAPTCHA / JS Challenge на этих маршрутах: Outline Client не является браузером. Если загрузка YAML выдаёт 403, проверьте Security Events и точечно исключите блокирующее правило для `conn`, а не всей зоны.

## API и защищённый HTTPS

Процесс `node` работает в контейнере с UID/GID `10001` и без Docker socket. Данные на хосте принадлежат UID/GID `10001` (резервное копирование — с root-правами).

Установщик генерирует самоподписанный сертификат с SAN для указанного IP/DNS. API имеет **три независимых защиты**:

- TLS — защита канала; клиент сверяет SHA-256 отпечаток сертификата.
- Nginx разрешает запросы только от указанного IPv4/подсети управляющего сервера; остальные получают 403.
- API отвечает только под непредсказуемым `/m/<SECRET>/...`; остальные адреса дают 404.

API URL и компактный отпечаток (в base64url) сохраняются в `/opt/outline-wss-docker/config/admin-url.txt` (режим `0600`). Этот адрес **секретен**: не помещайте в GitHub, issue или скриншоты. Он выглядит так:

```text
https://<NODE_IP>:<API_PORT>/m/<SECRET>#fp=<BASE64URL_SHA256>
```

Поля `#fp=...` при HTTP-запросе на сервер не передаются. Они используются обновлённым клиентом `client/outline_vpn/outline_vpn.py`, который **проверяет pin** и отклоняет другой сертификат, даже если сертификат самоподписан. Старый Shadowbox API без отпечатка остаётся совместимым с библиотекой, но для новых нод обязательно используйте pin.

При использовании стандартного `requests` вместо обновлённой библиотеки нужно доверять сертификату `config/tls/server.crt` на управляющем хосте и указывать `verify=/path/to/server.crt`. **Не используйте `verify=False` без отдельной проверки отпечатка.**

Не открывайте API через Cloudflare Tunnel и не публикуйте FastAPI напрямую. Защита по IP работает только если Nginx видит исходный IPv4 клиента — проверьте при сетевом NAT/проксировании. Если контроллер выходит через другой адрес, скорректируйте `allow` в `config/nginx.conf` и примените `docker compose restart api-tls`.

### Вызовы

API повторяет основные маршруты Shadowbox:

```text
GET    /m/<secret>/health
GET    /m/<secret>/access-keys/
POST   /m/<secret>/access-keys/                  {"port":636}
DELETE /m/<secret>/access-keys/<id>
PUT    /m/<secret>/access-keys/<id>/name
PUT    /m/<secret>/access-keys/<id>/data-limit   {"limit":{"bytes":0}}
DELETE /m/<secret>/access-keys/<id>/data-limit
GET    /m/<secret>/metrics/transfer
```

Существующий Python-клиент возвращает `OutlineKey` c `key_id`, `password`, `method`, `port`, `access_url`, `used_bytes`, `data_limit`.

Динамический ключ:

```text
ssconf://conn.example.org/<random-secret>.yml#Outline-WSS
```

## Управление

```bash
cd /opt/outline-wss-docker
sudo docker compose ps
sudo docker compose logs --tail=80 node
sudo docker compose logs --tail=80 cloudflared
sudo docker compose logs --tail=80 api-tls
sudo docker compose restart node
```

**Резервное копирование:** `data/` (в том числе `data/manager/state.db`, SQLite WAL-файлы, `config.yaml`, `manager/base.yaml`, `keys/`), `config/` (сертификат и секреты), `.env`. Для консистентного бэкапа SQLite остановите `node` или используйте SQLite backup API. Не выгружайте эти каталоги в Git.

**Обновления:** сперва сохраните резервную копию конфигурации и базы, протестируйте миграцию, потом изменяйте код и пересобирайте образ через `docker compose up -d --build`. Установщик намеренно не перезаписывает уже созданный `.env` и не является системой автообновлений.

## Границы и ограничения

- **Это не полноценный Shadowbox.** Реализованы основные маршруты управления пользовательскими ключами; часть административных операций возвращает ограничение 409.
- Счётчики трафика собираются с периодом 5 с; лимит не является аппаратным побайтовым отсечением. Возможен небольшой перерасход. Уже установленные VPN-соединения могут не разорваться мгновенно; после лимита `0` новые соединения отклоняются.
- Динамические ссылки являются секретами — их обладатель получает доступ к VPN. Удаление ключа удаляет индивидуальный YAML. При лимите `0` YAML остаётся доступным, но пароль исключён из активной конфигурации.
- Публичная выдача ключей **не имеет логина**, потому что Outline Client не умеет передавать авторизацию при загрузке динамической конфигурации. Используются длинные случайные идентификаторы и HTTPS.
- Сертификат API самоподписанный и срок его действия ограничен. Ротация сертификата требует обновления pin на управляющем сервере.
- **Установщик не импортирует старые действующие ключи**, не удаляет существующие systemd-службы, не переносит прежние `cloudflared` настройки. Если привязать второй коннектор к старому туннелю, трафик может распределяться между несовместимыми нодами. Для теста создайте отдельный туннель/домены или спланируйте миграцию без потери ключей.
- До публичного production-использования требуются Docker-интеграционные тесты на чистой ноде и проверка учёта лимитов под нагрузкой.

## Локальные тесты

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/pytest -q tests
bash -n install.sh
```

## Публикация на GitHub

Создайте новый пустой **Public** репозиторий `outline-wss` (не добавляйте README/license через интерфейс, чтобы не получить конфликт). Из локального каталога:

```bash
git init -b main
git add .
git commit -m "Initial outline-wss Docker distribution"
git remote add origin git@github.com:<GITHUB_USERNAME>/outline-wss.git
git push -u origin main
```

**Перед публикацией** замените в `install.sh` `YOUR_GITHUB_USERNAME/outline-wss` на свой путь, проверьте `git status` и `git ls-files`, убедитесь, что в Git отсутствуют `.env`, `data/`, `config/`, токены, пароли и действующие YAML. В репозитории нет настроек конкретной VPN-ноды.

Сторонние компоненты: `outline-ss-server` (OutlineFoundation), `cloudflared` (Cloudflare), Nginx. Их собственные лицензии и условия применяются отдельно. Перед лицензированием этого репозитория проверьте права на включённую пользовательскую Python-библиотеку.
