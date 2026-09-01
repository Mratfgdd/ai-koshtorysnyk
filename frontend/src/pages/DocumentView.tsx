import { useParams } from "react-router-dom";
import { api } from "../api";
import { Card, ConfidenceBadge, Empty, Notice, Spinner, Stat, useAsync } from "../components";

export default function DocumentView() {
  const { documentId } = useParams();
  const id = Number(documentId);
  const { data, error, loading } = useAsync(() => api.getDocument(id), [id]);

  if (loading) return <Spinner label="Завантаження документа…" />;
  if (error) return <Notice tone="error" title="Помилка">{error}</Notice>;
  if (!data) return null;

  const vision = data.pages.filter((p) => p.needs_vision);
  const analysed = data.pages.filter((p) => p.analysed);
  const skipped = data.pages.filter((p) => !p.needs_vision);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{data.filename}</h1>
          <div className="sub">
            {data.page_count} сторінок · {(data.size_bytes / 1e6).toFixed(1)} МБ · {data.kind}
          </div>
        </div>
      </div>

      <div className="stack">
        {data.error && <Notice tone="error" title="Помилка обробки">{data.error}</Notice>}

        <div className="grid cols-4">
          <Stat label="Усього сторінок" value={data.page_count} />
          <Stat label="Потребують візуального аналізу" value={vision.length}
            hint="Растрові креслення без тексту" />
          <Stat label="Проаналізовано" value={analysed.length} />
          <Stat label="Пропущено" value={skipped.length}
            hint="Титули, візуалізації, маркетинг" />
        </div>

        <Notice tone="info" title="Як обробляється документ">
          Спершу виконується дешеве вилучення тексту й таблиць, далі сторінки класифікуються за
          типом. Дорогий візуальний аналіз запускається лише для сторінок, що несуть кількості:
          генплан, схема розпланування, дендроплан, відомості. Титули, візуалізації та
          маркетингові розвороти не аналізуються.
        </Notice>

        <Card title="Сторінки" tight>
          {data.pages.length === 0 ? (
            <Empty title="Сторінок немає" />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 60 }}>№</th>
                    <th>Тип сторінки</th>
                    <th className="num">Символів</th>
                    <th className="num">Зображень</th>
                    <th>Обробка</th>
                    <th>Стан</th>
                  </tr>
                </thead>
                <tbody>
                  {data.pages.map((p) => (
                    <tr key={p.page_number}>
                      <td className="muted">{p.page_number}</td>
                      <td><strong>{p.page_type_label}</strong></td>
                      <td className="num muted">{p.text_length}</td>
                      <td className="num muted">{p.image_count}</td>
                      <td>
                        {p.needs_vision
                          ? <span className="badge info">Візуальний аналіз</span>
                          : <span className="badge">Пропущено</span>}
                      </td>
                      <td>
                        {p.analysed
                          ? <ConfidenceBadge level={p.confidence} />
                          : <span className="muted small">не аналізовано</span>}
                      </td>
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
