/** Shared presentational pieces. */

import { type ReactNode, useEffect, useRef, useState } from "react";
import { api, money, type CatalogItem, type Confidence, type Finding, type Severity } from "./api";

export function Card({
  title,
  action,
  children,
  tight,
}: {
  title?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  tight?: boolean;
}) {
  return (
    <section className="card">
      {title && (
        <header className="card-head">
          <h2>{title}</h2>
          {action}
        </header>
      )}
      <div className={tight ? "card-body tight" : "card-body"}>{children}</div>
    </section>
  );
}

export function Stat({
  label,
  value,
  hint,
  tone,
  small,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  tone?: "accent" | "alert";
  small?: boolean;
}) {
  return (
    <div className={`card stat ${tone ?? ""}`}>
      <div className="label">{label}</div>
      <div className={small ? "value sm" : "value"}>{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

const SEVERITY_LABEL: Record<Severity, string> = {
  OK: "Все гаразд",
  WARNING: "Попередження",
  ERROR: "Помилка",
  NEEDS_USER_INPUT: "Потрібна відповідь",
};

const SEVERITY_CLASS: Record<Severity, string> = {
  OK: "ok",
  WARNING: "warn",
  ERROR: "error",
  NEEDS_USER_INPUT: "info",
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <span className={`badge ${SEVERITY_CLASS[severity]}`}>{SEVERITY_LABEL[severity]}</span>;
}

const CONFIDENCE_LABEL: Record<Confidence, string> = {
  high: "Висока впевненість",
  medium: "Середня впевненість",
  low: "Низька впевненість",
};

export function ConfidenceBadge({ level }: { level: Confidence }) {
  return <span className={`badge ${level}`}>{CONFIDENCE_LABEL[level]}</span>;
}

/** A finding rendered as: problem, why, what to do, impact. */
export function FindingCard({ finding }: { finding: Finding }) {
  return (
    <article className={`finding ${finding.severity}`}>
      <div className="spread">
        <strong>{finding.title}</strong>
        <SeverityBadge severity={finding.severity} />
      </div>
      <dl>
        {finding.detail && (
          <>
            <dt>Чому</dt>
            <dd>{finding.detail}</dd>
          </>
        )}
        {finding.fix_hint && (
          <>
            <dt>Що зробити</dt>
            <dd>{finding.fix_hint}</dd>
          </>
        )}
        {finding.impact && (
          <>
            <dt>Вплив</dt>
            <dd>{finding.impact}</dd>
          </>
        )}
        {finding.line_name && (
          <>
            <dt>Позиція</dt>
            <dd>{finding.line_name}</dd>
          </>
        )}
      </dl>
    </article>
  );
}

/** Project / estimate lifecycle, in one visual language.
 *  Backend statuses are English keys; the operator sees plain Ukrainian. */
const STATUS_TAGS: Record<string, { label: string; cls: string }> = {
  draft: { label: "Чернетка", cls: "tag-draft" },
  analysed: { label: "Проаналізовано", cls: "tag-input" },
  estimated: { label: "Кошторис сформовано", cls: "tag-warn" },
  approved: { label: "Затверджено", cls: "tag-ready" },
  exported: { label: "Експортовано", cls: "tag-ready" },
  uploaded: { label: "Завантажено", cls: "tag-draft" },
  extracted: { label: "Розібрано", cls: "tag-input" },
  error: { label: "Помилка", cls: "tag-error" },
  open: { label: "Потрібна відповідь", cls: "tag-input" },
  answered: { label: "Готово", cls: "tag-ready" },
  dismissed: { label: "Пропущено", cls: "tag-draft" },
};

export function StatusTag({ status, title }: { status: string; title?: string }) {
  const tag = STATUS_TAGS[status] ?? { label: status, cls: "tag-draft" };
  return <span className={`tag ${tag.cls}`} title={title}>{tag.label}</span>;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

export function Notice({
  tone = "info",
  title,
  children,
}: {
  tone?: "info" | "warn" | "error" | "ok";
  title?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className={`notice ${tone}`}>
      {title && <strong>{title}</strong>}
      {children}
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
  footer,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>{title}</h2>
          <button className="ghost" onClick={onClose} aria-label="Закрити">
            ✕
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer className="modal-foot">{footer}</footer>}
      </div>
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="row">
      <span className="spinner" />
      {label && <span className="muted small">{label}</span>}
    </span>
  );
}

/** Editable number cell that commits on blur or Enter. */
export function NumberCell({
  value,
  onCommit,
  disabled,
}: {
  value: number;
  onCommit: (next: number) => void;
  disabled?: boolean;
}) {
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);

  const commit = () => {
    const parsed = Number(text.replace(",", "."));
    if (!Number.isFinite(parsed) || parsed === value) {
      setText(String(value));
      return;
    }
    onCommit(parsed);
  };

  return (
    <input
      className="cell"
      value={text}
      disabled={disabled}
      onChange={(e) => setText(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        if (e.key === "Escape") setText(String(value));
      }}
    />
  );
}

/** Type-ahead over the catalog. Never invents a product: it only offers rows
 *  the price base actually contains. */
export function CatalogPicker({
  value,
  placeholder,
  kind,
  onPick,
  onTextChange,
}: {
  value: string;
  placeholder?: string;
  kind?: string;
  onPick: (item: CatalogItem) => void;
  onTextChange?: (text: string) => void;
}) {
  const [text, setText] = useState(value);
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<CatalogItem[]>([]);
  const [loading, setLoading] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => setText(value), [value]);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  useEffect(() => {
    if (!open || text.trim().length < 2) {
      setItems([]);
      return;
    }
    let alive = true;
    setLoading(true);
    const timer = setTimeout(() => {
      api
        .catalog({ q: text, kind, limit: 8 })
        .then((r) => alive && setItems(r.items))
        .catch(() => alive && setItems([]))
        .finally(() => alive && setLoading(false));
    }, 220);
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [text, open, kind]);

  return (
    <div className="picker" ref={box}>
      <input
        value={text}
        placeholder={placeholder ?? "Почніть вводити назву…"}
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setText(e.target.value);
          setOpen(true);
          onTextChange?.(e.target.value);
        }}
      />
      {open && (text.trim().length >= 2) && (
        <div className="picker-menu">
          {loading && <div className="picker-empty"><Spinner label="Пошук…" /></div>}
          {!loading && items.length === 0 && (
            <div className="picker-empty">
              Немає такої позиції в каталозі. Система не пропонує вигаданих товарів.
            </div>
          )}
          {items.map((item) => (
            <button
              key={item.catalog_id}
              type="button"
              className="picker-item"
              onClick={() => {
                setText(item.name);
                setOpen(false);
                onPick(item);
              }}
            >
              <span className="picker-name">{item.name}</span>
              <span className="picker-meta">
                {item.category} · {item.unit} ·{" "}
                {item.unit_price > 0 ? money(item.unit_price) : "ціни немає"}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    fn()
      .then((value) => alive && setData(value))
      .catch((e: Error) => alive && setError(e.message))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return { data, error, loading, reload: () => setNonce((n) => n + 1), setData };
}
