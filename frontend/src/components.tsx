import { useEffect, useState } from "react";
import { Link, Outlet, useNavigate, useParams } from "react-router-dom";
import { api } from "./api";
import { WorkflowProvider, useWorkflow } from "./WorkflowContext";
import { statusLabel, steps } from "./workflow";
import type { NodeKey } from "./types";

export function WorkflowShell() {
  const { workflowExecutionId } = useParams();
  const id = Number(workflowExecutionId);
  if (!Number.isInteger(id) || id <= 0) return <p>잘못된 Workflow 주소입니다.</p>;
  return <WorkflowProvider workflowExecutionId={id}><WorkflowLayout /></WorkflowProvider>;
}

function WorkflowLayout() { return <main className="workflow-layout"><header><Link to="/request" className="brand">VORA</Link><span>숏폼 제작 워크플로우</span></header><WorkflowStepper /><Outlet /></main>; }

function WorkflowStepper() {
  const { workflow, workflowExecutionId } = useWorkflow();
  const paths: Record<NodeKey, string> = { input_analysis: "plan", content_planning: "plan", script_generation: "script", video_generation: "video" };
  return <nav className="stepper" aria-label="Workflow 단계">{steps.map(({ key, label }, index) => {
    const node = workflow?.nodes[key]; const status = node?.status;
    return <Link key={key} className={`step ${status ?? "PENDING"}`} to={`/workflows/${workflowExecutionId}/${paths[key]}`} aria-current={status === "WAITING_APPROVAL" ? "step" : undefined}>
      <span>{index + 1}</span><strong>{label}</strong><small>{status ? statusLabel[status] : "대기"}</small>
    </Link>;
  })}</nav>;
}

export function RevisionModal({ nodeKey, onClose }: { nodeKey: NodeKey; onClose: () => void }) {
  const { workflowExecutionId, refetch } = useWorkflow(); const navigate = useNavigate();
  const [text, setText] = useState(""); const [busy, setBusy] = useState(false); const [restart, setRestart] = useState<string | null>(null); const [error, setError] = useState("");
  const submit = async () => { if (!text.trim()) { setError("수정 요청을 입력해주세요."); return; } setBusy(true); setError(""); try { const result = await api.revise(workflowExecutionId, nodeKey, text); await refetch(); if (result.restart_node !== nodeKey) setRestart(result.restart_node); else onClose(); } catch (e) { setError(e instanceof Error ? e.message : "수정 요청에 실패했습니다."); } finally { setBusy(false); } };
  const destination = restart === "content_planning" ? "plan" : restart === "script_generation" ? "script" : "video";
  const restartLabel = restart === "content_planning" ? "기획" : restart === "script_generation" ? "대본" : "영상";
  if (restart) return <div className="modal-backdrop" role="dialog" aria-modal="true"><section className="modal"><h2>{restartLabel} 단계부터 다시 생성 중입니다.</h2><p>{restartLabel} 화면으로 이동하시겠습니까?</p><div className="actions"><button onClick={onClose}>현재 화면에 있기</button><button className="primary" onClick={() => navigate(`/workflows/${workflowExecutionId}/${destination}`)}>{restartLabel} 화면으로 이동</button></div></section></div>;
  return <div className="modal-backdrop" role="dialog" aria-modal="true"><section className="modal"><h2>수정 요청</h2><label>원하는 변경을 자연어로 알려주세요.<textarea value={text} onChange={(event) => setText(event.target.value)} rows={5} autoFocus /></label>{error && <p className="error">{error}</p>}<div className="actions"><button onClick={onClose}>취소</button><button className="primary" onClick={() => void submit()} disabled={busy}>{busy ? "요청 중…" : "수정 요청"}</button></div></section></div>;
}

export function ApprovalActions({ nodeKey }: { nodeKey: NodeKey }) {
  const { workflow, workflowExecutionId, refetch } = useWorkflow(); const navigate = useNavigate(); const [modal, setModal] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const node = workflow?.nodes[nodeKey];
  const approve = async () => { if (!node) return; setBusy(true); setError(""); try { await api.approve(workflowExecutionId, node.id); await refetch(); navigate(`/workflows/${workflowExecutionId}/${nodeKey === "content_planning" ? "script" : "video"}`); } catch (e) { setError(e instanceof Error ? e.message : "승인에 실패했습니다."); } finally { setBusy(false); } };
  if (node?.status !== "WAITING_APPROVAL") return null;
  return <><div className="actions">{nodeKey === "video_generation" && <a className="button" href={api.assetUrl(`/workflow-executions/${workflowExecutionId}/video/download`)}>영상 다운로드</a>}<button className="primary" onClick={() => void approve()} disabled={busy}>{busy ? "처리 중…" : nodeKey === "video_generation" ? "최종 승인" : "승인"}</button><button onClick={() => setModal(true)}>수정 요청</button></div>{error && <p className="error">{error}</p>}{modal && <RevisionModal nodeKey={nodeKey} onClose={() => setModal(false)} />}</>;
}

export function usePolling<T extends { node: { status: string } | null }>(loader: () => Promise<T>) {
  const [data, setData] = useState<T | null>(null); const [error, setError] = useState(""); const [refreshKey, setRefreshKey] = useState(0);
  useEffect(() => { let live = true; let timer: number | undefined; const load = async () => { try { const next = await loader(); if (!live) return; setData(next); setError(""); if (next.node?.status === "PENDING" || next.node?.status === "RUNNING") timer = window.setTimeout(() => void load(), 2000); } catch (e) { if (live) setError(e instanceof Error ? e.message : "조회에 실패했습니다."); } }; void load(); return () => { live = false; if (timer) window.clearTimeout(timer); }; }, [loader, refreshKey]);
  return { data, error, refetch: () => setRefreshKey((value) => value + 1) };
}
