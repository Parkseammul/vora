import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError } from "./api";
import type { V3ImageAsset, V3NodeType, V3Workflow, V3WorkflowTrace } from "./types";

const nodeOrder: V3NodeType[] = ["CONTENT_PLAN", "TEXT_GENERATE", "TEXT_CHECK", "IMAGE_GENERATE", "IMAGE_CHECK", "HUMAN_APPROVAL", "PUBLICATION"];
const summaryStages: Array<{ label: string; nodes: V3NodeType[] }> = [
  { label: "Plan", nodes: ["CONTENT_PLAN"] },
  { label: "Text", nodes: ["TEXT_GENERATE", "TEXT_CHECK"] },
  { label: "Image", nodes: ["IMAGE_GENERATE", "IMAGE_CHECK"] },
  { label: "Review", nodes: ["HUMAN_APPROVAL"] },
  { label: "Publish", nodes: ["PUBLICATION"] },
];

type ReusedNode = { sourceRevision: number };
type ReusedNodes = Partial<Record<V3NodeType, ReusedNode>>;

function statusLabel(status: string | null | undefined) {
  return status === "WAITING" ? "WAITING APPROVAL" : status ?? "PENDING";
}

function statusTone(status: string | null | undefined) {
  return status?.toLowerCase().replace("_", "-") ?? "pending";
}

function stageStatus(workflow: V3Workflow, nodes: V3NodeType[], reusedNodes: ReusedNodes): string {
  const values = nodes.map((node) => reusedNodes[node] ? "REUSED" : workflow.nodes[node]?.status);
  if (values.includes("FAILED")) return "FAILED";
  if (values.includes("WAITING")) return "WAITING";
  if (values.includes("RUNNING")) return "RUNNING";
  if (values.every((status) => status === "REUSED")) return "REUSED";
  if (values.every((status) => status === "SUCCEEDED" || status === "SKIPPED" || status === "REUSED")) return "SUCCEEDED";
  return "PENDING";
}

function reusedNodesFor(workflow: V3Workflow, trace: V3WorkflowTrace): ReusedNodes {
  const revision = trace.revisions.find((item) => item.revision_no === workflow.current_revision);
  if (!revision) return {};
  const startIndex = nodeOrder.indexOf(revision.start_node_type);
  if (startIndex < 0) return {};
  const result: ReusedNodes = {};
  for (const type of nodeOrder.slice(0, startIndex)) {
    if (workflow.nodes[type] !== null) continue;
    const previous = trace.nodes
      .filter((node) => node.node_type === type && node.revision_no < workflow.current_revision && node.status === "SUCCEEDED")
      .sort((left, right) => right.revision_no - left.revision_no)[0];
    if (previous) result[type] = { sourceRevision: previous.revision_no };
  }
  return result;
}

function apiErrorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 401) return "로그인이 만료되었습니다. 다시 인증한 뒤 시도해주세요.";
    if (error.status === 403 || error.status === 404) return "Workflow를 찾을 수 없거나 접근 권한이 없습니다.";
    if (error.status === 422) return error.message || "현재 Workflow 상태에서는 요청할 수 없습니다.";
    if (error.status >= 500) return "서버에서 처리하지 못했습니다. 잠시 후 다시 시도해주세요.";
  }
  return error instanceof Error ? error.message : "요청을 처리하지 못했습니다.";
}

