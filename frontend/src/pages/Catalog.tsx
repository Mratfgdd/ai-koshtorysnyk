import { useState } from "react";
import { api, money } from "../api";
import { Card, Empty, Notice, Spinner, Stat, useAsync } from "../components";

const KIND_LABEL: Record<string, string> = {
  material: "Матеріали",
  work: "Роботи",
  plant: "Рослини",
};

export default function Catalog() {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<string>("");
  const [category, setCategory] = useState<string>("");
  const [page, setPage] = useState(0);
  const limit = 50;

  const categories = useAsync(() => api.catalogCategories(), []);
  const { data, error, loading } = useAsync(
    () => api.catalog({ q: query, kind: kind || undefined, category: category || undefined, limit, offset: page * limit }),
    [query, kind, category, page],
  );

  const items = data?.items ?? [];
  const withoutPrice = items.filter((i) => i.unit_price <= 0).length;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Каталог товарів і робіт</h1>
          <div className="sub">Прайс-база замовника «2026 База 1» — джерело цін для кошторисів</div>
        </div>
      </div>

      <div className="stack">
        <Card>
          <div className="grid cols-3">
            <div className="field" style={{ margin: 0 }}>
              <label>Пошук</label>
              <input
                value={query}
                placeholder="Назва, характеристика, розмір…"
                onChange={(e) => { setQuery(e.target.value); setPage(0); }}
              />
              <div className="help">
                Порожній запит — перегляд списку. Із запитом вмикається ранжування за схожістю.
              </div>
            </div>
            <div className="field" style={{ margin: 0 }}>
              <label>Тип</label>
              <select value={kind} onChange={(e) => { setKind(e.target.value); setPage(0); }}>
                <option value="">Усі</option>
                <option value="material">Матеріали</option>
                <option value="work">Роботи</option>
                <option value="plant">Рослини</option>
              </select>
            </div>
            <div className="field" style={{ margin: 0 }}>
              <label>Категорія</label>
              <select value={category} onChange={(e) => { setCategory(e.target.value); setPage(0); }}>
                <option value="">Усі</option>
                {(categories.data ?? []).map((c) => (
                  <option key={c.category} value={c.category}>{c.category} ({c.total})</option>
                ))}
              </select>
            </div>
          </div>
        </Card>

        <div className="grid cols-3">
          <Stat label="Знайдено" value={data?.total ?? 0} small />
          <Stat label="Показано" value={items.length} small />
          <Stat
            label="Без ціни реалізації"
            value={withoutPrice}
            small
            tone={withoutPrice ? "alert" : undefined}
            hint="Рослини у базі ведуться без цін"
          />
        </div>

        {error && <Notice tone="error" title="Помилка">{error}</Notice>}

        <Card tight>
          {loading ? (
            <div className="card-body"><Spinner label="Пошук…" /></div>
          ) : items.length === 0 ? (
            <Empty title="Нічого не знайдено">
              <p className="muted">
                Система не пропонує позицій, яких немає в базі. Уточніть запит.
              </p>
            </Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Найменування</th>
                    <th>Категорія</th>
                    <th>Тип</th>
                    <th>Од.</th>
                    <th className="num">Собівартість</th>
                    <th className="num">Ціна</th>
                    <th className="num">Маржа</th>
                    {data?.ranked && <th className="num">Оцінка</th>}
                  </tr>
                </thead>
                <tbody>
                  {items.map((item) => (
                    <tr key={item.catalog_id}>
                      <td>
                        <div className="trunc" title={item.name}>{item.name}</div>
                        {item.reasons && <div className="small muted">{item.reasons.join("; ")}</div>}
                      </td>
                      <td className="small muted">{item.category}</td>
                      <td><span className="badge">{KIND_LABEL[item.kind] ?? item.kind}</span></td>
                      <td className="muted">{item.unit}</td>
                      <td className="num muted">{item.unit_cost ? money(item.unit_cost) : "—"}</td>
                      <td className="num">
                        {item.unit_price
                          ? <strong>{money(item.unit_price)}</strong>
                          : <span className="badge warn">не вказано</span>}
                      </td>
                      <td className="num muted">
                        {item.margin_pct ? `${(item.margin_pct * 100).toFixed(0)}%` : "—"}
                      </td>
                      {data?.ranked && <td className="num muted">{item.score?.toFixed(0)}</td>}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        {!data?.ranked && (data?.total ?? 0) > limit && (
          <div className="row">
            <button disabled={page === 0} onClick={() => setPage((p) => p - 1)}>Назад</button>
            <span className="muted small">
              Сторінка {page + 1} з {Math.ceil((data?.total ?? 0) / limit)}
            </span>
            <button
              disabled={(page + 1) * limit >= (data?.total ?? 0)}
              onClick={() => setPage((p) => p + 1)}
            >
              Далі
            </button>
          </div>
        )}
      </div>
    </>
  );
}
