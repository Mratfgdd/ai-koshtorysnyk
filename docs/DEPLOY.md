# Деплой за 3 хвилини: Render (бекенд) + Vercel (фронтенд)

Обидва сервіси безкоштовні. Результат — публічний URL, який можна дати клієнту.

---

## 0. Підготовка (один раз, локально)

Репозиторій має містити скомпільовані правила і прайс — без них деплой підніметься порожнім.

```powershell
$py = "C:\Users\user\AppData\Local\Programs\Python\Python312\python.exe"
$tpl = "шлях\до\Шаблон для ШІ.xlsx"

& $py scripts\compile_template.py $tpl   # -> backend/app/data/rules/template_layout.json
& $py scripts\import_catalog.py   $tpl   # -> локальна БД
& $py scripts\make_seed.py               # -> backend/app/data/seed/catalog.json

git add -A
git commit -m "Deploy config, rule pack and catalog seed"
git push
```

> `.env` у `.gitignore` — ключ у репозиторій не потрапляє. На Render він вводиться руками.

---

## 1. Backend на Render (~90 секунд)

1. <https://dashboard.render.com> → **Get Started for Free** → **GitHub** → авторизуйте Render.
2. **New +** → **Blueprint**.
3. Оберіть цей репозиторій → Render знайде `render.yaml` → **Apply**.
4. Він спитає значення для `ANTHROPIC_API_KEY` (позначений `sync: false`) — вставте ключ → **Apply**.
5. Чекайте на статус **Live** (перший білд ~3–5 хв).

Ваш URL: `https://koshtorysnyk-api.onrender.com` (точну адресу видно вгорі сторінки сервісу).

Перевірка:

```
https://<ваш-сервіс>.onrender.com/api/health
```

Має повернути `"catalog_items": 844`, `"quantity_rules": 233`, `"ai_available": true`.

**Що вже налаштовано в `render.yaml`:** порт через `$PORT`, `--host 0.0.0.0`,
health-check, регіон Frankfurt, диск 1 ГБ для БД та завантажень, `AI_MAX_CONCURRENCY=4`
і `MAX_UPLOAD_MB=80` під безкоштовний тариф.

---

## 2. Frontend на Vercel (~60 секунд)

1. <https://vercel.com/new> → **Continue with GitHub**.
2. **Import** цей репозиторій.
3. **Root Directory** → **Edit** → оберіть `frontend`. *(Обов'язково — інакше Vercel не знайде `package.json`.)*
4. Розгорніть **Environment Variables** і додайте:

   | Name | Value |
   |---|---|
   | `VITE_API_BASE_URL` | `https://<ваш-сервіс>.onrender.com` |

   Без слеша в кінці.
5. **Deploy**.

Ваш публічний URL: `https://<проєкт>.vercel.app` — це те, що даєте клієнту.

CORS уже дозволяє будь-який `*.vercel.app`, тому preview-збірки працюють без змін на бекенді.

---

## 3. Перевірка

Відкрийте Vercel-URL. На дашборді має бути `Позицій у базі: 844`, `Правил кількостей: 233`,
`AI: доступний`. Якщо порожньо — відкрийте DevTools → Network і подивіться, куди йдуть
запити `/api/...`: майже завжди це неправильний `VITE_API_BASE_URL`.

---

## Особливості безкоштовних тарифів

| Що | Наслідок |
|---|---|
| Render присипляє сервіс після ~15 хв простою | Перший запит після паузи чекає ~50 с. Далі — нормально. |
| 512 МБ RAM, слабкий CPU | `AI_MAX_CONCURRENCY=4`; аналіз 50-сторінкового комплекту йде довше, ніж локально. |
| Ліміт розміру запиту | `MAX_UPLOAD_MB=80`. Комплекти на 150 МБ треба стискати або піднімати платний тариф. |
| Диск 1 ГБ | Вистачає на БД і кілька десятків PDF. Без диска дані зникають при кожному redeploy. |
| Немає автентифікації | URL знає тільки той, кому ви його дали. Для реального продакшену додайте вхід за паролем. |

Щоб прибрати засинання — тариф Render **Starter** ($7/міс).

---

## Оновлення після зміни шаблону

```powershell
& $py scripts\compile_template.py $tpl
& $py scripts\import_catalog.py   $tpl
& $py scripts\make_seed.py
git commit -am "Оновлено прайс і правила"; git push
```

Render і Vercel перезберуться самі (`autoDeploy: true`).

> Сид завантажується **лише в порожню базу**. Щоб оновити ціни на вже запущеному
> Render, зайдіть у **Shell** сервісу і виконайте `python scripts/import_catalog.py`
> з новим файлом, або видаліть диск і передеплойте.

---

## Альтернатива: один сервіс замість двох

Бекенд уміє віддавати зібраний фронтенд сам. Якщо не потрібен окремий Vercel:

```yaml
buildCommand: pip install -r requirements.txt && cd ../frontend && npm ci && npm run build
```

Тоді `VITE_API_BASE_URL` не потрібен, а UI відкривається на Render-URL. Мінус —
довший білд і повільніша віддача статики.
