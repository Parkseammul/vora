import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError } from "./api";
import type { Publication, PublicationDetail, SocialConnection, V3ImageAsset, V3NodeType, V3Workflow, V3WorkflowTrace } from "./types";

const nodeOrder: V3NodeType[] = ["CONTENT_PLAN", "TEXT_GENERATE", "TEXT_CHECK", "IMAGE_GENERATE", "IMAGE_CHECK", "HUMAN_APPROVAL", "PUBLICATION"];
const summaryStages: Array<{ label: string; nodes: V3NodeType[] }> = [
  { label: "기획", nodes: ["CONTENT_PLAN"] },
  { label: "글 작성", nodes: ["TEXT_GENERATE", "TEXT_CHECK"] },
  { label: "이미지 제작", nodes: ["IMAGE_GENERATE", "IMAGE_CHECK"] },
  { label: "최종 확인", nodes: ["HUMAN_APPROVAL"] },
  { label: "게시 준비", nodes: ["PUBLICATION"] },
];

type ReusedNode = { sourceRevision: number };
type ReusedNodes = Partial<Record<V3NodeType, ReusedNode>>;

function statusLabel(status: string | null | undefined) {
  const labels: Record<string, string> = {
    PENDING: "대기 중", RUNNING: "진행 중", WAITING: "확인 대기 중",
    WAITING_APPROVAL: "확인 대기 중", SUCCEEDED: "완료", FAILED: "오류",
    SKIPPED: "완료", REUSED: "기존 결과 사용", PUBLISHING: "게시 중",
    PUBLISHED: "게시 완료", DELETED: "삭제됨",
  };
  return labels[status ?? "PENDING"] ?? "대기 중";
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
    if (error.status === 403 || error.status === 404) return "카드뉴스를 찾을 수 없거나 접근 권한이 없습니다.";
    if (error.status === 422) return error.message || "지금은 이 요청을 처리할 수 없습니다.";
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
  const [refreshing, setRefreshing] = useState(false);
  const [refreshNotice, setRefreshNotice] = useState("");
  const [retrying, setRetrying] = useState(false);

  const load = useCallback(async (signal?: AbortSignal): Promise<boolean> => {
    let succeeded = false;
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
        setError("");
        succeeded = true;
      }
      else setError("이 화면에서 볼 수 없는 콘텐츠예요.");
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) setError(apiErrorMessage(cause));
    } finally { setLoading(false); }
    return succeeded;
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

  useEffect(() => {
    if (!refreshNotice) return;
    const timer = window.setTimeout(() => setRefreshNotice(""), 3000);
    return () => window.clearTimeout(timer);
  }, [refreshNotice]);

  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshNotice("");
    const succeeded = await load();
    if (succeeded) setRefreshNotice("새로고침 완료");
    setRefreshing(false);
  };
  const retry = async () => {
    if (!workflow || workflow.status !== "FAILED" || retrying) return;
    setRetrying(true);
    setError("");
    try {
      await api.retry(workflowExecutionId);
      await load();
    } catch (cause) {
      setError(apiErrorMessage(cause));
    } finally {
      setRetrying(false);
    }
  };
  if (loading && !workflow) return <main className="v3-detail-shell"><p className="v3-state">카드뉴스를 불러오는 중이에요…</p></main>;
  if (error && !workflow) return <main className="v3-detail-shell"><p className="v3-error" role="alert">{error}</p><Link className="v3-secondary-button" to="/request">스튜디오로 돌아가기</Link></main>;
  if (!workflow) return null;

  return <main className="v3-detail-shell">
    <header className="v3-detail-header">
      <Link className="v3-brand" to="/request" aria-label="VORA 홈"><span className="v3-brand-mark" aria-hidden="true">✳</span><span>VORA<small>Creative Studio</small></span></Link>
      <span className="v3-header-context">카드뉴스 스튜디오</span>
      <div className="v3-header-actions"><span className="v3-refresh-notice" role="status" aria-live="polite">{refreshNotice}</span><button className="v3-icon-button" type="button" aria-label={refreshing ? "새로고침 중…" : "새로고침"} title="새로고침" disabled={refreshing} onClick={() => void refresh()}><span className={refreshing ? "v3-refresh-icon is-spinning" : "v3-refresh-icon"} aria-hidden="true">↻</span></button></div>
    </header>
    <div className="v3-page-content">
      {error && <p className="v3-error" role="alert">{error}</p>}
      <section className="v3-hero-card" aria-labelledby="v3-page-title">
        <div><p className="v3-eyebrow">YOUR TINY CREATIVE TEAM <span aria-hidden="true">/</span> CARD NEWS</p><h1 id="v3-page-title">아이디어가 콘텐츠가 되는 순간</h1><p>기획부터 디자인까지, VORA와 함께 완성해 보세요.</p><a className="v3-jump-link" href="#v3-slides-title">카드뉴스 보기 <span aria-hidden="true">↗</span></a></div>
        <span className="v3-hero-spark" aria-hidden="true">✳</span>
      </section>
      <section className="v3-progress" aria-labelledby="v3-status-title">
        <div className="v3-section-heading"><div><p className="v3-eyebrow">THE PROCESS</p><h2 id="v3-status-title">제작 진행 상황</h2></div><span className={`v3-status-badge ${statusTone(workflow.status)}`}>{statusLabel(workflow.status)}</span></div>
        <ol className="v3-stage-list">{summaryStages.map((stage, index) => { const status = stageStatus(workflow, stage.nodes, reusedNodes); const current = status === "RUNNING" || status === "WAITING"; return <li key={stage.label} className={statusTone(status)} aria-label={`${stage.label}, ${stageLabel(status)}`} aria-current={current ? "step" : undefined}><span className="v3-stage-icon" aria-hidden="true">{status === "SUCCEEDED" || status === "REUSED" ? "✓" : String(index + 1).padStart(2, "0")}</span><span className="v3-stage-copy"><strong>{stage.label}</strong><small>{stageLabel(status)}</small></span></li>; })}</ol>
        {workflow.status === "FAILED" && <div className="v3-progress-error"><span>제작 중 문제가 생겼어요. 준비된 결과는 그대로 보관돼요.</span><button className="v3-secondary-button" type="button" disabled={retrying} onClick={() => void retry()}>{retrying ? "재시도 중…" : "다시 시도"}</button></div>}
      </section>
      {workflow.status === "RUNNING" && workflow.current_revision > 0 && <p className="v3-workflow-notice" role="status">{workflow.editing_slides?.length === 1 ? `${workflow.editing_slides[0]}번 슬라이드를 수정하고 있어요.` : "카드뉴스를 수정하고 있어요."}</p>}
      {workflow.status === "WAITING_APPROVAL" && workflow.current_revision > 0 && <p className="v3-workflow-notice" role="status">수정된 카드뉴스를 확인하고 승인해 주세요.</p>}
      <div className="v3-main-grid"><div className="v3-main-column"><V3Slides workflow={workflow} /></div><aside className="v3-side-column" aria-label="검토와 게시">{workflow.status === "SUCCEEDED" && <V3CompletedEdit workflow={workflow} onUpdated={refresh} />}<V3Approval workflow={workflow} onUpdated={refresh} /><V3Publication key={`${workflow.workflow_execution_id}:${workflow.current_revision}`} workflow={workflow} /></aside></div>
      <p className="v3-back-link"><button className="v3-text-button" type="button" onClick={() => navigate("/request")}>← 새 카드뉴스 만들기</button></p>
    </div>
  </main>;
}

