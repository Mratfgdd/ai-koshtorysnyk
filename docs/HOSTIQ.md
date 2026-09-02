# Розгортання на HostIQ (cPanel)

Усе, що можна підготувати без доступу до сервера, вже готове й перевірено
локально. Нижче — те, що лишилось зробити на самому хостингу.

---

## Спершу — чого бракує для автоматичного деплою

Ви дали логін і пароль, але **не дали, куди підключатися**:

| Потрібно | Навіщо |
|---|---|
| Хост SSH або IP (`srvNN.hostiq.ua`) | `uandrij@gmail.com` — це логін cPanel, а не адреса сервера |
| Порт SSH | HostIQ на shared зазвичай не 22 |
| Логін cPanel | Часто не збігається з email (щось на кшталт `uandrij` або `ua12345`) |
| Домен(и) | Куди ставити фронтенд і бекенд |
| Чи увімкнено SSH | На shared-тарифах його часто немає взагалі — тоді все робиться через cPanel UI |

---

## Головне обмеження, яке треба знати заздалегідь

**cPanel «Setup Python App» запускає Passenger у режимі WSGI. FastAPI — ASGI.**
Якщо віддати Passenger застосунок напряму, він не працюватиме — не «частково»,
а взагалі, з першого ж запиту.

Тому в репозиторії є `backend/passenger_wsgi.py`, який:

1. перезаходить у virtualenv, який створив cPanel;
2. підключає міст `a2wsgi.ASGIMiddleware` (ASGI → WSGI);
3. **викликає `bootstrap()` вручну** — у WSGI немає lifespan, тому без цього
   таблиці не створюються і кожен запит падає з
   `no such table: catalog_items`.

Перевірено локально через `wsgiref` (те саме, що робить Passenger):

```
loaded: ASGIMiddleware (WSGI callable)
health     : 200 {'catalog_items': 844, 'quantity_rules': 233}
create     : 201
delete 204 : 204, body len 0
upload     : multipart розібрано
```

---

## Крок 1. Бекенд через cPanel → Setup Python App

1. **cPanel → Setup Python App → Create Application**
   - Python version: **3.11** або **3.12**
   - Application root: `koshtorysnyk` (тобто `~/koshtorysnyk`)
   - Application URL: оберіть **субдомен** `api.вашдомен` — так найпростіше
   - Application startup file: `passenger_wsgi.py`
   - Application Entry point: `application`

2. Залийте вміст папки `backend/` у `~/koshtorysnyk/` (Git або File Manager).

3. **Environment variables** у тій самій формі cPanel — скопіюйте з
   `deploy/hostiq/.env.production.example`. Обов'язково:

   ```
   ANTHROPIC_API_KEY   = <ваш ключ>
   CORS_ORIGINS        = https://вашдомен,https://www.вашдомен
   CORS_ALLOW_VERCEL   = false
   MAX_UPLOAD_MB       = 150
   AI_MAX_CONCURRENCY  = 2
   DATA_DIR            = ./data
   DATABASE_URL        = sqlite:///./data/estimator.db
   ```

4. **Run Pip Install** із `requirements.txt`, потім окремо:

   ```
   pip install a2wsgi
   ```

   (`a2wsgi` потрібен лише для Passenger, тому його немає в основному файлі.)

5. **Restart** застосунку. Перевірка:

   ```
   https://api.вашдомен/api/health
   ```

   Має повернути `catalog_items: 844`, `quantity_rules: 233`.

> **Якщо ви змонтували застосунок не на субдомен, а на шлях** `вашдомен/api`,
> то додайте `API_PREFIX=` (порожнє значення). Інакше шляхи стануть
> `/api/api/health` і все віддаватиме 404.

---

## Крок 2. Фронтенд у public_html

Локально, підставивши свій домен бекенду:

```powershell
cd frontend
$env:VITE_API_BASE_URL = "https://api.вашдомен"
npm ci
npm run build
```

