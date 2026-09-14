import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "./api";
import { ApprovalActions, usePolling } from "./components";
import { useWorkflow } from "./WorkflowContext";
import type { Detail, PlanOutput, ScriptOutput, VideoDetail } from "./types";

export function RequestPage() {
  const navigate = useNavigate(); const [text, setText] = useState(""); const [images, setImages] = useState<File[]>([]); const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const drag = useRef<number | null>(null);
  const addFiles = (files: FileList | null) => { if (!files) return; const next = [...images, ...Array.from(files)]; if (next.length > 6) { setError("이미지는 최대 6장까지 등록할 수 있습니다."); return; } if (next.some((file) => !["image/jpeg", "image/png"].includes(file.type))) { setError("JPG와 PNG 이미지만 등록할 수 있습니다."); return; } setError(""); setImages(next); };
  const submit = async () => { if (!text.trim()) { setError("숏폼 요청을 입력해주세요."); return; } setBusy(true); setError(""); try { const result = await api.createWorkflow(text, images); navigate(`/workflows/${result.workflow_execution_id}/plan`); } catch (e) { setError(e instanceof Error ? e.message : "요청 생성에 실패했습니다."); } finally { setBusy(false); } };
  return <main className="request-page"><section className="request-card"><p className="eyebrow">VORA</p><h1>무엇을 만들어볼까요?</h1><p>원하는 영상의 목적과 분위기를 자연어로 알려주세요.</p><textarea aria-label="숏폼 요청" value={text} onChange={(event) => setText(event.target.value)} placeholder="예: 이 제품 사진으로 20대 러너를 위한 활기찬 30초 광고 영상을 만들어줘" rows={7} />
    <label className="drop-zone" onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); addFiles(event.dataTransfer.files); }}><input type="file" accept="image/jpeg,image/png" multiple onChange={(event) => addFiles(event.target.files)} />이미지를 끌어놓거나 선택하세요 · 최대 6장</label>
    {images.length > 0 && <ol className="image-list">{images.map((image, index) => <li key={`${image.name}-${index}`} draggable onDragStart={() => { drag.current = index; }} onDragOver={(event) => event.preventDefault()} onDrop={() => { if (drag.current === null || drag.current === index) return; const next = [...images]; const [moved] = next.splice(drag.current, 1); next.splice(index, 0, moved); setImages(next); drag.current = null; }}><span>↕</span>{image.name}<button aria-label={`${image.name} 삭제`} onClick={() => setImages(images.filter((_, itemIndex) => itemIndex !== index))}>삭제</button></li>)}</ol>}
    {error && <p className="error">{error}</p>}<button className="primary create" onClick={() => void submit()} disabled={busy}>{busy ? "기획 생성 중…" : "숏폼 만들기"}</button>
  </section></main>;
}

function totalDuration(scenes: Array<{ duration_seconds: number }>) { return scenes.reduce((sum, scene) => sum + scene.duration_seconds, 0); }
function Waiting({ label }: { label: string }) { return <section className="state-card"><h1>{label} 생성 중</h1><p>생성 결과를 확인하고 있어요.</p></section>; }

export function PlanPage() {
  const { workflowExecutionId } = useWorkflow(); const load = useCallback(() => api.getPlan(workflowExecutionId), [workflowExecutionId]); const { data, error, refetch: refetchPlan } = usePolling<Detail<PlanOutput>>(load);
  if (error) return <p className="error">{error}</p>; if (!data?.output) return <Waiting label="기획" />;
  const plan = data.output; const fields = [["Concept", plan.concept], ["Hook", plan.hook], ["Key Message", plan.key_message], ["CTA", plan.cta], ["Visual Style", plan.visual_style], ["BGM Direction", plan.bgm_direction]];
  return <section className="page"><p className="eyebrow">기획</p><h1>영상의 방향을 확인해주세요.</h1><dl className="detail-grid">{fields.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}<div><dt>총 영상 길이</dt><dd>{totalDuration(plan.scenes)}초</dd></div><div><dt>Scene 개수</dt><dd>{plan.scenes.length}개</dd></div></dl><ApprovalActions nodeKey="content_planning" onRevisionCompleted={refetchPlan} /></section>;
}

