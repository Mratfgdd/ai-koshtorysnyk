import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { Card, Empty, Modal, Notice, Spinner, StatusTag, useAsync } from "../components";

export default function Projects() {
  const { data, error, loading, reload } = useAsync(() => api.listProjects(), []);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: "", client_name: "", address: "", manager: "", brief: "" });
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

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