Залийте **вміст** `frontend/dist/` у `public_html/` (не саму папку `dist`).

Скопіюйте `deploy/hostiq/public_html.htaccess` у `public_html/.htaccess` —
без нього React Router віддає 404 на будь-якому оновленні сторінки
(`/projects/7` → 404).

Перевірено, що адреса бекенду реально вшивається у бандл:

```js
const oh = "https://api.example.com".replace(/\/$/, "");
```

---

## Крок 3. Ліміт завантаження 100 МБ+

Ліміт треба підняти **на кожному рівні** — інакше файл ріжеться раніше, ніж
дійде до застосунку, і щоразу з іншою незрозумілою помилкою.

| Рівень | Файл | Значення |
|---|---|---|
| LiteSpeed / Apache | `deploy/hostiq/backend.htaccess` → корінь бекенду | `LimitRequestBody 209715200` (200 МБ) |
| PHP-FPM / suPHP | `deploy/hostiq/user.ini` → `.user.ini` у корені бекенду | `upload_max_filesize`, `post_max_size` = 200M |
| Застосунок | env `MAX_UPLOAD_MB` | **150** |

Порядок важливий: ліміт застосунку має бути **нижчим** за серверний, щоб
користувач отримував зрозумілий 413 з поясненням, а не обірваний зв'язок.

Бекенд тепер відхиляє завеликий файл **до запису на диск** — за заголовком
`Content-Length`, а потім потоково з ранньою зупинкою. Раніше він писав файл
цілком і лише потім перевіряв розмір, що на shared-хостингу з квотою могло
забити диск. Закріплено тестом.

> Якщо HostIQ ставить жорсткий ліміт на рівні LiteSpeed, який не переписується
> з `.htaccess`, — це вирішується тільки через їхню підтримку.

---

## Крок 4. Перевірка

```bash
curl https://api.вашдомен/api/health
curl -X POST https://api.вашдомен/api/projects \
     -H "Content-Type: application/json" -d '{"name":"Тест"}'
curl -F "file=@креслення.pdf" \
     https://api.вашдомен/api/projects/1/documents
```

Далі відкрийте фронтенд, створіть проєкт, залийте PDF і натисніть «Аналізувати».

---

## Чесно про обмеження shared-хостингу

Цей застосунок для shared-тарифу важкий. Що варто перевірити на практиці:

| Ризик | Чому |
|---|---|
| **PyMuPDF** | Тягне нативні бібліотеки (~20 МБ wheel). Зазвичай встановлюється, але якщо ні — потрібен VPS. |
| **Пам'ять** | Рендер сторінки креслення 130 DPI + Pillow — це сотні МБ на пікових моментах. LVE-ліміт shared-тарифу (часто 1 ГБ) може вбити процес посеред аналізу. Тому в прикладі `PAGE_RENDER_DPI=110`. |
| **Паралелізм** | `AI_MAX_CONCURRENCY=2`, не 6: shared-тарифи обмежують кількість процесів і CPU. |
| **Тривалі запити** | Аналіз комплекту — хвилини. Passenger і LiteSpeed мають таймаути; довгий запит може обірватися. |
| **Passenger «засинає»** | Idle-таймаут вивантажує застосунок; наступний запит чекає перезапуск. |

**Моя рекомендація:** для 150-мегабайтних комплектів креслень і багатохвилинного
аналізу shared-хостинг — не те місце. VPS (навіть найдешевший) із чистим
`uvicorn` за Nginx буде і простішим, і швидшим, і без цих обмежень. Якщо
залишаєтесь на HostIQ — почніть із малого PDF і подивіться, чи витримує тариф.

---

## Якщо є SSH — автоматичний деплой

Коли надасте хост/порт/логін:

```bash
bash deploy/hostiq/deploy.sh
```

Скрипт збирає фронтенд, заливає обидві частини, ставить залежності,
розкладає `.htaccess` і перевіряє `/api/health`. Параметри — через env або
файл `deploy/hostiq/deploy.env`.