export function ScriptPage() {
  const { workflowExecutionId } = useWorkflow(); const load = useCallback(() => api.getScript(workflowExecutionId), [workflowExecutionId]); const { data, error, refetch: refetchScript } = usePolling<Detail<ScriptOutput>>(load); const planLoad = useCallback(() => api.getPlan(workflowExecutionId), [workflowExecutionId]); const { data: planData } = usePolling<Detail<PlanOutput>>(planLoad);
  if (error) return <p className="error">{error}</p>; if (!data?.output) return <Waiting label="대본" />;
  const durations = planData?.output?.scenes.map((scene) => scene.duration_seconds) ?? [];
  const ranges = data.output.scenes.map((_, index) => ({ start: durations.slice(0, index).reduce((sum, value) => sum + value, 0), end: durations.slice(0, index + 1).reduce((sum, value) => sum + value, 0) }));
  return <section className="page"><p className="eyebrow">대본</p><h1>장면 흐름을 확인해주세요.</h1><p>총 영상 길이 {totalDuration(planData?.output?.scenes ?? [])}초</p><ol className="timeline">{data.output.scenes.map((scene, index) => <li key={scene.planning_scene_id}><strong>{ranges[index].start}~{ranges[index].end}초</strong><p>Narration: {scene.narration ?? "—"}</p><p>Subtitle: {scene.subtitle ?? "—"}</p></li>)}</ol><ApprovalActions nodeKey="script_generation" onRevisionCompleted={refetchScript} /></section>;
}

const stages = ["QUEUED", "SCENE_GENERATION", "TTS_GENERATION", "COMPOSING", "COMPLETED"] as const;
const stageLabel: Record<(typeof stages)[number], string> = { QUEUED: "작업 준비", SCENE_GENERATION: "Scene 생성", TTS_GENERATION: "음성 생성", COMPOSING: "영상 합성", COMPLETED: "완료" };

export function VideoPage() {
  const { workflowExecutionId, refetch } = useWorkflow(); const load = useCallback(() => api.getVideo(workflowExecutionId), [workflowExecutionId]); const { data, error, refetch: refetchVideo } = usePolling<VideoDetail>(load); const planLoad = useCallback(() => api.getPlan(workflowExecutionId), [workflowExecutionId]); const { data: planData } = usePolling<Detail<PlanOutput>>(planLoad); const [stage, setStage] = useState<typeof stages[number]>("QUEUED"); const [message, setMessage] = useState("작업을 준비하고 있어요."); const [retrying, setRetrying] = useState(false);
  useEffect(() => { if (!data?.node || !["RUNNING", "RETRYING"].includes(data.node.status)) return undefined; const source = new EventSource(api.eventUrl(workflowExecutionId)); source.onmessage = (event) => { const payload = JSON.parse(event.data) as { stage?: string; message?: string }; if (stages.includes(payload.stage as typeof stages[number])) setStage(payload.stage as typeof stages[number]); if (payload.message) setMessage(payload.message); if (payload.stage === "COMPLETED" || payload.stage === "FAILED") { source.close(); void refetch(); refetchVideo(); } }; source.onerror = () => source.close(); return () => source.close(); }, [data?.node, refetch, refetchVideo, workflowExecutionId]);
  const retry = async () => { setRetrying(true); try { await api.retry(workflowExecutionId); await refetch(); refetchVideo(); } finally { setRetrying(false); } };
  const detail = data?.node;
  if (error) return <p className="error">{error}</p>;
  if (detail?.status === "FAILED") return <section className="state-card"><h1>영상 생성에 실패했습니다.</h1><p>잠시 후 다시 시도해주세요.</p><button className="primary" onClick={() => void retry()} disabled={retrying}>{retrying ? "다시 시도 중…" : "다시 시도"}</button></section>;
  if (!data?.video) return <section className="page"><p className="eyebrow">영상</p><h1>영상 생성 중</h1>{detail?.status === "RETRYING" && <p className="notice">영상 생성 중 일시적인 문제가 발생했습니다. 자동으로 다시 시도하고 있어요. ({detail.attempt.current}/{detail.attempt.automatic_max})</p>}<ol className="progress">{stages.map((item) => <li key={item} className={stages.indexOf(item) <= stages.indexOf(stage) ? "done" : ""}>{stages.indexOf(item) < stages.indexOf(stage) ? "✓" : stages.indexOf(item) === stages.indexOf(stage) ? "●" : "○"} {stageLabel[item]}</li>)}</ol><p>{message}</p></section>;
  const scenes = planData?.output?.scenes ?? [];
  return <section className="page"><p className="eyebrow">영상</p><h1>최종 영상을 확인해주세요.</h1><video className="player" controls src={api.assetUrl(data.video.stream_url)} /><p>총 길이 {totalDuration(scenes)}초 · Scene 수 {scenes.length}개</p><ApprovalActions nodeKey="video_generation" onRevisionCompleted={refetchVideo} /></section>;
}
