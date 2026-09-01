import { useCallback, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, type DocumentInfo } from "../api";
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
  const { data, error, loading, reload } = useAsync(() => api.getProject(id), [id]);

  const [uploading, setUploading] = useState<string[]>([]);
  const [over, setOver] = useState(false);
  const [analysing, setAnalysing] = useState(false);
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

  const analyse = async () => {
    setAnalysing(true);
    setMessage(null);
    try {
      const result = await api.analyze(id);
      if (result.status === "ok") {
        setMessage({ tone: "ok", text: "Аналіз завершено. Перейдіть до «Аналіз об'єкта»." });
      } else {
        setMessage({ tone: "error", text: result.message ?? "Аналіз не вдався." });
      }
      reload();
    } catch (e) {
      setMessage({ tone: "error", text: (e as Error).message });
    } finally {
      setAnalysing(false);
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
          <Link className="btn" to={`/projects/${id}/analysis`}>Аналіз об'єкта</Link>
          {data.latest_estimate_id && (
            <Link className="btn" to={`/projects/${id}/estimate/${data.latest_estimate_id}`}>Кошторис</Link>
          )}
          <button className="primary" onClick={analyse} disabled={analysing || data.documents.length === 0}>
            {analysing ? "Аналіз триває…" : "Аналізувати документи"}
          </button>
        </div>
      </div>

      <div className="stack">
        {message && <Notice tone={message.tone}>{message.text}</Notice>}

        {analysing && (
          <Card>
            <Spinner label="Обробка документів: дешеве вилучення тексту, класифікація сторінок, потім аналіз лише сторінок із даними." />
            <div className="progress" style={{ marginTop: 10 }}>
              <div style={{ width: "60%" }} />
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
                      <td className="num">
                        <button className="sm danger" onClick={() => removeDocument(d)}>Видалити</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
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