function useV3ImageUrl(imageUrl: string | undefined) {
  const [result, setResult] = useState<{ source: string; url: string | null; state: "ready" | "error" } | null>(null);
  useEffect(() => {
    if (!imageUrl) return;
    const controller = new AbortController(); let objectUrl: string | null = null; let live = true;
    void api.getSlideImageBlob(imageUrl, controller.signal).then((blob) => { if (!live) return; objectUrl = URL.createObjectURL(blob); setResult({ source: imageUrl, url: objectUrl, state: "ready" }); }).catch((cause) => { if (live && !(cause instanceof DOMException && cause.name === "AbortError")) setResult({ source: imageUrl, url: null, state: "error" }); });
    return () => { live = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [imageUrl]);
  return result && result.source === imageUrl ? result : { url: null, state: "loading" as const };
}

function V3Image({ asset }: { asset: V3ImageAsset }) {
  const { url, state } = useV3ImageUrl(asset.image_url);
  if (state === "loading") return <div className="v3-image-placeholder">이미지 불러오는 중…</div>;
  if (state === "error" || !url) return <div className="v3-image-placeholder error">이미지를 불러오지 못했습니다.</div>;
  return <img src={url} alt={`${asset.slide_no}번 카드뉴스`} />;
}

function V3ThumbnailImage({ asset }: { asset: V3ImageAsset | undefined }) {
  const { url } = useV3ImageUrl(asset?.image_url);
  return url ? <img src={url} alt="" /> : <span className="v3-thumb-placeholder" aria-hidden="true">✳</span>;
}

function V3Slides({ workflow }: { workflow: V3Workflow }) {
  const [selected, setSelected] = useState(1);
  const [viewPrevious, setViewPrevious] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState("");
  const downloadingRef = useRef(false);
  const previousAssets = workflow.previous_images ?? [];
  const showingPrevious = (viewPrevious || workflow.images.length === 0) && previousAssets.length === 5;
  const assets = [...(showingPrevious ? previousAssets : workflow.images)].sort((a, b) => a.slide_no - b.slide_no);
  const activeSlide = assets.some((asset) => asset.slide_no === selected) ? selected : assets[0]?.slide_no ?? selected;
  const selectedAsset = assets.find((asset) => asset.slide_no === activeSlide);
  const download = async () => {
    if (!selectedAsset || downloadingRef.current) return;
    downloadingRef.current = true;
    setDownloading(true); setDownloadError("");
    try {
      const blob = await api.getSlideImageBlob(selectedAsset.image_url);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url; link.download = selectedAsset.file_name || `vora-slide-${activeSlide}.png`;
      try { link.click(); }
      finally { window.setTimeout(() => URL.revokeObjectURL(url), 0); }
    } catch (cause) { setDownloadError(apiErrorMessage(cause)); }
    finally { downloadingRef.current = false; setDownloading(false); }
  };
  const onSlideKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>, number: number) => {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 1 : event.key === "End" ? 5 : ((number - 1 + (event.key === "ArrowRight" ? 1 : 4)) % 5) + 1;
    setSelected(next);
    event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button")[next - 1]?.focus();
  };
  return <section className="v3-panel v3-slides-panel" aria-labelledby="v3-slides-title">
    <div className="v3-section-heading"><div><p className="v3-eyebrow">THE CANVAS</p><h2 id="v3-slides-title">카드뉴스 미리보기</h2><p className="v3-section-caption">한 장씩 넘겨 보며 디테일까지 확인해 보세요.</p></div><div className="v3-slide-tools"><span className="v3-count">{assets.length} / 5</span>{selectedAsset && <button className="v3-text-button" type="button" disabled={downloading} onClick={() => void download()}>{downloading ? "저장 중…" : "이미지 저장"}</button>}</div></div>
    {previousAssets.length === 5 && workflow.images.length === 5 && <div className="v3-compare-switch" role="group" aria-label="카드뉴스 결과 비교"><button type="button" aria-pressed={!showingPrevious} onClick={() => setViewPrevious(false)}>현재 카드뉴스</button><button type="button" aria-pressed={showingPrevious} onClick={() => setViewPrevious(true)}>이전 카드뉴스</button></div>}
    <div id="v3-slide-panel" className="v3-slide-stage" role="tabpanel" aria-labelledby={`v3-slide-tab-${activeSlide}`}><span className="v3-slide-counter">{String(activeSlide).padStart(2, "0")} / 05</span>{selectedAsset ? <V3Image key={selectedAsset.image_url} asset={selectedAsset} /> : <div className="v3-empty-slides"><span aria-hidden="true">✳</span><strong>아직 준비된 이미지가 없어요</strong><small>제작이 끝나면 이곳에서 확인할 수 있어요.</small></div>}</div>
    <div className="v3-thumbnails" role="tablist" aria-label="카드뉴스 슬라이드 선택">{[1, 2, 3, 4, 5].map((number) => { const asset = assets.find((item) => item.slide_no === number); return <button key={number} id={`v3-slide-tab-${number}`} type="button" role="tab" aria-controls="v3-slide-panel" aria-label={`${number}번 슬라이드`} aria-selected={activeSlide === number} tabIndex={activeSlide === number ? 0 : -1} className={activeSlide === number ? "selected" : ""} onClick={() => setSelected(number)} onKeyDown={(event) => onSlideKeyDown(event, number)}><V3ThumbnailImage asset={asset} /><span className="v3-thumb-number">{number}</span></button>; })}</div>
    {showingPrevious && <p className="v3-image-note" role="status">이전에 완료한 카드뉴스를 보고 있어요.</p>}
    {downloadError && <p className="v3-error" role="alert">{downloadError}</p>}
  </section>;
}

function V3SlideSelection({ selectedSlides, setSelectedSlides, disabled, showSelectAll = false }: { selectedSlides: number[]; setSelectedSlides: (slides: number[]) => void; disabled: boolean; showSelectAll?: boolean }) {
  const summary = selectedSlides.length === 1
    ? `${selectedSlides[0]}번 슬라이드만 새로 만들어요. 나머지 4장은 그대로 유지해요.`
    : selectedSlides.length === 5
      ? "5장 모두 새로 만들어요."
      : `${selectedSlides.length}장만 새로 만들어요. 나머지 ${5 - selectedSlides.length}장은 그대로 유지해요.`;
  return <fieldset>
    <legend>수정할 슬라이드</legend>
    {showSelectAll && <label><input type="checkbox" checked={selectedSlides.length === 5} disabled={disabled} onChange={() => setSelectedSlides([1, 2, 3, 4, 5])} />전체 선택</label>}
    {[1, 2, 3, 4, 5].map((slideNo) => <label key={slideNo}><input type="checkbox" checked={selectedSlides.includes(slideNo)} disabled={disabled || (selectedSlides.length === 1 && selectedSlides.includes(slideNo))} onChange={() => setSelectedSlides(selectedSlides.includes(slideNo) ? selectedSlides.filter((value) => value !== slideNo) : [...selectedSlides, slideNo].sort((left, right) => left - right))} />{slideNo}번</label>)}
    <small>{summary}</small>
    <small>예상 이미지 재생성: {selectedSlides.length}장</small>
  </fieldset>;
}

function stageLabel(status: string): string {
  return status === "REUSED" ? "완료" : statusLabel(status);
}

function V3CompletedEdit({ workflow, onUpdated }: { workflow: V3Workflow; onUpdated: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [target, setTarget] = useState<"PLAN" | "TEXT" | "IMAGE">("IMAGE");
  const [selectedSlides, setSelectedSlides] = useState<number[]>([1, 2, 3, 4, 5]);
  const [previewSlide, setPreviewSlide] = useState(1);
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const close = () => { if (!submitting.current) { setOpen(false); setError(""); } };

  useEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); close(); return; }
      if (event.key !== "Tab") return;
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>("button:not([disabled]), textarea:not([disabled]), select:not([disabled])");
      if (!focusable?.length) return;
      const first = focusable[0]; const last = focusable[focusable.length - 1];
      if (!dialogRef.current?.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (trigger?.isConnected && !trigger.disabled) trigger.focus();
    };
  }, [open]);

  const toggleSlide = (slideNo: number) => {
    setPreviewSlide(slideNo);
    setSelectedSlides((current) => current.includes(slideNo)
      ? (current.length > 1 ? current.filter((number) => number !== slideNo) : current)
      : [...current, slideNo].sort((left, right) => left - right));
  };
  const submit = async () => {
    if (submitting.current || submitted) return;
    if (!feedback.trim()) { setError("수정 요청사항을 입력해 주세요."); return; }
    submitting.current = true;
    setBusy(true); setError("");
    try {
      await api.reviseCompleted(workflow.workflow_execution_id, {
        target,
        ...(target === "IMAGE" ? { slide_numbers: selectedSlides } : {}),
        feedback: feedback.trim(),
      });
      setSubmitted(true);
      setOpen(false);
      await onUpdated();
    } catch (cause) {
      setError(apiErrorMessage(cause));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  };
  const selectedAsset = workflow.images.find((asset) => asset.slide_no === previewSlide);
  const editCount = selectedSlides.length;
  const actionLabel = target === "IMAGE" ? `선택한 ${editCount}장 수정하기` : target === "TEXT" ? "글 수정하기" : "구성 수정하기";
  return <section className="v3-edit-entry" aria-label="완료된 카드뉴스 수정">
    <span className="v3-edit-entry-icon" aria-hidden="true">✳</span>
    <div><strong>조금 더 마음에 들게 바꿔볼까요?</strong><p>완료한 결과는 그대로 두고, 새 수정본을 만들 수 있어요.</p></div>
    <button ref={triggerRef} className="v3-edit-cta" type="button" disabled={submitted} onClick={() => setOpen(true)}>{submitted ? "수정 요청을 확인하고 있어요…" : <>다시 수정하기 <span aria-hidden="true">✦</span></>}</button>
    {open && createPortal(<div className="v3-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}>
      <div ref={dialogRef} className="v3-edit-dialog" role="dialog" aria-modal="true" aria-labelledby="v3-edit-title" aria-describedby="v3-edit-description">
        <header className="v3-edit-dialog-header"><div><p className="v3-eyebrow">MAKE IT YOURS</p><h2 id="v3-edit-title">카드뉴스 수정하기</h2><p id="v3-edit-description">마음에 드는 건 그대로, 바꾸고 싶은 것만.</p></div><button ref={closeRef} className="v3-icon-button" type="button" aria-label="수정 창 닫기" disabled={busy} onClick={close}><span aria-hidden="true">×</span></button></header>
        <div className="v3-edit-dialog-body">
          <section className="v3-edit-canvas" aria-label="현재 카드뉴스 이미지"><div className="v3-edit-canvas-stage">{selectedAsset ? <V3Image key={selectedAsset.image_url} asset={selectedAsset} /> : <span>이미지를 불러올 수 없어요.</span>}<span className="v3-slide-counter">{String(previewSlide).padStart(2, "0")} / 05</span></div><p>지금 완성된 이미지예요. 선택한 슬라이드만 새로 만듭니다.</p></section>
          <div className="v3-edit-controls"><div className="v3-edit-targets" role="group" aria-label="수정 대상"><button type="button" aria-pressed={target === "IMAGE"} disabled={busy} onClick={() => setTarget("IMAGE")}>이미지</button><button type="button" aria-pressed={target === "TEXT"} disabled={busy} onClick={() => setTarget("TEXT")}>글</button><button type="button" aria-pressed={target === "PLAN"} disabled={busy} onClick={() => setTarget("PLAN")}>구성</button></div>
            <div className="v3-edit-selector"><div className="v3-edit-section-heading"><h3>어떤 슬라이드를 바꿀까요?</h3>{target === "IMAGE" && <button type="button" className="v3-text-button" disabled={busy} onClick={() => setSelectedSlides([1, 2, 3, 4, 5])}>전체 선택</button>}</div>
              <div className="v3-edit-thumbnails" role="group" aria-label="수정할 슬라이드 선택">{workflow.images.map((asset) => <button key={asset.slide_no} type="button" className={target === "IMAGE" && selectedSlides.includes(asset.slide_no) ? "is-selected" : ""} aria-label={`${asset.slide_no}번 슬라이드 수정`} aria-pressed={target === "IMAGE" && selectedSlides.includes(asset.slide_no)} disabled={busy || target !== "IMAGE"} onClick={() => toggleSlide(asset.slide_no)}><V3ThumbnailImage asset={asset} /><span className="v3-edit-thumb-number">{asset.slide_no}</span><span className="v3-edit-thumb-check" aria-hidden="true">✓</span></button>)}</div>
              {target !== "IMAGE" && <p className="v3-edit-target-note">글이나 구성을 바꾸면 연결된 이미지를 다시 만들 수 있어요.</p>}
            </div>
            <label className="v3-edit-feedback-label" htmlFor="v3-edit-feedback">어떻게 바꿀까요?</label><textarea id="v3-edit-feedback" value={feedback} disabled={busy} onChange={(event) => setFeedback(event.target.value)} placeholder="예: 3번 이미지 상단에 제목을 추가하고 기존 색감은 유지해 주세요." rows={5} aria-invalid={Boolean(error && !feedback.trim())} />
            {error && <p className="v3-error" role="alert">{error}</p>}
          </div>
        </div>
        <footer className="v3-edit-dialog-footer"><div className="v3-edit-summary" aria-live="polite"><strong>{target === "IMAGE" ? (editCount === 1 ? `${selectedSlides[0]}번 슬라이드만 새로 만들어요.` : `${editCount}장만 새로 만들어요`) : `${target === "TEXT" ? "글" : "구성"}을 새로 만들어요`}</strong><span>{target === "IMAGE" ? (editCount === 5 ? "5장 모두 새롭게 준비돼요." : `나머지 ${5 - editCount}장은 그대로 유지돼요.`) : "기존 결과는 이전 카드뉴스에서 확인할 수 있어요."}</span></div><div className="v3-edit-footer-actions"><button className="v3-text-button" type="button" disabled={busy} onClick={close}>취소</button><button className="v3-primary-button" type="button" disabled={busy} onClick={() => void submit()}>{busy ? "수정 시작 중…" : actionLabel}</button></div></footer>
      </div>
    </div>, document.body)}
  </section>;
}

