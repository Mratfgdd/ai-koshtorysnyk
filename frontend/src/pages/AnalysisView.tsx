import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, type Analysis, type Fact, num } from "../api";
import { Card, ConfidenceBadge, Empty, Notice, Spinner, useAsync } from "../components";

const STATUS_LABEL: Record<Fact["status"], string> = {
  confirmed: "Підтверджено",
  assumption: "Припущення",
  unknown: "Невідомо",
  needs_user_input: "Потрібне рішення",
};

export default function AnalysisView() {
  const { projectId } = useParams();
  const id = Number(projectId);
  const navigate = useNavigate();

  const { data, error, loading, setData, reload } = useAsync(() => api.getAnalysis(id), [id]);
  const [saving, setSaving] = useState(false);
  const [building, setBuilding] = useState(false);
  const [message, setMessage] = useState<{ tone: "info" | "warn" | "error" | "ok"; text: string } | null>(null);

  const plants = useMemo(() => data?.plants ?? [], [data]);
  const included = plants.filter((p) => !p.is_existing);
  const existing = plants.filter((p) => p.is_existing);

  const patch = async (changes: Partial<Analysis>) => {
    if (!data) return;
    setSaving(true);
    try {
      const updated = await api.updateAnalysis(data.id, changes);
      setData(updated);
    } catch (e) {
      setMessage({ tone: "error", text: (e as Error).message });
    } finally {
      setSaving(false);
    }
  };

  const editFact = (index: number, changes: Partial<Fact>) => {
    if (!data) return;
    const facts = data.facts.map((f, i) => (i === index ? { ...f, ...changes } : f));
    setData({ ...data, facts });
  };

  const build = async () => {
    setBuilding(true);
    setMessage(null);
    try {
      const result = await api.createEstimate(id, {});
      if (result.status === "ok" && result.estimate_id) {
        navigate(`/projects/${id}/estimate/${result.estimate_id}`);
      } else {
        setMessage({ tone: "error", text: result.message ?? "Не вдалося сформувати кошторис." });
      }
    } catch (e) {
      setMessage({ tone: "error", text: (e as Error).message });
    } finally {
      setBuilding(false);
    }
  };

  if (loading) return <Spinner label="Завантаження аналізу…" />;
  if (error)
    return (
      <Empty title="Аналіз ще не сформовано">
        <p className="muted">{error}</p>
        <button onClick={() => navigate(`/projects/${id}`)}>До проєкту</button>
      </Empty>
    );
  if (!data) return null;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Аналіз об'єкта</h1>
          <div className="sub">
            {data.object_type || "Тип об'єкта не визначено"} · версія {data.version}
            {saving && <> · <span className="muted">збереження…</span></>}
          </div>
        </div>
        <div className="row">
          <button onClick={() => patch({ facts: data.facts })} disabled={saving}>Зберегти зміни</button>
          <button className="primary" onClick={build} disabled={building}>
            {building ? "Формування…" : "Сформувати кошторис"}
          </button>
        </div>
      </div>

      <div className="stack">
        {message && <Notice tone={message.tone}>{message.text}</Notice>}

        {data.summary && <Card title="Стисло">{data.summary}</Card>}

        {data.conflicts.length > 0 && (
          <Card title="Суперечливі дані">
            <div className="stack">
              {data.conflicts.map((c, i) => (
                <Notice key={i} tone="warn" title={c.topic}>
                  <div>Варіанти: {c.values.join(" / ")}</div>
                  {c.impact && <div className="small muted">Вплив: {c.impact}</div>}
                  {c.question && <div style={{ marginTop: 6 }}><strong>{c.question}</strong></div>}
                </Notice>
              ))}
              <div className="small muted">
                Система не обирає варіант самостійно — відповідь дається у розділі «Питання».
              </div>
            </div>
          </Card>
        )}

        <Card
          title="Показники об'єкта"
          action={<span className="small muted">Значення можна редагувати — кошторис перерахується</span>}
          tight
        >
          {data.facts.length === 0 ? (
            <Empty title="Показників не виявлено" />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Показник</th>
                    <th style={{ width: 150 }}>Значення</th>
                    <th>Од.</th>
                    <th>Статус</th>
                    <th>Впевненість</th>
                    <th>Джерело</th>
                  </tr>
                </thead>
                <tbody>
                  {data.facts.map((f, i) => (
                    <tr key={`${f.key}-${i}`}>
                      <td>
                        <strong>{f.label || f.key}</strong>
                        {f.note && <div className="small muted">{f.note}</div>}
                      </td>
                      <td>
                        <input
                          className="cell"
                          value={f.value}
                          onChange={(e) => editFact(i, { value: e.target.value, status: "confirmed" })}
                        />
                      </td>
                      <td className="muted">{f.unit || "—"}</td>
                      <td>
                        <select
                          value={f.status}
                          onChange={(e) => editFact(i, { status: e.target.value as Fact["status"] })}
                        >
                          {Object.entries(STATUS_LABEL).map(([k, v]) => (
                            <option key={k} value={k}>{v}</option>
                          ))}
                        </select>
                      </td>
                      <td><ConfidenceBadge level={f.confidence} /></td>
                      <td className="small muted">{f.source_ref || f.source_type}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <div className="grid cols-2">
          <Card title="Виявлені системи" tight>
            {data.systems.length === 0 ? (
              <Empty title="Систем не виявлено" />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Система</th><th>Підстава</th><th>Впевненість</th></tr></thead>
                  <tbody>
                    {data.systems.map((s, i) => (
                      <tr key={i}>
                        <td><strong>{s.label}</strong><div className="small muted mono">{s.key}</div></td>
                        <td className="small">{s.evidence}</td>
                        <td><ConfidenceBadge level={s.confidence} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card title="Відомість покриттів" tight>
            {data.components.length === 0 ? (
              <Empty title="Даних немає" />
            ) : (
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Найменування</th><th className="num">К-сть</th><th>Од.</th></tr></thead>
                  <tbody>
                    {data.components.map((c, i) => (
                      <tr key={i}>
                        <td>{c.name}{c.note && <div className="small muted">{c.note}</div>}</td>
                        <td className="num">{num(c.quantity)}</td>
                        <td className="muted">{c.unit}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>

        <Card
          title="Асортиментна відомість рослин"
          action={
            <span className="small muted">
              До кошторису: {included.length} · існуючі (виключені): {existing.length}
            </span>
          }
          tight
        >
          {plants.length === 0 ? (
            <Empty title="Рослин не виявлено" />
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>№</th><th>Назва</th><th>Латинська</th>
                    <th className="num">К-сть</th><th>Примітка</th><th>У кошторис</th>
                  </tr>
                </thead>
                <tbody>
                  {plants.map((p, i) => (
                    <tr key={i} style={p.is_existing ? { opacity: 0.55 } : undefined}>
                      <td className="muted">{p.number || i + 1}</td>
                      <td><strong>{p.name}</strong>{p.size && <div className="small muted">{p.size}</div>}</td>
                      <td className="small muted"><i>{p.latin_name}</i></td>
                      <td className="num">{num(p.quantity)}</td>
                      <td className="small muted">{p.note}</td>
                      <td>
                        {p.is_existing
                          ? <span className="badge">Існуюча — не включати</span>
                          : <span className="badge ok">Включено</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <div className="grid cols-3">
          <ListCard title="Припущення" items={data.assumptions} tone="warn" />
          <ListCard title="Невідомо" items={data.unknowns} tone="info" />
          <ListCard title="Ризики" items={data.risks} tone="error" />
        </div>

        <div className="row">
          <button onClick={reload}>Оновити</button>
        </div>
      </div>
    </>
  );
}

function ListCard({ title, items, tone }: { title: string; items: string[]; tone: "warn" | "info" | "error" }) {
  return (
    <Card title={title}>
      {items.length === 0 ? (
        <div className="muted small">Немає</div>
      ) : (
        <ul style={{ margin: 0, paddingLeft: 18 }} className={tone === "error" ? "small" : "small"}>
          {items.map((t, i) => <li key={i} style={{ marginBottom: 5 }}>{t}</li>)}
        </ul>
      )}
    </Card>
  );
}