export function V3WorkflowPage({ workflowExecutionId, initialWorkflow }: { workflowExecutionId: number; initialWorkflow?: V3Workflow }) {
  const navigate = useNavigate();
  const [workflow, setWorkflow] = useState<V3Workflow | null>(initialWorkflow ?? null);
  const [reusedNodes, setReusedNodes] = useState<ReusedNodes>({});
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(!initialWorkflow);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await api.getWorkflow(workflowExecutionId, signal);
      if ("workflow_version" in next && next.workflow_version === 3) {
        setWorkflow(next);
        if (next.current_revision > 0 && Object.values(next.nodes).some((node) => node === null)) {
          try {
            const trace = await api.getWorkflowTrace(workflowExecutionId, signal);
            setReusedNodes(reusedNodesFor(next, trace));
          } catch {
            setReusedNodes({});
          }
        } else setReusedNodes({});
      }
      else setError("이 Workflow는 V1 영상 Workflow입니다.");
      setError("");
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) setError(apiErrorMessage(cause));
    } finally { setLoading(false); }
  }, [workflowExecutionId]);

  useEffect(() => {
    const controller = new AbortController();
    if (!initialWorkflow) queueMicrotask(() => void load(controller.signal));
    return () => controller.abort();
  }, [initialWorkflow, load]);

  useEffect(() => {
    if (!workflow || !["PENDING", "RUNNING", "REVISING"].includes(workflow.status)) return undefined;
    const timer = window.setTimeout(() => void load(), 2500);
    return () => window.clearTimeout(timer);
  }, [load, workflow]);

  useEffect(() => {
    if (!initialWorkflow || initialWorkflow.current_revision <= 0 || !Object.values(initialWorkflow.nodes).some((node) => node === null)) return;
    const controller = new AbortController();
    void api.getWorkflowTrace(workflowExecutionId, controller.signal)
      .then((trace) => setReusedNodes(reusedNodesFor(initialWorkflow, trace)))
      .catch(() => undefined);
    return () => controller.abort();
  }, [initialWorkflow, workflowExecutionId]);

  const refresh = () => load();
  if (loading && !workflow) return <main className="v3-detail-shell"><p className="v3-state">Workflow를 불러오는 중입니다…</p></main>;
  if (error && !workflow) return <main className="v3-detail-shell"><p className="v3-error" role="alert">{error}</p><Link className="v3-button" to="/request">Creative Studio로 돌아가기</Link></main>;
  if (!workflow) return null;

  return <main className="v3-detail-shell">
    <header className="v3-detail-header"><Link className="v3-brand" to="/request">VORA <small>Creative Studio</small></Link><span className="v3-contract">V3 / CAROUSEL WORKFLOW</span><button className="v3-quiet-button" type="button" onClick={refresh}>새로고침</button></header>
    {error && <p className="v3-error" role="alert">{error}</p>}
    <section className="v3-hero-card"><div><p className="v3-eyebrow">YOUR TINY CREATIVE TEAM</p><h1>아이디어가 콘텐츠가 되는 과정</h1><p>실제 Workflow 상태와 카드뉴스 결과를 한 곳에서 확인하세요.</p></div><div className="v3-orbit-art" aria-hidden="true"><span>✦</span><i /><b>VORA</b></div></section>
    <section className="v3-status-card" aria-labelledby="v3-status-title"><div className="v3-section-heading"><div><p className="v3-eyebrow">WORKFLOW STATUS</p><h2 id="v3-status-title">제작 진행 상황</h2></div><div className={`v3-status-badge ${statusTone(workflow.status)}`}>{statusLabel(workflow.status)}</div></div><p className="v3-meta">Workflow #{workflow.workflow_execution_id} · Revision {workflow.current_revision} · 현재 노드 {workflow.current_node_type ?? "-"}</p><ol className="v3-stage-list">{summaryStages.map((stage) => { const status = stageStatus(workflow, stage.nodes, reusedNodes); return <li key={stage.label} className={statusTone(status)}><span>{stage.label}</span><strong>{status === "REUSED" ? "REUSED" : statusLabel(status)}</strong><small>{stage.nodes.join(" · ")}</small></li>; })}</ol></section>
    <V3ReuseNotice reusedNodes={reusedNodes} />
    <div className="v3-content-grid"><V3NodeList workflow={workflow} reusedNodes={reusedNodes} /><V3Slides workflow={workflow} /></div>
    <V3Approval workflow={workflow} onUpdated={refresh} />
    <V3Publication workflow={workflow} />
    <p className="v3-back-link"><button className="v3-quiet-button" type="button" onClick={() => navigate("/request")}>← 새 카드뉴스 만들기</button></p>
  </main>;
}

