import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  api,
  num,
  type Analysis,
  type ComponentRow,
  type Fact,
  type FactStatus,
  type SystemRow,
} from "../api";
import { Card, ConfidenceBadge, Empty, Notice, Spinner, useAsync } from "../components";

const STATUS_LABEL: Record<FactStatus, string> = {
  confirmed: "Підтверджено",
  assumption: "Припущення",
  unknown: "Невідомо",
  needs_user_input: "Потрібне рішення",
  excluded: "Не враховувати в КП",
};

/** Statuses whose value the backend keeps out of the estimate. */
const OUT_OF_ESTIMATE: FactStatus[] = ["unknown", "needs_user_input", "excluded"];

export default function AnalysisView() {
  const { projectId } = useParams();
  const id = Number(projectId);
  const navigate = useNavigate();

  const { data, error, loading, setData, reload } = useAsync(() => api.getAnalysis(id), [id]);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [building, setBuilding] = useState(false);
  const [message, setMessage] = useState<{ tone: "info" | "warn" | "error" | "ok"; text: string } | null>(null);

  const plants = useMemo(() => data?.plants ?? [], [data]);
  const included = plants.filter((p) => !p.is_existing);
  const existing = plants.filter((p) => p.is_existing);
  const excluded = (data?.facts ?? []).filter((f) => f.status === "excluded").length;

  const edit = (changes: Partial<Analysis>) => {
    if (!data) return;
    setData({ ...data, ...changes });
    setDirty(true);
  };

  const save = async () => {
    if (!data) return;
    setSaving(true);
    setMessage(null);
    try {
      const updated = await api.updateAnalysis(data.id, {
        facts: data.facts,
        systems: data.systems,
        components: data.components,
      });
      setData(updated);
      setDirty(false);
      setMessage({
        tone: "ok",
        text: "Зміни збережено. Натисніть «Сформувати кошторис», щоб перерахувати.",
      });
    } catch (e) {
      setMessage({ tone: "error", text: (e as Error).message });
    } finally {
      setSaving(false);
    }
  };

  // --- facts -----------------------------------------------------------------
  const editFact = (index: number, changes: Partial<Fact>) => {
    if (!data) return;
    edit({ facts: data.facts.map((f, i) => (i === index ? { ...f, ...changes } : f)) });
  };

  // --- systems ---------------------------------------------------------------
  const editSystem = (index: number, changes: Partial<SystemRow>) => {
    if (!data) return;
    edit({ systems: data.systems.map((s, i) => (i === index ? { ...s, ...changes } : s)) });
  };
  const addSystem = () => {
    if (!data) return;
    edit({
      systems: [
        ...data.systems,
        { key: "", label: "", evidence: "Додано вручну", confidence: "high" },
      ],
    });
  };
  const removeSystem = (index: number) => {
    if (!data) return;
    edit({ systems: data.systems.filter((_, i) => i !== index) });
  };

  // --- coverage --------------------------------------------------------------
  const editComponent = (index: number, changes: Partial<ComponentRow>) => {
    if (!data) return;
    edit({ components: data.components.map((c, i) => (i === index ? { ...c, ...changes } : c)) });
  };
  const addComponent = () => {
    if (!data) return;
    edit({
      components: [...data.components, { name: "", quantity: null, unit: "м2", note: "Додано вручну" }],
    });
  };
  const removeComponent = (index: number) => {
    if (!data) return;
    edit({ components: data.components.filter((_, i) => i !== index) });
  };

  const build = async () => {
    setBuilding(true);
    setMessage(null);
    try {
      if (dirty) await save();
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
            {dirty && !saving && <> · <span className="badge warn">є незбережені зміни</span></>}
          </div>
        </div>
        <div className="row">
          <button onClick={save} disabled={saving || !dirty}>Зберегти зміни</button>
          <button className="primary" onClick={build} disabled={building || saving}>
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
          action={
            <span className="small muted">
              Значення можна редагувати
              {excluded > 0 && <> · виключено з КП: {excluded}</>}
            </span>
          }
          tight
        >
          {data.facts.length === 0 ? (
            <Empty title="Показників не виявлено" />
          ) : (
            <>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Показник</th>
                      <th style={{ width: 150 }}>Значення</th>
                      <th>Од.</th>
                      <th style={{ width: 180 }}>Статус</th>
                      <th>Впевненість</th>
                      <th>Джерело</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.facts.map((f, i) => {
                      const off = OUT_OF_ESTIMATE.includes(f.status);
                      return (
                        <tr key={`${f.key}-${i}`} style={off ? { opacity: 0.55 } : undefined}>
                          <td>
                            <strong>{f.label || f.key}</strong>
                            {f.note && <div className="small muted">{f.note}</div>}
                          </td>
                          <td>
                            <input
                              className="cell"
                              value={f.value}
                              onChange={(e) =>
                                editFact(i, {
                                  value: e.target.value,
                                  // Typing a value is a decision, unless the row
                                  // is deliberately held out of the estimate.
                                  status: f.status === "excluded" ? "excluded" : "confirmed",
                                })
                              }
                            />
                          </td>
                          <td className="muted">{f.unit || "—"}</td>
                          <td>
                            <select
                              value={f.status}
                              onChange={(e) => editFact(i, { status: e.target.value as FactStatus })}
                            >
                              {(Object.keys(STATUS_LABEL) as FactStatus[]).map((k) => (
                                <option key={k} value={k}>{STATUS_LABEL[k]}</option>
                              ))}
                            </select>
                          </td>
                          <td><ConfidenceBadge level={f.confidence} /></td>
                          <td className="small muted">{f.source_ref || f.source_type}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div className="small muted" style={{ padding: "8px 12px" }}>
                Показник зі статусом «Не враховувати в КП», «Невідомо» або «Потрібне рішення»
                не бере участі в розрахунку кошторису.
              </div>
            </>
          )}
        </Card>

        <div className="grid cols-2">
          <Card
            title="Виявлені системи"
            action={<button className="sm" onClick={addSystem}>Додати систему</button>}
            tight
          >
            {data.systems.length === 0 ? (
              <Empty title="Систем не виявлено">
                <button onClick={addSystem}>Додати вручну</button>
              </Empty>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Система</th>
                      <th>Ключ секції</th>
                      <th>Підстава</th>
                      <th>Впевн.</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {data.systems.map((s, i) => (
                      <tr key={i}>
                        <td>
                          <input
                            className="cell"
                            value={s.label}
                            placeholder="Автоматичний полив"
                            onChange={(e) => editSystem(i, { label: e.target.value })}
                          />
                        </td>
                        <td>
                          <input
                            className="cell mono"
                            value={s.key}
                            placeholder="irrigation"
                            onChange={(e) => editSystem(i, { key: e.target.value.trim() })}
                          />
                        </td>
                        <td>
                          <input
                            className="cell"
                            value={s.evidence}
                            onChange={(e) => editSystem(i, { evidence: e.target.value })}
                          />
                        </td>
                        <td><ConfidenceBadge level={s.confidence} /></td>
                        <td className="num">
                          <button className="sm danger" onClick={() => removeSystem(i)}>×</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="small muted" style={{ padding: "8px 12px" }}>
                  «Ключ секції» має збігатися з ключем секції шаблону — саме за ним
                  секція потрапляє до кошторису.
                </div>
              </div>
            )}
          </Card>

          <Card
            title="Відомість покриттів"
            action={<button className="sm" onClick={addComponent}>Додати позицію</button>}
            tight
          >
            {data.components.length === 0 ? (
              <Empty title="Даних немає">
                <button onClick={addComponent}>Додати вручну</button>
              </Empty>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Найменування</th>
                      <th className="num" style={{ width: 100 }}>К-сть</th>
                      <th style={{ width: 80 }}>Од.</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {data.components.map((c, i) => (
                      <tr key={i}>
                        <td>
                          <input
                            className="cell"
                            value={c.name}
                            placeholder="Бруківка бетонна"
                            onChange={(e) => editComponent(i, { name: e.target.value })}
                          />
                          {c.note && <div className="small muted">{c.note}</div>}
                        </td>
                        <td>
                          <input
                            className="cell num"
                            value={c.quantity ?? ""}
                            inputMode="decimal"
                            onChange={(e) => {
                              const raw = e.target.value.replace(",", ".").trim();
                              const parsed = raw === "" ? null : Number(raw);
                              editComponent(i, {
                                quantity: parsed === null || Number.isNaN(parsed) ? null : parsed,
                              });
                            }}
                          />
                        </td>
                        <td>
                          <input
                            className="cell"
                            value={c.unit}
                            onChange={(e) => editComponent(i, { unit: e.target.value })}
                          />
                        </td>
                        <td className="num">
                          <button className="sm danger" onClick={() => removeComponent(i)}>×</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="small muted" style={{ padding: "8px 12px" }}>
                  Всього позицій: {data.components.length} · із кількістю:{" "}
                  {data.components.filter((c) => typeof c.quantity === "number").length}.
                  Позиція без кількості до кошторису не потрапить.
                </div>
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
          <button onClick={reload} disabled={dirty}>Оновити</button>
          {dirty && <span className="small muted">Спершу збережіть або скасуйте зміни.</span>}
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
