import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Project } from "../api";
import { Card, Empty, Modal, Notice, Spinner, StatusTag, useAsync } from "../components";

export default function Projects() {
  const { data, error, loading, reload } = useAsync(() => api.listProjects(), []);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", client_name: "", address: "", manager: "", brief: "" });
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  // Uploading straight from the list saves opening the project first, which is
  // the whole point of the button in the row.
  const [uploadTo, setUploadTo] = useState<Project | null>(null);
  const [uploading, setUploading] = useState<string[]>([]);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const uploadFiles = async (project: Project, files: FileList | File[]) => {
    const list = Array.from(files).filter((f) => f.name.toLowerCase().endsWith(".pdf"));
    const rejected = Array.from(files).length - list.length;
    setUploadError(rejected > 0 ? `Пропущено ${rejected} файл(ів): підтримуються лише PDF.` : null);
    if (list.length === 0) return;

    const uploaded: string[] = [];
    for (const file of list) {
      setUploading((u) => [...u, file.name]);
      try {
        await api.uploadDocument(project.id, file);
        uploaded.push(file.name);
      } catch (e) {
        setUploadError(`${file.name}: ${(e as Error).message}`);
      } finally {
        setUploading((u) => u.filter((n) => n !== file.name));
      }
    }
    if (uploaded.length) setDone(`Завантажено до «${project.name}»: ${uploaded.join(", ")}`);
    reload();
  };

  const create = async () => {
    if (!form.name.trim()) return;
    setBusy(true);
    setFailure(null);
    try {
      await api.createProject(form);
      setCreating(false);
      setForm({ name: "", client_name: "", address: "", manager: "", brief: "" });
      reload();
    } catch (e) {
      setFailure((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: number, name: string) => {
    if (!confirm(`Видалити проєкт «${name}» разом із документами та кошторисами?`)) return;
    await api.deleteProject(id);
    reload();
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Проєкти</h1>
          <div className="sub">Кожен проєкт — окремий об'єкт із документами та кошторисами</div>
        </div>
        <button className="primary" onClick={() => setCreating(true)}>Новий проєкт</button>
      </div>

      {error && <Notice tone="error" title="Помилка">{error}</Notice>}
      {done && <Notice tone="ok">{done}</Notice>}

      <Card tight>
        {loading ? (
          <div className="card-body"><Spinner label="Завантаження…" /></div>
        ) : !data || data.length === 0 ? (
          <Empty title="Проєктів ще немає">
            <p className="muted">Створіть перший проєкт, щоб завантажити документацію.</p>
            <button className="primary" onClick={() => setCreating(true)}>Створити проєкт</button>
          </Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Назва</th>
                  <th>Замовник</th>
                  <th>Адреса</th>
                  <th>ПМ</th>
                  <th>Статус</th>
                  <th>Документи</th>
                  <th className="num">Док.</th>
                  <th className="num">Кошт.</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {data.map((p) => (
                  <tr key={p.id}>
                    <td><Link to={`/projects/${p.id}`}><strong>{p.name}</strong></Link></td>
                    <td>{p.client_name || "—"}</td>
                    <td className="muted">{p.address || "—"}</td>
                    <td className="muted">{p.manager || "—"}</td>
                    <td><StatusTag status={p.status} /></td>
                    <td>
                      <button
                        className="sm"
                        onClick={() => {
                          setUploadTo(p);
                          setUploadError(null);
                          setDone(null);
                        }}
                      >
                        Завантажити проєкт
                      </button>
                    </td>
                    <td className="num">{p.document_count}</td>
                    <td className="num">{p.estimate_count}</td>
                    <td className="num">
                      <button className="sm danger" onClick={() => remove(p.id, p.name)}>Видалити</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {uploadTo && (
        <Modal
          title={`Завантажити документи — ${uploadTo.name}`}
          onClose={() => setUploadTo(null)}
          footer={
            <>
              <button onClick={() => setUploadTo(null)}>Закрити</button>
              <Link className="btn btn-primary" to={`/projects/${uploadTo.id}`}>
                Відкрити проєкт
              </Link>
            </>
          }
        >
          {uploadError && <Notice tone="error">{uploadError}</Notice>}
          <input
            ref={fileRef}
            type="file"
            accept="application/pdf"
            multiple
            hidden
            onChange={(e) => e.target.files && uploadFiles(uploadTo, e.target.files)}
          />
          <div
            className="drop"
            style={{ cursor: "pointer" }}
            onClick={() => fileRef.current?.click()}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => {
              e.preventDefault();
              uploadFiles(uploadTo, e.dataTransfer.files);
            }}
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
          {done && <Notice tone="ok">{done}</Notice>}
        </Modal>
      )}

      {creating && (
        <Modal
          title="Новий проєкт"
          onClose={() => setCreating(false)}
          footer={
            <>
              <button onClick={() => setCreating(false)}>Скасувати</button>
              <button className="primary" onClick={create} disabled={busy || !form.name.trim()}>
                {busy ? "Створення…" : "Створити"}
              </button>
            </>
          }
        >
          {failure && <Notice tone="error">{failure}</Notice>}
          <div className="field">
            <label>Назва проєкту *</label>
            <input
              value={form.name}
              autoFocus
              placeholder="Наприклад: Будьків, ділянка Галини"
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </div>
          <div className="grid cols-2">
            <div className="field">
              <label>Замовник</label>
              <input value={form.client_name} onChange={(e) => setForm({ ...form, client_name: e.target.value })} />
            </div>
            <div className="field">
              <label>Менеджер проєкту</label>
              <input value={form.manager} onChange={(e) => setForm({ ...form, manager: e.target.value })} />
            </div>
          </div>
          <div className="field">
            <label>Адреса об'єкту</label>
            <input value={form.address} onChange={(e) => setForm({ ...form, address: e.target.value })} />
          </div>
          <div className="field">
            <label>Технічне завдання</label>
            <textarea
              value={form.brief}
              placeholder="Побажання замовника, склад робіт, обмеження. Використовується разом із кресленнями."
              onChange={(e) => setForm({ ...form, brief: e.target.value })}
            />
            <div className="help">Необов'язково, але помітно покращує аналіз.</div>
          </div>
        </Modal>
      )}
    </>
  );
}
