import { useState } from "react";
import { useParams } from "react-router-dom";
import {
  api,
  money,
  num,
  type CatalogItem,
  type Line,
  type Section,
  type Totals,
  type Validation,
} from "../api";
import {
  Card,
  ConfidenceBadge,
  Empty,
  FindingCard,
  Modal,
  Notice,
  NumberCell,
  SeverityBadge,
  Spinner,
  Stat,
  StatusTag,
  useAsync,
} from "../components";

const BLOCK_LABEL = { plants: "Рослини", materials: "Матеріали", works: "Робота" } as const;

const SOURCE_LABEL: Record<string, string> = {
  rule: "Правило шаблону",
  document: "З документа",
  user_input: "Введено вручну",
  historical: "З історичних кошторисів",
  template_default: "Значення за шаблоном",
  unknown: "Джерело невідоме",
};

export default function EstimateView() {
  const { estimateId } = useParams();
  const id = Number(estimateId);

  const { data, error, loading, setData, reload } = useAsync(() => api.getEstimate(id), [id]);
  const [validation, setValidation] = useState<Validation | null>(null);
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<Line | null>(null);
  const [swap, setSwap] = useState<Line | null>(null);
  const [message, setMessage] = useState<{ tone: "info" | "warn" | "error" | "ok"; text: string } | null>(null);
  const [tab, setTab] = useState<"estimate" | "issues">("estimate");

  const applyResult = (result: { sections: Section[]; totals: Totals; validation: Validation }) => {
    if (data) setData({ ...data, sections: result.sections, totals: result.totals });
    setValidation(result.validation);
  };

  const run = async (fn: () => Promise<{ sections: Section[]; totals: Totals; validation: Validation }>) => {
    setBusy(true);
    setMessage(null);
    try {
      applyResult(await fn());
    } catch (e) {
      setMessage({ tone: "error", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  const changeQuantity = (line: Line, quantity: number) =>
    run(() => api.updateLine(id, line.id, { quantity }));

  const changePrice = (line: Line, unit_price: number) =>
    run(() => api.updateLine(id, line.id, { unit_price }));

  const removeLine = (line: Line) => {
    if (!confirm(`Видалити позицію «${line.name}»?`)) return;
    run(() => api.deleteLine(id, line.id));
  };

  const chooseCandidate = (line: Line, item: CatalogItem) => {
    setSwap(null);
    run(() => api.updateLine(id, line.id, { catalog_id: item.catalog_id }));
  };

  const approve = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await api.approve(id);
      setValidation(result.report);
      setMessage({ tone: "ok", text: "Кошторис затверджено. Можна експортувати." });
      reload();
    } catch (e) {
      setMessage({
        tone: "warn",
        text: `${(e as Error).message} Перегляньте вкладку «Проблеми».`,
      });
      setTab("issues");
      try {
        setValidation(await api.validateEstimate(id));
      } catch {
        /* validation endpoint already reported above */
      }
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <Spinner label="Завантаження кошторису…" />;
  if (error) return <Notice tone="error" title="Помилка">{error}</Notice>;
  if (!data) return null;

  const t = data.totals;
  const findings = validation?.findings ?? data.issues.map((i) => ({ ...i, line_name: null, section: null }));
  const counts = validation?.counts ?? {};
  const flagged = new Map<string, string>();
  findings.forEach((f) => {
    if (f.line_name && !flagged.has(f.line_name)) flagged.set(f.line_name, f.severity);
  });

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{data.title}</h1>
          <div className="sub">
            Версія {data.version} · <StatusTag status={data.status} />
            {busy && <> · <span className="muted">перерахунок…</span></>}
          </div>
        </div>
        <div className="row">
          <button onClick={() => run(() => api.recalculate(id))} disabled={busy}>Перерахувати</button>
          <button onClick={approve} disabled={busy}>Затвердити</button>
          <a className="btn" href={api.exportPdfUrl(id)}>Завантажити PDF</a>
          <a className="btn btn-primary" href={api.exportUrl(id)}>Експорт у XLSX</a>
        </div>
      </div>

      <div className="stack">
        {message && <Notice tone={message.tone}>{message.text}</Notice>}

        <div className="grid cols-4">
          <Stat label="Разом за матеріали" value={money(t.materials_total)} small />
          <Stat label="Разом за роботу" value={money(t.works_total)} small />
          <Stat label="Загальний рахунок" value={money(t.grand_total)} tone="accent" small
            hint={t.surcharge ? `Включно з надбавкою ${money(t.surcharge)}` : undefined} />
          <Stat
            label="Маржа"
            value={money(t.margin)}
            small
            hint={`${(t.margin_pct * 100).toFixed(1)}% від підсумку`}
          />
        </div>

        <div className="grid cols-3">
          <Stat label="Аванс (матеріали)" value={money(t.prepayment_materials)} small
            hint={t.formulas?.prepayment_materials} />
          <Stat label="Аванс (роботи)" value={money(t.prepayment_works)} small
            hint={t.formulas?.prepayment_works} />
          <Stat label="Залишок" value={money(t.balance)} small hint={t.formulas?.balance} />
        </div>

        <div className="tabs">
          <button className={tab === "estimate" ? "tab on" : "tab"} onClick={() => setTab("estimate")}>
            Кошторис
          </button>
          <button className={tab === "issues" ? "tab on" : "tab"} onClick={() => setTab("issues")}>
            Проблеми та питання
            {(counts.ERROR || counts.NEEDS_USER_INPUT) ? (
              <span className="badge error" style={{ marginLeft: 6 }}>
                {(counts.ERROR ?? 0) + (counts.NEEDS_USER_INPUT ?? 0)}
              </span>
            ) : null}
          </button>
        </div>

        {tab === "issues" ? (
          <div className="stack">
            {validation && (
              <Notice tone={validation.exportable ? "ok" : "warn"}>
                <div className="spread">
                  <span>
                    {validation.exportable
                      ? "Критичних проблем немає — кошторис можна затверджувати."
                      : "Є проблеми, які блокують затвердження."}
                  </span>
                  <SeverityBadge severity={validation.status} />
                </div>
              </Notice>
            )}
            {findings.length === 0 ? (
              <Empty title="Проблем не виявлено">
                <p className="muted">Натисніть «Перерахувати», щоб запустити перевірку ще раз.</p>
              </Empty>
            ) : (
              <div className="stack">
                {findings.map((f, i) => <FindingCard key={i} finding={f as never} />)}
              </div>
            )}
          </div>
        ) : data.sections.length === 0 ? (
          <Empty title="У кошторисі немає позицій із кількістю">
            <p className="muted">
              Секції показуються лише коли в них є позиції з кількістю більшою за нуль —
              так само, як у шаблоні замовника.
            </p>
          </Empty>
        ) : (
          <Card tight>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th style={{ width: 40 }}>#</th>
                    <th>Найменування</th>
                    <th style={{ width: 100 }} className="num">К-сть</th>
                    <th style={{ width: 70 }}>Од.</th>
                    <th style={{ width: 120 }} className="num">Ціна</th>
                    <th style={{ width: 130 }} className="num">Сума</th>
                    <th style={{ width: 130 }}>Джерело</th>
                    <th style={{ width: 120 }}>Статус</th>
                    <th style={{ width: 130 }} />
                  </tr>
                </thead>
                <tbody>
                  {data.sections.map((section) => (
                    <SectionRows
                      key={section.key}
                      section={section}
                      flagged={flagged}
                      busy={busy}
                      onQuantity={changeQuantity}
                      onPrice={changePrice}
                      onDetail={setDetail}
                      onSwap={setSwap}
                      onDelete={removeLine}
                    />
                  ))}
                  <tr className="total-row">
                    <td colSpan={5}>Разом за матеріали</td>
                    <td className="num">{money(t.materials_total)}</td>
                    <td colSpan={3} />
                  </tr>
                  <tr className="total-row">
                    <td colSpan={5}>Разом за роботу</td>
                    <td className="num">{money(t.works_total)}</td>
                    <td colSpan={3} />
                  </tr>
                  <tr className="total-row">
                    <td colSpan={5}><strong>Загальний рахунок</strong></td>
                    <td className="num"><strong>{money(t.grand_total)}</strong></td>
                    <td colSpan={3} />
                  </tr>
                </tbody>
              </table>
            </div>
          </Card>
        )}
      </div>

      {detail && <LineDetail line={detail} onClose={() => setDetail(null)} />}
      {swap && (
        <SwapDialog
          line={swap}
          onClose={() => setSwap(null)}
          onPick={(item) => chooseCandidate(swap, item)}
        />
      )}
    </>
  );
}

function SectionRows({
  section,
  flagged,
  busy,
  onQuantity,
  onPrice,
  onDetail,
  onSwap,
  onDelete,
}: {
  section: Section;
  flagged: Map<string, string>;
  busy: boolean;
  onQuantity: (line: Line, v: number) => void;
  onPrice: (line: Line, v: number) => void;
  onDetail: (line: Line) => void;
  onSwap: (line: Line) => void;
  onDelete: (line: Line) => void;
}) {
  return (
    <>
      <tr className="section-row">
        <td colSpan={5}>Рахунок {section.title}</td>
        <td className="num">{money(section.total)}</td>
        <td colSpan={3} />
      </tr>
      {section.blocks.map((block) => (
        <>
          <tr className="block-row" key={`${section.key}-${block.kind}`}>
            <td colSpan={9}>{BLOCK_LABEL[block.kind]}</td>
          </tr>
          {block.lines.map((line, i) => {
            const flag = flagged.get(line.name);
            return (
              <tr key={line.id} className={flag ? `flag-${flag}` : undefined}>
                <td className="muted num">{i + 1}</td>
                <td>
                  <div className="trunc" title={line.name}><strong>{line.name}</strong></div>
                  {line.comment && <div className="small muted">{line.comment}</div>}
                </td>
                <td className="num">
                  <NumberCell value={line.quantity} disabled={busy} onCommit={(v) => onQuantity(line, v)} />
                </td>
                <td className="muted">{line.unit || "—"}</td>
                <td className="num">
                  <NumberCell value={line.unit_price} disabled={busy} onCommit={(v) => onPrice(line, v)} />
                </td>
                <td className="num"><strong>{money(line.total)}</strong></td>
                <td>
                  <span className="small muted">{SOURCE_LABEL[line.qty_source] ?? line.qty_source}</span>
                  {line.locked && <div className="small muted">заблоковано</div>}
                </td>
                <td>
                  {line.match_status === "matched"
                    ? <ConfidenceBadge level={line.confidence} />
                    : <span className="badge warn">
                        {line.match_status === "ambiguous" ? "Потрібен вибір" : "Немає в каталозі"}
                      </span>}
                </td>
                <td className="num">
                  <button className="sm ghost" onClick={() => onDetail(line)} title="Чому додано">Чому?</button>
                  <button className="sm ghost" onClick={() => onSwap(line)} title="Замінити позицію">↔</button>
                  <button className="sm ghost danger" onClick={() => onDelete(line)} title="Видалити">✕</button>
                </td>
              </tr>
            );
          })}
          <tr key={`${section.key}-${block.kind}-total`}>
            <td colSpan={5} className="num muted">
              {block.kind === "works" ? "Разом за роботу:" :
                block.kind === "plants" ? "Разом за рослини:" : "Разом за матеріали:"}
            </td>
            <td className="num"><strong>{money(block.total)}</strong></td>
            <td colSpan={3} />
          </tr>
        </>
      ))}
    </>
  );
}

function LineDetail({ line, onClose }: { line: Line; onClose: () => void }) {
  return (
    <Modal title="Чому ця позиція тут" onClose={onClose}>
      <h3 style={{ marginBottom: 10 }}>{line.name}</h3>

      <div className="grid cols-3" style={{ marginBottom: 14 }}>
        <Stat label="Кількість" value={`${num(line.quantity)} ${line.unit}`} small />
        <Stat label="Ціна" value={money(line.unit_price)} small />
        <Stat label="Сума" value={money(line.total)} small />
      </div>

      <div className="field">
        <label>Формула суми</label>
        <div className="trace mono">{line.formula}</div>
      </div>

      <div className="field">
        <label>Звідки кількість</label>
        <div className="trace">
          {SOURCE_LABEL[line.qty_source] ?? line.qty_source}
          {line.qty_trace && <div style={{ marginTop: 6 }}>{line.qty_trace}</div>}
        </div>
      </div>

      {line.qty_expr && (
        <div className="field">
          <label>Правило шаблону</label>
          <div className="trace mono" style={{ wordBreak: "break-all" }}>{line.qty_expr}</div>
          <div className="help">Правило витягнуто з файлу «Шаблон для ШІ.xlsx».</div>
        </div>
      )}

      {line.reasons.length > 0 && (
        <div className="field">
          <label>Обґрунтування</label>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {line.reasons.map((r, i) => <li key={i}>{r}</li>)}
          </ul>
        </div>
      )}

      {line.source_refs.length > 0 && (
        <div className="field">
          <label>Джерела</label>
          <ul style={{ margin: 0, paddingLeft: 18 }} className="small">
            {line.source_refs.map((s, i) => (
              <li key={i}>
                <span className="badge">{s.source_type}</span> {s.source_ref}
                {s.detail && <div className="muted">{s.detail}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="field">
        <label>Маржинальність</label>
        <div className="trace">
          Собівартість {money(line.unit_cost)} за од. · маржа по позиції {money(line.margin)}
        </div>
      </div>
    </Modal>
  );
}

function SwapDialog({
  line,
  onClose,
  onPick,
}: {
  line: Line;
  onClose: () => void;
  onPick: (item: CatalogItem) => void;
}) {
  const [query, setQuery] = useState(line.name);
  const { data, loading } = useAsync(() => api.catalog({ q: query, limit: 12 }), [query]);
  const options = data?.items ?? line.candidates;

  return (
    <Modal title={`Замінити позицію: ${line.name}`} onClose={onClose}>
      <div className="field">
        <label>Пошук у каталозі</label>
        <input value={query} onChange={(e) => setQuery(e.target.value)} autoFocus />
        <div className="help">
          Пошук враховує категорію, характеристики (діаметр, розмір, щільність) та одиницю виміру.
        </div>
      </div>

      {loading ? (
        <Spinner label="Пошук…" />
      ) : options.length === 0 ? (
        <Notice tone="warn" title="Нічого не знайдено">
          Система не пропонує неіснуючих товарів. Уточніть запит або додайте позицію в базу.
        </Notice>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr><th>Назва</th><th>Категорія</th><th>Од.</th><th className="num">Ціна</th><th /></tr>
            </thead>
            <tbody>
              {options.map((item) => (
                <tr key={item.catalog_id}>
                  <td>
                    <div className="trunc" title={item.name}>{item.name}</div>
                    {item.reasons && <div className="small muted">{item.reasons.join("; ")}</div>}
                  </td>
                  <td className="small muted">{item.category}</td>
                  <td className="muted">{item.unit}</td>
                  <td className="num">{money(item.unit_price)}</td>
                  <td className="num">
                    <button className="sm primary" onClick={() => onPick(item)}>Обрати</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}