function V3Approval({ workflow, onUpdated }: { workflow: V3Workflow; onUpdated: () => Promise<void> }) {
  const [target, setTarget] = useState<"PLAN" | "TEXT" | "IMAGE">("TEXT");
  const [selectedSlides, setSelectedSlides] = useState<number[]>([1, 2, 3, 4, 5]);
  const [request, setRequest] = useState("");
  const [revisionOpen, setRevisionOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const approvalId = workflow.human_approval.node_execution_id;
  const waiting = workflow.human_approval.waiting && approvalId !== null;
  const submit = async (decision: "APPROVED" | "REJECTED") => {
    if (!waiting || approvalId === null || submitting.current) return;
    if (decision === "REJECTED" && !request.trim()) { setError("어떻게 바꿀지 알려 주세요."); return; }
    submitting.current = true;
    setBusy(true); setError("");
    try {
      const revisionOptions = decision === "REJECTED" && target === "IMAGE" ? { slideNumbers: selectedSlides, feedback: request.trim() } : undefined;
      await api.approve(workflow.workflow_execution_id, approvalId, decision, decision === "REJECTED" ? target : undefined, decision === "REJECTED" ? request.trim() : undefined, revisionOptions);
      setRequest(""); setRevisionOpen(false);
      await onUpdated();
    } catch (cause) { setError(apiErrorMessage(cause)); }
    finally { submitting.current = false; setBusy(false); }
  };
  return <section className="v3-approval-panel" aria-labelledby="v3-approval-title">
    <div className="v3-section-heading"><div><p className="v3-eyebrow">YOUR CALL</p><h2 id="v3-approval-title">최종 확인</h2></div><span className={`v3-status-badge ${statusTone(workflow.human_approval.status)}`}>{statusLabel(workflow.human_approval.status)}</span></div>
    <p className="v3-section-caption">{waiting ? "이미지를 살펴보고, 마음에 들면 승인해 주세요." : workflow.human_approval.status === "SUCCEEDED" ? "확인까지 마쳤어요. 이제 게시를 준비할 수 있어요." : "이미지가 준비되면 여기서 확인할 수 있어요."}</p>
    {waiting && <><div className="v3-review-actions"><button className="v3-primary-button" type="button" disabled={busy} onClick={() => void submit("APPROVED")}>{busy && !revisionOpen ? "처리 중…" : "이대로 승인하기"}</button><button className="v3-secondary-button" type="button" disabled={busy} aria-expanded={revisionOpen} onClick={() => { setRevisionOpen((value) => !value); setError(""); }}>수정 요청</button></div>
      {revisionOpen && <div className="v3-revision-form"><label htmlFor="v3-revision-target">수정할 부분</label><select id="v3-revision-target" value={target} disabled={busy} onChange={(event) => setTarget(event.target.value as typeof target)}><option value="PLAN">구성</option><option value="TEXT">글</option><option value="IMAGE">이미지</option></select>{target === "IMAGE" && <V3SlideSelection selectedSlides={selectedSlides} setSelectedSlides={setSelectedSlides} disabled={busy} />}<label htmlFor="v3-revision-request">어떻게 바꿀까요?</label><textarea id="v3-revision-request" value={request} disabled={busy} onChange={(event) => setRequest(event.target.value)} placeholder="바꾸고 싶은 내용을 알려 주세요" rows={4} /><button className="v3-secondary-button" type="button" disabled={busy} onClick={() => void submit("REJECTED")}>{busy ? "요청 중…" : "수정 요청 보내기"}</button></div>}
    </>}
    {error && <p className="v3-error" role="alert">{error}</p>}
  </section>;
}

function currentInstagramPublication(detail: PublicationDetail | null, revision: number): Publication | undefined {
  return detail?.publications.find((item) => item.platform === "INSTAGRAM" && item.revision_no === revision);
}

function instagramPostUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || !["instagram.com", "www.instagram.com"].includes(url.hostname) || url.username || url.password || !/^\/(p|reel|tv)\/[^/]+\/?$/.test(url.pathname)) return null;
    return url.href;
  } catch { return null; }
}

