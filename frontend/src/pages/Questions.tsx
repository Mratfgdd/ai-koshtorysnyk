import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api, type Question } from "../api";
import { CatalogPicker, Card, Empty, Notice, Spinner, useAsync } from "../components";

/** Families ordered so the cheapest wins come first. */
const FAMILY_HINT: Record<string, string> = {
  price: "Одна ціна застосовується до всіх обраних рослин.",
  match: "Оберіть позицію каталогу — вона підставиться в усі обрані питання.",
  missing: "Знайдіть аналог у каталозі або опишіть заміну текстом.",
  coverage: "Вкажіть, до якої позиції кошторису віднести кількість із креслення.",
  conflict: "Оберіть, яке значення вважати правильним.",
};

export default function Questions() {
  const { projectId } = useParams();
  const id = Number(projectId);

  const [family, setFamily] = useState<string>("");
  const [search, setSearch] = useState("");
  const [onlyOpen, setOnlyOpen] = useState(true);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [bulkValue, setBulkValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "error" | "info"; text: string } | null>(null);
  const [drafts, setDrafts] = useState<Record<number, string>>({});

  const summary = useAsync(() => api.questionsSummary(id), [id]);
  const { data, error, loading, reload } = useAsync(
    () => api.listQuestions(id, {
      family: family || undefined,
      status: onlyOpen ? "open" : undefined,
      q: search || undefined,
    }),
    [id, family, onlyOpen, search],
  );

  const questions = useMemo(() => data ?? [], [data]);
  const selectable = questions.filter((q) => q.status === "open");
  const allSelected = selectable.length > 0 && selectable.every((q) => selected.has(q.id));

  const refresh = () => {
    setSelected(new Set());
    setBulkValue("");
    reload();
    summary.reload();
  };

  const toggle = (qid: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(qid) ? next.delete(qid) : next.add(qid);
      return next;
    });

  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(selectable.map((q) => q.id)));

  const applyBulk = async () => {
    if (!bulkValue.trim() || selected.size === 0) return;
    setBusy(true);
    setMessage(null);
    try {
      const r = await api.bulkAnswer(id, [...selected], bulkValue.trim());
      setMessage({ tone: "ok", text: `Застосовано до ${r.answered} питань, кошторис перераховано.` });
      refresh();
    } catch (e) {
      setMessage({ tone: "error", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const dismiss = async () => {
    if (selected.size === 0) return;
    if (!confirm(`Пропустити ${selected.size} питань? Вони позначаться як свідомо пропущені.`)) return;
    setBusy(true);
    try {
      const r = await api.dismissQuestions(id, [...selected], "Пропущено користувачем");
      setMessage({ tone: "info", text: `Пропущено ${r.dismissed} питань.` });
      refresh();
    } catch (e) {
      setMessage({ tone: "error", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const answerOne = async (q: Question, value: string) => {
    if (!value.trim()) return;
    setBusy(true);
    try {
      await api.answerQuestion(q.id, value.trim());
      setMessage({ tone: "ok", text: "Відповідь збережено, кошторис перераховано." });
      refresh();
    } catch (e) {
      setMessage({ tone: "error", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const s = summary.data;

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Питання</h1>
          <div className="sub">
            Система не вигадує відсутні дані — вона питає.
            {s && <> Відкритих: <strong>{s.open}</strong> із {s.total}.</>}
          </div>
        </div>
        <button onClick={refresh} disabled={busy}>Оновити</button>
      </div>

      <div className="stack">
        {message && <Notice tone={message.tone}>{message.text}</Notice>}
        {error && <Notice tone="error" title="Помилка">{error}</Notice>}

        {/* --- filters ---------------------------------------------------- */}
        <Card>
          <div className="filter-bar">
            <div className="chips">
              <button className={family === "" ? "chip on" : "chip"} onClick={() => setFamily("")}>
                Усі{s ? ` · ${s.open}` : ""}
              </button>
              {(s?.families ?? []).map((f) => (
                <button
                  key={f.family}
                  className={family === f.family ? "chip on" : "chip"}
                  onClick={() => setFamily(f.family)}
                >
                  {f.label} · {f.open}
                </button>
              ))}
            </div>
            <div className="filter-tools">
              <input
                className="search"
                value={search}
                placeholder="Пошук у тексті питання…"
                onChange={(e) => setSearch(e.target.value)}
              />
              <label className="toggle">
                <input
                  type="checkbox"
                  checked={onlyOpen}
                  onChange={(e) => setOnlyOpen(e.target.checked)}
                />
                Лише відкриті
              </label>
            </div>
          </div>
        </Card>

        {/* --- bulk actions ------------------------------------------------ */}
        {selectable.length > 0 && (
          <Card>
            <div className="bulk">
              <label className="toggle">
                <input type="checkbox" checked={allSelected} onChange={toggleAll} />
                Обрати всі на екрані ({selectable.length})
              </label>

              <span className="bulk-count">
                {selected.size > 0 ? `обрано ${selected.size}` : "нічого не обрано"}
              </span>

              <div className="bulk-input">
                {family === "match" || family === "missing" ? (
                  <CatalogPicker
                    value={bulkValue}
                    placeholder="Знайти позицію каталогу…"
                    onPick={(item) => setBulkValue(item.name)}
                    onTextChange={setBulkValue}
                  />
                ) : (
                  <input
                    type={family === "price" ? "number" : "text"}
                    value={bulkValue}
                    placeholder={
                      family === "price"
                        ? "Ціна за 1 шт для всіх обраних"
                        : "Одна відповідь для всіх обраних"
                    }
                    onChange={(e) => setBulkValue(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && applyBulk()}
                  />
                )}
              </div>

              <button
                className="primary"
                onClick={applyBulk}
                disabled={busy || selected.size === 0 || !bulkValue.trim()}
              >
                Застосувати до {selected.size || "…"}
              </button>
              <button onClick={dismiss} disabled={busy || selected.size === 0}>
                Пропустити
              </button>
            </div>
            {family && FAMILY_HINT[family] && (
              <div className="small muted" style={{ marginTop: 10 }}>{FAMILY_HINT[family]}</div>
            )}
          </Card>
        )}

        {/* --- list -------------------------------------------------------- */}
        {loading ? (
          <Card><Spinner label="Завантаження питань…" /></Card>
        ) : questions.length === 0 ? (
          <Empty title="Питань немає">
            <p className="muted">
              {family || search
                ? "За цим фільтром нічого не знайдено."
                : "Питання з'являються після аналізу документів або формування кошторису."}
            </p>
          </Empty>
        ) : (
          <div className="q-list">
            {questions.map((q) => (
              <QuestionCard
                key={q.id}
                question={q}
                selected={selected.has(q.id)}
                busy={busy}
                draft={drafts[q.id] ?? ""}
                onToggle={() => toggle(q.id)}
                onDraft={(v) => setDrafts({ ...drafts, [q.id]: v })}
                onAnswer={(v) => answerOne(q, v)}
              />
            ))}
          </div>
        )}
      </div>
    </>
  );
}

function QuestionCard({
  question,
  selected,
  busy,
  draft,
  onToggle,
  onDraft,
  onAnswer,
}: {
  question: Question;
  selected: boolean;
  busy: boolean;
  draft: string;
  onToggle: () => void;
  onDraft: (v: string) => void;
  onAnswer: (v: string) => void;
}) {
  const open = question.status === "open";
  const usesCatalog = question.kind_key === "match" || question.kind_key === "missing";

  return (
    <article className={`q-card ${open ? "" : "done"} ${selected ? "picked" : ""}`}>
      <div className="q-head">
        {open && (
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggle}
            aria-label="Обрати питання"
          />
        )}
        <div className="q-title">
          <strong>{question.text}</strong>
          {question.why && <div className="small muted">{question.why}</div>}
        </div>
        <span className={`tag ${open ? "tag-input" : "tag-ready"}`}>
          {open ? "Потрібна відповідь" : question.status === "dismissed" ? "Пропущено" : "Готово"}
        </span>
      </div>

      {open ? (
        <div className="q-answer">
          {usesCatalog ? (
            <CatalogPicker
              value={draft}
              placeholder="Знайти аналог у каталозі…"
              onPick={(item) => onAnswer(item.name)}
              onTextChange={onDraft}
            />
          ) : question.kind === "choice" && question.choices.length > 0 ? (
            <select value={draft} onChange={(e) => onDraft(e.target.value)}>
              <option value="">— оберіть варіант —</option>
              {question.choices.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          ) : question.kind === "boolean" ? (
            <select value={draft} onChange={(e) => onDraft(e.target.value)}>
              <option value="">— оберіть —</option>
              <option value="так">Так</option>
              <option value="ні">Ні</option>
            </select>
          ) : (
            <input
              type={question.kind === "number" ? "number" : "text"}
              value={draft}
              placeholder={question.kind === "number" ? "Число" : "Відповідь"}
              onChange={(e) => onDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && onAnswer(draft)}
            />
          )}
          <button
            className="primary"
            onClick={() => onAnswer(draft)}
            disabled={busy || !draft.trim()}
          >
            Відповісти
          </button>
        </div>
      ) : (
        <div className="q-done">Відповідь: <strong>{question.answer}</strong></div>
      )}

      {question.affects.filter(Boolean).length > 0 && (
        <div className="q-foot small muted">Впливає на: {question.affects.filter(Boolean).join(", ")}</div>
      )}
    </article>
  );
}
