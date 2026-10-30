import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Navigate, Route, Routes } from "react-router-dom";
import { BrowserRouter } from "react-router-dom";
import { WorkflowShell } from "./components";
import { DashboardPage } from "./dashboard";
import { PlanPage, ScriptPage, SocialSettingsPage, VideoPage } from "./pages";
import "./styles.css";
import "./studio.css";
import "./creativeStudio.css";
import "./v3Studio.css";
import "./homePrototype.css";
import "./auth.css";
import "./recentProjects.css";
import { AuthCallbackPage, AuthProvider, LoginPage, RequireAuth } from "./auth";

createRoot(document.getElementById("root")!).render(<StrictMode><BrowserRouter><AuthProvider><Routes>
  <Route path="/login" element={<LoginPage />} />
  <Route path="/auth/callback" element={<AuthCallbackPage />} />
  <Route path="/request" element={<DashboardPage />} />
  <Route path="/settings/social" element={<RequireAuth><SocialSettingsPage /></RequireAuth>} />
  <Route path="/workflows/:workflowExecutionId" element={<RequireAuth><WorkflowShell /></RequireAuth>}>
    <Route path="plan" element={<PlanPage />} /><Route path="script" element={<ScriptPage />} /><Route path="video" element={<VideoPage />} />
  </Route>
  <Route path="*" element={<Navigate to="/request" replace />} />
</Routes></AuthProvider></BrowserRouter></StrictMode>);