// A stalled response must not leave the publication controls locked indefinitely.
async function boundedPublicationRequest<T>(run: (signal: AbortSignal) => Promise<T>, timeoutMs = 15000): Promise<T> {
  const controller = new AbortController();
  let timer: number | undefined;
  try {
    return await Promise.race([
      run(controller.signal),
      new Promise<never>((_, reject) => {
        timer = window.setTimeout(() => { controller.abort(); reject(new Error("게시 정보 확인 시간이 초과됐어요.")); }, timeoutMs);
      }),
    ]);
  } finally { if (timer !== undefined) window.clearTimeout(timer); }
}

function V3Publication({ workflow }: { workflow: V3Workflow }) {
  const workflowId = workflow.workflow_execution_id;
  const revision = workflow.current_revision;
  const publicationNode = workflow.nodes.PUBLICATION;
  const ready = workflow.status === "SUCCEEDED" && publicationNode?.status === "SUCCEEDED" && workflow.images.length === 5 && [1, 2, 3, 4, 5].every((slide) => workflow.images.some((asset) => asset.slide_no === slide));
  const uncertaintyKey = `vora.publication-uncertain:${workflowId}:${revision}`;
  const [detail, setDetail] = useState<PublicationDetail | null>(null);
  const [connections, setConnections] = useState<SocialConnection[] | null>(null);
  const [caption, setCaption] = useState("");
  const [selectedSlide, setSelectedSlide] = useState(1);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");
  const [uncertain, setUncertain] = useState(() => window.sessionStorage.getItem(uncertaintyKey) === "1");
  const postingRef = useRef(false);
  const statusRef = useRef(false);
  const infoRef = useRef(false);
  const pollsRef = useRef(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const applyStatus = useCallback((next: PublicationDetail) => {
    setDetail(next);
    setCaption((value) => value || next.draft.instagram_caption);
    if (currentInstagramPublication(next, revision)) {
      window.sessionStorage.removeItem(uncertaintyKey);
      setUncertain(false);
    }
  }, [revision, uncertaintyKey]);
  const refreshStatus = useCallback(async () => {
    if (statusRef.current) return;
    statusRef.current = true;
    setChecking(true);
    try { applyStatus(await boundedPublicationRequest((signal) => api.getPublications(workflowId, signal))); setError(""); }
    catch { setError("게시 상태를 확인하지 못했어요. 잠시 후 다시 확인해 주세요."); }
    finally { statusRef.current = false; setChecking(false); }
  }, [applyStatus, workflowId]);

  const loadInfo = useCallback(async () => {
    if (infoRef.current) return;
    infoRef.current = true;
    setLoading(true);
    try {
      const [next, accounts] = await Promise.all([
        boundedPublicationRequest((signal) => api.getPublications(workflowId, signal)),
        boundedPublicationRequest((signal) => api.connections(signal)),
      ]);
      applyStatus(next); setConnections(accounts.connections); setError("");
    } catch {
      setError("게시 정보나 계정 연결 상태를 확인하지 못했어요. 다시 불러와 주세요.");
    } finally { infoRef.current = false; setLoading(false); }
  }, [applyStatus, workflowId]);

  useEffect(() => {
    if (!ready) return undefined;
    let active = true;
    queueMicrotask(() => { if (active) void loadInfo(); });
    return () => { active = false; };
  }, [ready, loadInfo]);

  const current = currentInstagramPublication(detail, revision);
  const unknownHistory = detail?.publications.some((item) => item.platform === "INSTAGRAM" && item.revision_no == null) ?? false;
  const pending = current?.status === "PENDING" || current?.status === "PUBLISHING";
  useEffect(() => {
    if (!ready || (!pending && !uncertain)) return undefined;
    const timer = window.setInterval(() => {
      if (pollsRef.current >= 6) { window.clearInterval(timer); return; }
      pollsRef.current += 1;
      void refreshStatus();
    }, 15000);
    return () => window.clearInterval(timer);
  }, [ready, pending, uncertain, refreshStatus]);

  useEffect(() => {
    if (!open) return undefined;
    const trigger = triggerRef.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); setOpen(false); return; }
      if (event.key !== "Tab") return;
      const focusable = dialogRef.current?.querySelectorAll<HTMLElement>("button:not([disabled]), textarea:not([disabled])");
      if (!focusable?.length) return;
      const first = focusable[0]; const last = focusable[focusable.length - 1];
      if (!dialogRef.current?.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (trigger?.isConnected && !trigger.disabled) trigger.focus();
      else document.querySelector<HTMLButtonElement>("[data-publication-status-action]")?.focus();
    };
  }, [open]);

  const connected = connections?.find((item) => item.platform === "INSTAGRAM" && item.status === "CONNECTED");
  const canRequest = ready && !loading && detail !== null && connections !== null && connected !== undefined && !current && !unknownHistory && !uncertain && !busy;
  const close = () => setOpen(false);
  const publish = async () => {
    if (!open || !canRequest || postingRef.current || !caption.trim()) return;
    postingRef.current = true; setBusy(true); setError("");
    let postStarted = false;
    try {
      // Recheck the durable publication and connection immediately before crossing the Meta boundary.
      const [latest, accounts] = await Promise.all([
        boundedPublicationRequest((signal) => api.getPublications(workflowId, signal)),
        boundedPublicationRequest((signal) => api.connections(signal)),
      ]);
      applyStatus(latest); setConnections(accounts.connections);
      if (latest.publications.some((item) => item.platform === "INSTAGRAM" && (item.revision_no === revision || item.revision_no == null))) { setOpen(false); return; }
      if (!accounts.connections.some((item) => item.platform === "INSTAGRAM" && item.status === "CONNECTED")) { setOpen(false); setError("Instagram 계정을 연결한 뒤 다시 확인해 주세요."); return; }
      if (!ready) return;
      postStarted = true;
      window.sessionStorage.setItem(uncertaintyKey, "1");
      setUncertain(true);
      // A timed-out POST may still have reached the server; never issue an automatic second POST.
      const next = await boundedPublicationRequest(() => api.publishInstagram(workflowId, { platforms: ["INSTAGRAM"], instagram_caption: caption.trim(), force_republish: false }), 20000);
      applyStatus({ ...latest, publications: [...next.publications, ...latest.publications.filter((item) => !next.publications.some((created) => created.id === item.id))] });
      setOpen(false);
      pollsRef.current = 0;
    } catch (cause) {
      if (postStarted) {
        setOpen(false);
        setError("게시 요청 결과가 불확실해요. 상태를 확인 중이며 자동으로 다시 게시하지 않아요.");
        await refreshStatus();
      } else setError(cause instanceof Error && cause.message.includes("초과") ? "게시 정보를 확인하지 못했어요. 잠시 후 다시 시도해 주세요." : apiErrorMessage(cause));
    } finally { postingRef.current = false; setBusy(false); }
  };
  const selectedAsset = workflow.images.find((asset) => asset.slide_no === selectedSlide);
  const postUrl = current?.status === "PUBLISHED" ? instagramPostUrl(current.external_post_url) : null;
  return <section className="v3-publication-panel" aria-labelledby="v3-publication-title">
    <div className="v3-section-heading"><div><p className="v3-eyebrow">WHEN YOU'RE READY</p><h2 id="v3-publication-title">게시 준비</h2></div><span className={`v3-status-badge ${statusTone(publicationNode?.status)}`}>{statusLabel(publicationNode?.status)}</span></div>
    <p className="v3-section-caption">준비된 카드뉴스를 확인하고, 원할 때 Instagram에 게시해 보세요. 자동으로 게시되지는 않아요.</p>
    {ready && <div className="v3-publication-status" aria-live="polite">
      {loading ? <span>게시 상태를 확인하고 있어요…</span> : current?.status === "PUBLISHED" ? <div className="v3-publish-result is-success"><strong>It's live! ✳</strong><p>카드뉴스가 Instagram에 게시됐어요.</p>{postUrl ? <a href={postUrl} target="_blank" rel="noopener noreferrer">Instagram에서 보기 ↗</a> : <small>게시물 링크는 아직 제공되지 않았어요.</small>}</div> : current?.status === "FAILED" ? <div className="v3-publish-result is-failed"><strong>앗, 게시를 완료하지 못했어요.</strong><p>게시 상태를 확인한 뒤 다시 시도해 주세요. 자동 재게시나 수동 재게시 기능은 제공하지 않아요.</p></div> : current?.status === "PUBLISHING" ? <div className="v3-publish-result"><strong>Instagram에 올리는 중이에요 ✦</strong><p>결과를 확인할 때까지 다시 요청하지 않아요.</p></div> : current?.status === "PENDING" ? <div className="v3-publish-result"><strong>게시 요청을 접수했어요.</strong><p>Instagram 게시 상태를 확인하고 있어요.</p></div> : current ? <span>게시 상태를 확인해 주세요. 추가 게시 요청은 보내지 않아요.</span> : uncertain ? <span>이전 게시 요청의 결과를 확인 중이에요. 자동으로 다시 요청하지 않아요.</span> : unknownHistory ? <span>게시 이력을 확인할 수 없어 새 요청을 보내지 않아요.</span> : <span>카드뉴스 {workflow.images.length}장이 게시 준비를 마쳤어요.</span>}
      {detail?.publications.filter((item) => item.platform === "INSTAGRAM" && item.revision_no !== revision).map((item) => <span key={item.id}>이전 카드뉴스 · Instagram {statusLabel(item.status)}</span>)}
      {connections !== null && !connected && <p className="v3-publish-connect">연결된 Instagram 계정이 없어요. <Link to="/settings/social">계정 연결하기 ↗</Link></p>}
      {!current && !unknownHistory && (!uncertain || busy) && <button ref={triggerRef} className="v3-publish-cta" type="button" disabled={!canRequest} onClick={() => setOpen(true)}><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="1" fill="currentColor" stroke="none"/></svg><span>{busy ? "게시 요청 확인 중…" : "Instagram에 게시하기 ↗"}</span></button>}
      {(pending || uncertain) && <button data-publication-status-action className="v3-text-button" type="button" disabled={checking} onClick={() => void refreshStatus()}>{checking ? "상태 확인 중…" : "게시 상태 다시 확인"}</button>}
      {!loading && error && !busy && !current && !uncertain && <button className="v3-text-button" type="button" onClick={() => void loadInfo()}>게시 정보 다시 불러오기</button>}
      {busy && <p className="v3-publish-progress">게시 요청을 확인 중이에요. 창을 닫아도 게시 요청은 취소되지 않아요.</p>}
    </div>}
    {error && <p className="v3-error" role="alert">{error}</p>}
    {open && createPortal(<div className="v3-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) close(); }}><div ref={dialogRef} className="v3-publish-dialog" role="dialog" aria-modal="true" aria-labelledby="v3-publish-title" aria-describedby="v3-publish-description">
      <header className="v3-publish-dialog-header"><div><p className="v3-eyebrow">READY TO GO LIVE ✦</p><h2 id="v3-publish-title">이 콘텐츠, 세상에 보여줄까? ✨</h2><p id="v3-publish-description">카드뉴스 5장이 준비됐어요. 연결된 Instagram 계정에 게시할까요?</p></div><button ref={closeRef} className="v3-icon-button" type="button" aria-label="게시 창 닫기" onClick={close}>×</button></header>
      <div className="v3-publish-dialog-body"><section className="v3-publish-preview" aria-label="게시할 카드뉴스 이미지"><div className="v3-publish-preview-stage">{selectedAsset && <V3Image key={selectedAsset.image_url} asset={selectedAsset} />}<span className="v3-slide-counter">{selectedSlide} / 5</span></div><div className="v3-publish-thumbnails" role="group" aria-label="게시 이미지 선택">{workflow.images.map((asset) => <button key={asset.slide_no} type="button" aria-label={`${asset.slide_no}번 게시 이미지 보기`} aria-pressed={selectedSlide === asset.slide_no} disabled={busy} onClick={() => setSelectedSlide(asset.slide_no)}><V3ThumbnailImage asset={asset} /></button>)}</div><p>총 5장의 현재 카드뉴스가 게시돼요.</p></section>
      <div className="v3-publish-details"><span className="v3-publish-detail-label">PUBLISH TO</span><strong>Instagram · {connected?.account_name || "연결된 계정"}</strong><label htmlFor="v3-publish-caption">게시될 캡션</label><textarea id="v3-publish-caption" value={caption} disabled={busy} onChange={(event) => setCaption(event.target.value)} rows={6} /><p className="v3-publish-warning">{busy ? "창을 닫아도 게시 요청은 취소되지 않아요. 게시 상태는 이 화면에서 계속 확인할 수 있어요." : "게시하기를 누르면 연결된 Instagram 계정에 콘텐츠가 공개됩니다. 아직 게시하고 싶지 않다면 창을 닫아 주세요."}</p>{error && <p className="v3-error" role="alert">{error}</p>}</div></div>
      <footer className="v3-publish-dialog-footer"><button className="v3-text-button" type="button" onClick={close}>{busy ? "창만 닫기" : "아직 아니야"}</button><button className="v3-primary-button" type="button" disabled={busy || !caption.trim()} onClick={() => void publish()}>{busy ? "게시 요청 중…" : "응, 게시할래 ↗"}</button></footer>
    </div></div>, document.body)}
  </section>;
}
