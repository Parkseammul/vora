import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Navigate, Route, Routes } from "react-router-dom";
import { BrowserRouter } from "react-router-dom";
import { WorkflowShell } from "./components";
import { PlanPage, RequestPage, ScriptPage, SocialSettingsPage, VideoPage } from "./pages";
import "./styles.css";

createRoot(document.getElementById("root")!).render(<StrictMode><BrowserRouter><Routes>
  <Route path="/request" element={<RequestPage />} />
  <Route path="/settings/social" element={<SocialSettingsPage />} />
  <Route path="/workflows/:workflowExecutionId" element={<WorkflowShell />}>
    <Route path="plan" element={<PlanPage />} /><Route path="script" element={<ScriptPage />} /><Route path="video" element={<VideoPage />} />
  </Route>
  <Route path="*" element={<Navigate to="/request" replace />} />
</Routes></BrowserRouter></StrictMode>);