function V3ReuseNotice({ reusedNodes }: { reusedNodes: ReusedNodes }) {
  const entries = Object.entries(reusedNodes);
  if (!entries.length) return null;
  return <p className="v3-reuse-notice">이전 Revision 결과 재사용 (REUSED): {entries.map(([node, value]) => `${node} ← Revision ${value.sourceRevision}`).join(" · ")}</p>;
}

function V3NodeList({ workflow, reusedNodes }: { workflow: V3Workflow; reusedNodes: ReusedNodes }) {
  return <section className="v3-panel" aria-labelledby="v3-nodes-title"><div className="v3-section-heading"><div><p className="v3-eyebrow">SEVEN NODE TRACE</p><h2 id="v3-nodes-title">실제 Workflow 노드</h2></div></div><ol className="v3-node-list">{nodeOrder.map((type) => { const node = workflow.nodes[type]; const reused = reusedNodes[type]; return <li key={type} className={node ? statusTone(node.status) : reused ? "reused" : "pending"}><span className="v3-node-index">{String(nodeOrder.indexOf(type) + 1).padStart(2, "0")}</span><div><strong>{type}</strong><small>{node ? `Revision ${node.revision_no} · Attempt ${node.attempt.current}` : reused ? `이전 Revision ${reused.sourceRevision} 결과 재사용` : "아직 생성되지 않음"}</small></div><em>{reused ? "REUSED" : statusLabel(node?.status)}</em></li>; })}</ol></section>;
}

