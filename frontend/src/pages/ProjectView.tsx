import { useCallback, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, type AnalyzeResult, type DocumentInfo } from "../api";
import { Card, Empty, Notice, Spinner, Stat, StatusTag, useAsync } from "../components";

const KIND_LABEL: Record<string, string> = {
  drawings: "Комплект креслень",
  concept: "Концептуальний проєкт",
  estimate: "Готовий кошторис",
  brief: "Технічне завдання",
  unknown: "Не визначено",
};

export default function ProjectView() {
  const { projectId } = useParams();
  const id = Number(projectId);
  const navigate = useNavigate();
  const { data, error, loading, reload } = useAsync(() => api.getProject(id), [id]);

  const [uploading, setUploading] = useState<string[]>([]);
  const [over, setOver] = useState(false);
  const [analysing, setAnalysing] = useState<number | null>(null);
  const [step, setStep] = useState("");
  const [diagnosis, setDiagnosis] = useState<AnalyzeResult | null>(null);
  const [message, setMessage] = useState<{ tone: "info" | "warn" | "error" | "ok"; text: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const upload = useCallback(
    async (files: FileList | File[]) => {
      const list = Array.from(files).filter((f) => f.name.toLowerCase().endsWith(".pdf"));
      const rejected = Array.from(files).length - list.length;
      if (rejected > 0) {
        setMessage({ tone: "warn", text: `Пропущено ${rejected} файл(ів): підтримуються лише PDF.` });
      }
      for (const file of list) {
        setUploading((u) => [...u, file.name]);
        try {
          await api.uploadDocument(id, file);
        } catch (e) {
          setMessage({ tone: "error", text: `${file.name}: ${(e as Error).message}` });
        } finally {
          setUploading((u) => u.filter((n) => n !== file.name));
        }
      }
      reload();
    },
    [id, reload],
  );

  /**
   * The one button the estimator needs: document analysis, then the object
   * model, in that order.
   *
   * Both steps live behind a single POST — the per-page findings are what the
   * object model is fused from, so splitting them into two clicks only ever
   * produced a half-finished state. A successful reply means both are done,
   * and the object analysis is where the estimator has to go next, so we take
   * them there.
   */
  const analyse = async (doc: DocumentInfo) => {
    setAnalysing(doc.id);
    setMessage(null);
    setDiagnosis(null);
    setStep("Крок 1 з 2: аналіз документа — вилучення тексту, класифікація та розбір сторінок");
    try {
      const result = await api.analyze(id);
      setStep("Крок 2 з 2: аналіз об'єкта");
      reload();

      if (result.status !== "ok") {
        setDiagnosis(result);
        return;
      }
      if (result.degraded) {
        // Thin evidence: say so here rather than let a shaky analysis pass for
        // a solid one on the next screen.
        setDiagnosis(result);
        return;
      }
      navigate(`/projects/${id}/analysis`);
    } catch (e) {
      setMessage({ tone: "error", text: (e as Error).message });
    } finally {
      setAnalysing(null);
      setStep("");
    }
  };

  const removeDocument = async (doc: DocumentInfo) => {
    if (!confirm(`Видалити «${doc.filename}»?`)) return;
    await api.deleteDocument(doc.id);
    reload();
  };

  if (loading) return <Spinner label="Завантаження проєкту…" />;
  if (error) return <Notice tone="error" title="Помилка">{error}</Notice>;
  if (!data) return null;

  const totalPages = data.documents.reduce((s, d) => s + d.page_count, 0);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{data.name}</h1>
          <div className="sub">
            {[data.client_name, data.address, data.manager].filter(Boolean).join(" · ") || "Без деталей"}
          </div>
        </div>
        <div className="row">
          {data.latest_estimate_id && (
            <Link className="btn" to={`/projects/${id}/estimate/${data.latest_estimate_id}`}>Кошторис</Link>
          )}
        </div>
      </div>

      <div className="stack">
        {message && <Notice tone={message.tone}>{message.text}</Notice>}

        {diagnosis && (
          <Notice
            tone={diagnosis.degraded ? "warn" : "error"}
            title={diagnosis.degraded ? "Аналіз виконано з обмеженнями" : "Аналіз не дав результату"}
          >
            <div>{diagnosis.reason ?? diagnosis.message}</div>

            {diagnosis.recommendations && diagnosis.recommendations.length > 0 && (
              <>
                <div style={{ marginTop: 8, fontWeight: 600 }}>Що зробити:</div>
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                  {diagnosis.recommendations.map((r, i) => (
                    <li key={i} style={{ marginBottom: 3 }}>{r}</li>
                  ))}
                </ul>
              </>
            )}

            {diagnosis.stats && (
              <div className="small muted" style={{ marginTop: 8 }}>
                Прочитано: {diagnosis.stats.pages ?? 0} стор.
                {" · "}із текстом: {diagnosis.stats.pages_with_text ?? 0}
                {" · "}растрових: {diagnosis.stats.pages_raster_only ?? 0}
                {" · "}символів тексту: {diagnosis.stats.text_chars ?? 0}
              </div>
            )}

            {diagnosis.errors && diagnosis.errors.length > 0 && (
              <details style={{ marginTop: 8 }}>
                <summary className="small muted">Технічні деталі</summary>
                <ul className="small muted" style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                  {diagnosis.errors.slice(0, 12).map((t, i) => <li key={i}>{t}</li>)}
                </ul>
              </details>
            )}

            <div className="row" style={{ marginTop: 10 }}>
              <Link className="btn" to={`/projects/${id}/analysis`}>
                Відкрити аналіз об'єкта
              </Link>
              <button onClick={() => setDiagnosis(null)}>Приховати</button>
            </div>
          </Notice>
        )}

        {analysing !== null && (
          <Card>
            <Spinner label={step} />
            <div className="progress" style={{ marginTop: 10 }}>
              <div style={{ width: step.startsWith("Крок 2") ? "85%" : "45%" }} />
            </div>
            <div className="small muted" style={{ marginTop: 8 }}>
              Аналізуються лише сторінки з даними. На великому комплекті це кілька хвилин —
              сторінку можна не закривати.
            </div>
          </Card>
        )}

        <div className="grid cols-4">
          <Stat label="Документів" value={data.documents.length} />
          <Stat label="Сторінок" value={totalPages} />
          <Stat label="Статус" value={<StatusTag status={data.status} />} small />
          <Stat label="Кошторисів" value={data.estimate_count} />
        </div>

        <Card
          title="Документи"
          action={<button onClick={() => inputRef.current?.click()}>Завантажити PDF</button>}
        >
          <input
            ref={inputRef}
            type="file"
            accept="application/pdf"
            multiple
            hidden
            onChange={(e) => e.target.files && upload(e.target.files)}
          />
          <div
            className={over ? "drop over" : "drop"}
            onDragOver={(e) => { e.preventDefault(); setOver(true); }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => { e.preventDefault(); setOver(false); upload(e.dataTransfer.files); }}
            onClick={() => inputRef.current?.click()}
            style={{ cursor: "pointer" }}
          >
            Перетягніть сюди креслення, концепцію або ТЗ у PDF — або натисніть, щоб обрати.
            <div className="small" style={{ marginTop: 6 }}>
              Великі комплекти креслень (50–60 сторінок, понад 100 МБ) підтримуються.
            </div>
          </div>

          {uploading.length > 0 && (
            <div style={{ marginTop: 12 }}>
              <Spinner label={`Завантаження: ${uploading.join(", ")}`} />
            </div>
          )}

          {data.documents.length > 0 && (
            <div className="table-wrap" style={{ marginTop: 14 }}>
              <table>
                <thead>
                  <tr>
                    <th>Файл</th>
                    <th>Тип</th>
                    <th className="num">Сторінок</th>
                    <th className="num">Розмір</th>
                    <th>Статус</th>
                    <th>Аналіз</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.documents.map((d) => (
                    <tr key={d.id}>
                      <td><Link to={`/documents/${d.id}`}>{d.filename}</Link></td>
                      <td><span className="badge">{KIND_LABEL[d.kind] ?? d.kind}</span></td>
                      <td className="num">{d.page_count}</td>
                      <td className="num">{(d.size_bytes / 1e6).toFixed(1)} МБ</td>
                      <td>
                        <StatusTag status={d.status} title={d.error ?? undefined} />
                      </td>
                      <td>
                        <button
                          className="sm primary"
                          onClick={() => analyse(d)}
                          disabled={analysing !== null}
                          title="Аналіз документа, а потім автоматично аналіз об'єкта"
                        >
                          {analysing === d.id ? "Аналіз…" : "Аналіз"}
                        </button>
                      </td>
                      <td className="num">
                        <button
                          className="sm danger"
                          onClick={() => removeDocument(d)}
                          disabled={analysing !== null}
                        >
                          Видалити
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {data.documents.length > 1 && (
                <div className="small muted" style={{ marginTop: 8 }}>
                  Аналіз охоплює всі документи проєкту — модель об'єкта будується з них разом,
                  тому кнопка в будь-якому рядку дає той самий результат.
                </div>
              )}
            </div>
          )}
        </Card>

        {data.brief && (
          <Card title="Технічне завдання">
            <div style={{ whiteSpace: "pre-wrap" }}>{data.brief}</div>
          </Card>
        )}

        {data.documents.length === 0 && (
          <Empty title="Документів ще немає">
            <p className="muted">Завантажте креслення або концептуальний проєкт, щоб почати аналіз.</p>
          </Empty>
        )}
      </div>
    </>
  );
}
