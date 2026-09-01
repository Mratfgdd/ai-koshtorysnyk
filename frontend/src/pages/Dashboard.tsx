import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, Empty, Notice, Spinner, Stat, StatusTag, useAsync } from "../components";

export default function Dashboard() {
  const { data, error, loading } = useAsync(() => api.dashboard(), []);
  const health = useAsync(() => api.health(), []);

  if (loading) return <Spinner label="Завантаження…" />;
  if (error) return <Notice tone="error" title="Не вдалося завантажити дашборд">{error}</Notice>;
  if (!data) return null;

  const t = data.totals;
  const h = health.data;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Дашборд</h1>
          <div className="sub">Стан системи та останні проєкти</div>
        </div>
        <Link className="btn btn-primary" to="/projects">Новий проєкт</Link>
      </div>

      <div className="stack">
        {h && !h.ai_available && (
          <Notice tone="warn" title="AI-аналіз документів вимкнено">
            Не налаштовано <span className="mono">ANTHROPIC_API_KEY</span>. Каталог, правила
            шаблону, розрахунки, перевірка та експорт працюють; автоматичний аналіз креслень —
            ні. Система не вигадує дані замість аналізу: аналіз об'єкта можна заповнити вручну.
          </Notice>
        )}

        <div className="grid cols-4">
          <Stat label="Проєкти" value={t.projects} />
          <Stat label="Кошториси" value={t.estimates} />
          <Stat
            label="Відкриті питання"
            value={t.open_questions}
            tone={t.open_questions ? "alert" : undefined}
            hint="Потребують відповіді користувача"
          />
          <Stat
            label="Проблеми в кошторисах"
            value={t.open_issues}
            tone={t.open_issues ? "alert" : undefined}
            hint="Помилки та невизначеності"
          />
        </div>

        <div className="grid cols-2">
          <Card title="Каталог">
            <div className="grid cols-2">
              <Stat label="Позицій у базі" value={t.catalog_items} small />
              <Stat
                label="Без ціни реалізації"
                value={t.catalog_without_price}
                small
                hint="Переважно асортимент рослин — ціни задаються за проєктом"
              />
            </div>
            <div style={{ marginTop: 12 }}>
              <Link className="btn" to="/catalog">Відкрити каталог</Link>
            </div>
          </Card>

          <Card title="Правила та шаблон">
            {h ? (
              <div className="grid cols-2">
                <Stat label="Секцій кошторису" value={h.template_sections} small />
                <Stat
                  label="Правил кількостей"
                  value={h.quantity_rules}
                  small
                  hint="Витягнуті з шаблону замовника"
                />
              </div>
            ) : (
              <Spinner />
            )}
            {h && h.template_sections === 0 && (
              <div style={{ marginTop: 12 }}>
                <Notice tone="warn" title="Шаблон не скомпільовано">
                  Виконайте <span className="mono">python scripts/compile_template.py «Шаблон для ШІ.xlsx»</span>
                </Notice>
              </div>
            )}
          </Card>
        </div>

        <Card title="Останні проєкти" tight>
          {data.recent_projects.length === 0 ? (
            <Empty title="Проєктів ще немає">
              <p className="muted">Створіть проєкт і завантажте проєктну документацію.</p>
              <Link className="btn btn-primary" to="/projects">Створити проєкт</Link>
            </Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Проєкт</th>
                    <th>Замовник</th>
                    <th>Адреса</th>
                    <th>Статус</th>
                    <th className="num">Документів</th>
                    <th className="num">Кошторисів</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_projects.map((p) => (
                    <tr key={p.id}>
                      <td><Link to={`/projects/${p.id}`}>{p.name}</Link></td>
                      <td>{p.client_name || "—"}</td>
                      <td className="muted">{p.address || "—"}</td>
                      <td><StatusTag status={p.status} /></td>
                      <td className="num">{p.document_count}</td>
                      <td className="num">{p.estimate_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </>
  );
}