function V3Image({ asset }: { asset: V3ImageAsset }) {
  const [url, setUrl] = useState<string | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  useEffect(() => {
    const controller = new AbortController(); let objectUrl: string | null = null; let live = true;
    void api.getSlideImageBlob(asset.image_url, controller.signal).then((blob) => { if (!live) return; objectUrl = URL.createObjectURL(blob); setUrl(objectUrl); setState("ready"); }).catch((cause) => { if (live && !(cause instanceof DOMException && cause.name === "AbortError")) setState("error"); });
    return () => { live = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [asset.image_url]);
  if (state === "loading") return <div className="v3-image-placeholder">이미지 불러오는 중…</div>;
  if (state === "error" || !url) return <div className="v3-image-placeholder error">이미지를 불러오지 못했습니다.</div>;
  return <img src={url} alt={`${asset.slide_no}번 카드뉴스`} />;
}

function V3Slides({ workflow }: { workflow: V3Workflow }) {
  const [selected, setSelected] = useState(1);
  const assets = useMemo(() => [...workflow.images].sort((a, b) => a.slide_no - b.slide_no), [workflow.images]);
  const activeSlide = assets.some((asset) => asset.slide_no === selected) ? selected : assets[0]?.slide_no ?? selected;
  const selectedAsset = assets.find((asset) => asset.slide_no === activeSlide);
  return <section className="v3-panel v3-slides-panel" aria-labelledby="v3-slides-title"><div className="v3-section-heading"><div><p className="v3-eyebrow">AI IMAGE OUTPUT</p><h2 id="v3-slides-title">카드뉴스 5장</h2></div><span className="v3-count">{assets.length} / 5</span></div><div className="v3-slide-stage">{selectedAsset ? <V3Image key={selectedAsset.image_url} asset={selectedAsset} /> : <div className="v3-empty-slides"><span aria-hidden="true">✦</span><strong>아직 생성된 이미지가 없습니다</strong><small>IMAGE_GENERATE가 완료되면 실제 FileAsset이 표시됩니다.</small></div>}</div><div className="v3-thumbnails" role="tablist" aria-label="카드뉴스 슬라이드 선택">{[1, 2, 3, 4, 5].map((number) => { const asset = assets.find((item) => item.slide_no === number); return <button key={number} type="button" role="tab" aria-selected={activeSlide === number} className={activeSlide === number ? "selected" : ""} onClick={() => setSelected(number)}>{asset ? <span>{number}</span> : <span className="missing">{number}</span>}</button>; })}</div><p className="v3-image-note">기준 이미지는 생성 입력으로만 사용되며, 위 영역은 Backend에 연결된 AI FileAsset만 표시합니다.</p></section>;
}

function V3Approval({ workflow, onUpdated }: { workflow: V3Workflow; onUpdated: () => Promise<void> }) {
  const [target, setTarget] = useState<"PLAN" | "TEXT" | "IMAGE">("TEXT");
  const [request, setRequest] = useState(""); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const approvalId = workflow.human_approval.node_execution_id;
  const waiting = workflow.human_approval.waiting && approvalId !== null;
  const submit = async (decision: "APPROVED" | "REJECTED") => {
    if (!waiting || approvalId === null) return;
    if (decision === "REJECTED" && !request.trim()) { setError("수정 요청 내용을 입력해주세요."); return; }
    setBusy(true); setError("");
    try { await api.approve(workflow.workflow_execution_id, approvalId, decision, decision === "REJECTED" ? target : undefined, decision === "REJECTED" ? request.trim() : undefined); setRequest(""); await onUpdated(); }
    catch (cause) { setError(apiErrorMessage(cause)); }
    finally { setBusy(false); }
  };
  return <section className="v3-approval-panel" aria-labelledby="v3-approval-title"><div><p className="v3-eyebrow">HUMAN APPROVAL</p><h2 id="v3-approval-title">{waiting ? "검토가 필요합니다" : "승인 단계"}</h2><p>{waiting ? "현재 revision의 유효한 승인 대상만 요청에 사용할 수 있습니다." : `현재 상태: ${statusLabel(workflow.human_approval.status)}`}</p></div>{waiting ? <div className="v3-approval-actions"><button className="v3-primary-button" type="button" disabled={busy} onClick={() => void submit("APPROVED")}>승인</button><div className="v3-revision-form"><label>수정 대상<select value={target} onChange={(event) => setTarget(event.target.value as typeof target)}><option value="PLAN">Plan</option><option value="TEXT">Text</option><option value="IMAGE">Image</option></select></label><textarea value={request} onChange={(event) => setRequest(event.target.value)} placeholder="수정 요청을 입력하세요" rows={3} /><button type="button" disabled={busy} onClick={() => void submit("REJECTED")}>수정 요청</button></div></div> : <span className={`v3-status-badge ${statusTone(workflow.human_approval.status)}`}>{statusLabel(workflow.human_approval.status)}</span>}{error && <p className="v3-error" role="alert">{error}</p>}</section>;
}

function V3Publication({ workflow }: { workflow: V3Workflow }) {
  const publication = workflow.nodes.PUBLICATION;
  const [detail, setDetail] = useState<{ publications: Array<{ platform: string; status: string }> } | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  useEffect(() => { if (publication?.status !== "SUCCEEDED" || workflow.status !== "SUCCEEDED") return; void api.getPublications(workflow.workflow_execution_id).then(setDetail).catch((cause) => setError(apiErrorMessage(cause))); }, [publication?.status, workflow.status, workflow.workflow_execution_id]);
  const publish = async () => { setBusy(true); setError(""); try { const next = await api.publishInstagram(workflow.workflow_execution_id, { platforms: ["INSTAGRAM"], instagram_caption: "", force_republish: false }); setDetail(next); } catch (cause) { setError(apiErrorMessage(cause)); } finally { setBusy(false); } };
  return <section className="v3-publication-panel" aria-labelledby="v3-publication-title"><div><p className="v3-eyebrow">PUBLICATION</p><h2 id="v3-publication-title">게시 준비와 Instagram 게시</h2><p>PUBLICATION은 카드뉴스 자산 준비 단계이며, 실제 Instagram 게시 결과와 별도로 표시됩니다.</p></div><div className="v3-publication-status"><strong>준비 상태: {statusLabel(publication?.status)}</strong>{detail?.publications.map((item) => <span key={item.platform}>{item.platform}: {item.status}</span>)}{publication?.status === "SUCCEEDED" && workflow.status === "SUCCEEDED" && <button className="v3-primary-button" type="button" onClick={() => void publish()} disabled={busy}>{busy ? "게시 요청 중…" : "Instagram 게시 요청"}</button>}</div>{error && <p className="v3-error" role="alert">{error}</p>}</section>;
}
