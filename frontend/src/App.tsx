import { NavLink, Navigate, Route, Routes, useParams } from "react-router-dom";
import { api } from "./api";
import { useAsync } from "./components";
import AnalysisView from "./pages/AnalysisView";
import Catalog from "./pages/Catalog";
import Dashboard from "./pages/Dashboard";
import DocumentView from "./pages/DocumentView";
import EstimateView from "./pages/EstimateView";
import ProjectView from "./pages/ProjectView";
import Projects from "./pages/Projects";
import Questions from "./pages/Questions";

export default function App() {
  return (
    <div className="shell">
      <Sidebar />
      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/projects/:projectId" element={<ProjectView />} />
          <Route path="/projects/:projectId/analysis" element={<AnalysisView />} />
          <Route path="/projects/:projectId/questions" element={<Questions />} />
          <Route path="/projects/:projectId/estimate/:estimateId" element={<EstimateView />} />
          <Route path="/documents/:documentId" element={<DocumentView />} />
          <Route path="/catalog" element={<Catalog />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function Sidebar() {
  const health = useAsync(() => api.health(), []);

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">К</div>
        <div>
          <div className="brand-name">AI Кошторисник</div>
          <div className="brand-sub">ландшафт і благоустрій</div>
        </div>
      </div>

      <div className="nav-group">Робота</div>
      <NavLink className="nav-link" to="/dashboard">Дашборд</NavLink>
      <NavLink className="nav-link" to="/projects">Проєкти</NavLink>

      <ProjectLinks />

      <div className="nav-group">Довідники</div>
      <NavLink className="nav-link" to="/catalog">
        Каталог
        {health.data && <span className="count">{health.data.catalog_items}</span>}
      </NavLink>

      <div style={{ marginTop: "auto", paddingTop: 18 }}>
        {health.data && (
          <div className="small muted" style={{ padding: "0 8px" }}>
            <div>Секцій шаблону: {health.data.template_sections}</div>
            <div>Правил кількостей: {health.data.quantity_rules}</div>
            <div style={{ marginTop: 6 }}>
              AI:{" "}
              {health.data.ai_available
                ? <span className="badge ok">доступний</span>
                : <span className="badge warn">вимкнено</span>}
            </div>
          </div>
        )}
      </div>
    </aside>
  );
}

/** Contextual links for the project currently open. */
function ProjectLinks() {
  const params = useParams();
  const projectId = params.projectId ? Number(params.projectId) : null;
  const path = window.location.pathname;
  const match = path.match(/\/projects\/(\d+)/);
  const id = projectId ?? (match ? Number(match[1]) : null);

  const questions = useAsync(
    () => (id ? api.listQuestions(id, { status: "open" }) : Promise.resolve([])),
    [id],
  );
  const project = useAsync(
    () => (id ? api.getProject(id) : Promise.resolve(null)),
    [id],
  );

  if (!id) return null;
  const openCount = questions.data?.length ?? 0;
  const estimateId = project.data?.latest_estimate_id;

  return (
    <>
      <div className="nav-group">Поточний проєкт</div>
      <NavLink className="nav-link" to={`/projects/${id}`} end>Документи</NavLink>
      <NavLink className="nav-link" to={`/projects/${id}/analysis`}>Аналіз об'єкта</NavLink>
      {estimateId && (
        <NavLink className="nav-link" to={`/projects/${id}/estimate/${estimateId}`}>Кошторис</NavLink>
      )}
      <NavLink className="nav-link" to={`/projects/${id}/questions`}>
        Питання
        {openCount > 0 && <span className="count alert">{openCount}</span>}
      </NavLink>
    </>
  );
}
