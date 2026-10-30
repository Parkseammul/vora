import { useCallback, useEffect, useRef, useState, type ChangeEvent, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api, getConfiguredRulesVersionId } from "./api";
import { useAuth } from "./auth";
import type { RecentWorkflow } from "./types";

const steps = ["기획", "글 작성", "이미지", "최종 확인", "게시 준비"];
const loginDraftKey = "vora.login-draft.request-text";

export function DashboardPage() {
  const navigate = useNavigate();
  const { state: authState, user } = useAuth();
  const authUserId = user?.profile.sub ?? user?.profile.email;
  const [recentResult, setRecentResult] = useState<{ owner: string | undefined; items: RecentWorkflow[] } | null>(null);
  const [recentLoading, setRecentLoading] = useState(false);
  const [recentFailure, setRecentFailure] = useState<{ owner: string | undefined; message: string } | null>(null);
  const recentProjects = authState === "authenticated" && recentResult?.owner === authUserId ? recentResult?.items ?? null : null;
  const recentError = authState === "authenticated" && recentFailure?.owner === authUserId ? recentFailure?.message ?? "" : "";
  const [openingRecent, setOpeningRecent] = useState(false);
  const [showRecentHeaderError, setShowRecentHeaderError] = useState(false);
  const recentRequest = useRef<Promise<RecentWorkflow[]> | null>(null);
  const recentController = useRef<AbortController | null>(null);
  const openingRecentRef = useRef(false);
  const loadRecent = useCallback((signal?: AbortSignal) => {
    if (recentRequest.current) return recentRequest.current;
    setRecentLoading(true); setRecentFailure(null);
    const request = api.getRecentWorkflows(5, undefined, signal)
      .then((response) => {
        if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
        setRecentResult({ owner: authUserId, items: response.items });
        return response.items;
      })
      .catch((cause) => {
        if (!signal?.aborted && !(cause instanceof DOMException && cause.name === "AbortError")) {
          setRecentResult(null);
          setRecentFailure({ owner: authUserId, message: recentProjectsError(cause) });
        }
        throw cause;
      })
      .finally(() => { if (recentRequest.current === request) { recentRequest.current = null; setRecentLoading(false); } });
    recentRequest.current = request;
    return request;
  }, [authUserId]);
  useEffect(() => {
    if (authState !== "authenticated") {
      recentController.current?.abort();
      recentController.current = null;
      recentRequest.current = null;
      return undefined;
    }
    const controller = new AbortController();
    recentController.current = controller;
    const timer = window.setTimeout(() => { void loadRecent(controller.signal).catch(() => {}); }, 0);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
      if (recentController.current === controller) recentController.current = null;
      recentRequest.current = null;
    };
  }, [authState, authUserId, loadRecent]);
  const recentBusy = recentLoading || (authState === "authenticated" && recentProjects === null && !recentError);
  const openLatestProject = async () => {
    if (authState !== "authenticated") { navigate("/login?returnTo=%2Frequest"); return; }
    if (openingRecentRef.current || recentBusy || recentRequest.current) return;
    openingRecentRef.current = true;
    setOpeningRecent(true); setShowRecentHeaderError(false);
    try {
      const projects = recentProjects ?? await loadRecent(recentController.current?.signal);
      if (projects.length) navigate(`/workflows/${projects[0].workflow_execution_id}`);
      else document.getElementById("recent-projects")?.scrollIntoView({ behavior: "smooth" });
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) setShowRecentHeaderError(true);
    } finally { openingRecentRef.current = false; setOpeningRecent(false); }
  };
  const [restoredDraft] = useState(() => window.sessionStorage.getItem(loginDraftKey) !== null);
  const [brief, setBrief] = useState(() => {
    const saved = window.sessionStorage.getItem(loginDraftKey) ?? "";
    window.sessionStorage.removeItem(loginDraftKey);
    return saved;
  });
  const [image, setImage] = useState<File | null>(null);
  const [imageUrl, setImageUrl] = useState<string | null>(null);
  const [selectedSlide, setSelectedSlide] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => () => { if (imageUrl) URL.revokeObjectURL(imageUrl); }, [imageUrl]);

  const onImage = (file: File | undefined) => {
    if (!file) return;
    if (!["image/png", "image/jpeg"].includes(file.type)) { setError("JPG 또는 PNG 이미지만 업로드할 수 있어요."); return; }
    setImage(file);
    setImageUrl(URL.createObjectURL(file));
    setSelectedSlide(0);
    setError("");
  };

  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    if (!brief.trim()) { setError("콘텐츠 주제를 입력해주세요."); return; }
    if (authState !== "authenticated") {
      window.sessionStorage.setItem(loginDraftKey, brief.trim());
      navigate("/login?returnTo=%2Frequest");
      return;
    }
    if (!image) { setError("현재 생성 API는 기준 이미지 1장을 필요로 합니다."); return; }
    const rulesVersionId = getConfiguredRulesVersionId();
    if (rulesVersionId === null) { setError("VORA 규칙 버전이 설정되지 않았습니다."); return; }
    setBusy(true); setError("");
    try {
      const result = await api.createWorkflow(brief.trim(), image, "Instagram 카드뉴스 제작", rulesVersionId);
      navigate(`/workflows/${result.workflow_execution_id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "콘텐츠 생성 요청에 실패했습니다.");
    } finally { setBusy(false); }
  };

  return <div className="studio-shell studio-shell--home">
    <StudioHeader onOpenRecent={() => void openLatestProject()} recentBusy={recentBusy || openingRecent} recentError={showRecentHeaderError ? recentError : ""} />
    <main className="studio-main">
      <section className="hero-section" aria-labelledby="hero-title">
        <div className="hero-copy"><span className="hero-edition">CREATIVE STUDIO <span aria-hidden="true">/</span> 01</span><p className="hero-eyebrow">✦ YOUR TINY CREATIVE TEAM</p><h1 id="hero-title">당신의 아이디어가<br /><span>콘텐츠가 되는 순간.</span></h1><p className="hero-subtitle">혼자 일해도, 작은 크리에이티브 팀이<br className="desktop-break" /> 함께하는 것처럼.</p><a className="hero-cta" href="#create-studio">카드뉴스 만들기 <span aria-hidden="true">↗</span></a><div className="hero-promise"><span className="hero-promise-mark" aria-hidden="true">✳</span><span>아이디어 한 줄에서<br /><strong>다섯 장의 이야기까지.</strong></span></div></div>
        <HeroScene />
      </section>
      <div className="studio-grid">
        <CreateForm brief={brief} setBrief={setBrief} image={image} onImage={onImage} onCreate={create} busy={busy} error={error} restoredDraft={restoredDraft} />
        <PreviewPanel imageUrl={imageUrl} selectedSlide={selectedSlide} setSelectedSlide={setSelectedSlide} />
        <WorkflowSummary />
        <RecentProjects authenticated={authState === "authenticated"} projects={recentProjects ?? []} loading={recentBusy} error={recentError} onRetry={() => { void loadRecent(recentController.current?.signal).catch(() => {}); }} />
      </div>
    </main>
  </div>;
}

function StudioHeader({ onOpenRecent, recentBusy, recentError }: { onOpenRecent: () => void; recentBusy: boolean; recentError: string }) {
  const { user, state, logout } = useAuth();
  const accountName = user?.profile.email || "내 계정";
  return <header className="studio-header"><div className="studio-header-inner">
    <Link to="/request" className="studio-logo" aria-label="VORA 홈"><span className="logo-mark" aria-hidden="true">✳</span><span>VORA<small>Creative Studio</small></span></Link>
    <nav className="studio-nav" aria-label="주 탐색"><a href="#create-studio" className="studio-nav-item active">만들기</a><a href="#workflow-progress" className="studio-nav-item">제작 흐름</a><button type="button" className="studio-nav-item studio-nav-recent" onClick={onOpenRecent} disabled={recentBusy || state === "loading"} aria-busy={recentBusy}>{recentBusy ? "불러오는 중…" : "최근 프로젝트"}</button></nav>
    {state === "authenticated" ? <div className="studio-account-pill"><span className="studio-account-avatar" aria-hidden="true">{accountName[0].toUpperCase()}</span><span className="studio-account-name" title={accountName}>{accountName}</span><button className="studio-logout" type="button" onClick={() => void logout()}>로그아웃</button></div> : <Link className="studio-account-link" to="/login?returnTo=%2Frequest">로그인 <span aria-hidden="true">↗</span></Link>}
  </div>{recentError && <div className="studio-header-feedback" role="alert"><span>{recentError}</span><button type="button" onClick={onOpenRecent} disabled={recentBusy}>다시 시도</button></div>}</header>;
}

const localPrototypeImage = import.meta.env.DEV ? "/__prototype__/home.png" : undefined;

function HeroScene({ mascotSrc = localPrototypeImage }: { mascotSrc?: string }) {
  return <div className="hero-scene" aria-label="폭신한 보라색 후드를 입고 노트북으로 작업하는 캐릭터 일러스트"><span className="scene-spotlight" aria-hidden="true" /><MascotArtwork src={mascotSrc} /><FloatingCard className="float-idea" number="01 / SPARK" icon="✳" title="Idea" detail="오늘의 작은 영감" /><FloatingCard className="float-plan" number="02 / SHAPE" icon="▤" title="Content Plan" detail="생각을 이야기로" /><FloatingCard className="float-image" number="03 / MAKE" icon="✿" title="AI Image" detail="상상을 이미지로" /><FloatingCard className="float-review" number="04 / CHECK" icon="✓" title="Ready for Review" detail="마지막 선택은 당신이" /><span className="scene-star star-one" aria-hidden="true">✦</span><span className="scene-star star-two" aria-hidden="true">✳</span><span className="scene-orb orb-one" aria-hidden="true" /><span className="scene-orb orb-two" aria-hidden="true" /><span className="scene-squiggle" aria-hidden="true">〰</span><span className="scene-caption">A little magic, made together.</span></div>;
}

// The development reference is replaceable with a licensed, independent VORA mascot image.
function MascotArtwork({ src }: { src?: string }) {
  if (!src) return null;
  return <div className="mascot-stage"><div className="mascot-crop"><img className="mascot-image" src={src} alt="" draggable="false" /></div><span className="mascot-ground" aria-hidden="true" /></div>;
}

function FloatingCard({ className, number, icon, title, detail }: { className: string; number: string; icon: string; title: string; detail: string }) {
  return <div className={`float-card ${className}`} aria-hidden="true"><span className="float-icon">{icon}</span><span><small>{number}</small><strong>{title}</strong><em>{detail}</em></span></div>;
}

function CreateForm({ brief, setBrief, image, onImage, onCreate, busy, error, restoredDraft }: { brief: string; setBrief: (value: string) => void; image: File | null; onImage: (file: File | undefined) => void; onCreate: (event: FormEvent<HTMLFormElement>) => void; busy: boolean; error: string; restoredDraft: boolean }) {
  return <section id="create-studio" className="create-card" aria-labelledby="create-title"><div className="section-kicker"><span className="kicker-icon" aria-hidden="true">✦</span> CREATE STUDIO <span className="contract-pill">V3 / 5 SLIDES</span></div><h2 id="create-title">오늘은 어떤 이야기를 만들까요?</h2><p className="section-description">주제와 기준 이미지를 알려주면 VORA가 제작을 시작해요.</p><form onSubmit={(event) => void onCreate(event)} noValidate>{restoredDraft && <p className="form-hint" role="status">입력한 주제를 복원했습니다. 업로드 이미지는 보안상 저장되지 않으니 다시 선택해주세요.</p>}<label className="field-label" htmlFor="content-brief">콘텐츠 주제 <span>필수</span></label><textarea id="content-brief" value={brief} onChange={(event) => setBrief(event.target.value)} placeholder="예: 신제품을 소개하는 감각적인 인스타그램 카드뉴스" rows={4} aria-invalid={Boolean(error && !brief.trim())} /><label className="field-label image-field-label" htmlFor="source-image">기준 이미지 <span>JPG 또는 PNG · 1장</span></label><label className="upload-field" htmlFor="source-image"><input id="source-image" type="file" accept="image/png,image/jpeg" onChange={(event: ChangeEvent<HTMLInputElement>) => onImage(event.target.files?.[0])} /><span className="upload-icon" aria-hidden="true">＋</span><span><strong>{image ? image.name : "이미지 선택하기"}</strong><small>이미지는 제작의 출발점으로 사용돼요.</small></span><span className="upload-arrow" aria-hidden="true">↗</span></label>{error && <p className="form-error" role="alert">{error}</p>}<button className="generate-button" type="submit" disabled={busy}><span aria-hidden="true">✦</span>{busy ? "생성 요청 중..." : "AI로 생성하기"}<span className="button-arrow" aria-hidden="true">→</span></button><p className="form-hint">생성한 결과는 HUMAN_APPROVAL에서 확인하고 수정할 수 있어요.</p></form></section>;
}

function PreviewPanel({ imageUrl, selectedSlide, setSelectedSlide }: { imageUrl: string | null; selectedSlide: number; setSelectedSlide: (value: number) => void }) {
  const onSlideKeyDown = (event: KeyboardEvent<HTMLButtonElement>, slide: number) => {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? 4 : (slide + (event.key === "ArrowRight" ? 1 : 4)) % 5;
    setSelectedSlide(next);
    event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
  };
  const showSource = Boolean(imageUrl && selectedSlide === 0);
  return <section className="preview-panel" aria-labelledby="preview-title"><div className="preview-header"><div><span className="preview-eyebrow">THE CANVAS</span><h2 id="preview-title">5장 미리보기</h2></div><span className="preview-badge">{imageUrl ? "기준 이미지" : "아직 생성 전"}</span></div><div className={`preview-stage ${showSource ? "has-image" : "is-empty"}`}>{showSource ? <><img src={imageUrl!} alt="업로드한 기준 이미지 미리보기" /><span className="preview-source-label">기준 이미지 · 생성 결과 아님</span></> : <div className="empty-preview"><span className="empty-preview-art" aria-hidden="true"><i /><i /><i /></span><strong>{imageUrl ? `${selectedSlide + 1}번 슬라이드 이미지 없음` : "아직 생성된 카드뉴스가 없어요"}</strong><p>생성 결과는 Workflow에서<br />실제 파일이 준비된 뒤 확인할 수 있어요.</p></div>}{imageUrl && <span className="slide-counter">{String(selectedSlide + 1).padStart(2, "0")} / 05</span>}</div>{imageUrl && <div className="thumbnail-strip" role="group" aria-label="슬라이드 선택">{[1, 2, 3, 4, 5].map((number, index) => <button key={number} type="button" className={`thumbnail ${selectedSlide === index ? "selected" : ""}`} onClick={() => setSelectedSlide(index)} onKeyDown={(event) => onSlideKeyDown(event, index)} aria-label={`${number}번째 슬라이드 선택`} aria-pressed={selectedSlide === index}>{imageUrl && index === 0 ? <img src={imageUrl} alt="" /> : <span>{String(number).padStart(2, "0")}</span>}</button>)}</div>}<p className="preview-note">{imageUrl ? "업로드한 사진은 기준 이미지예요. AI 생성 이미지와 구분해서 표시합니다." : "주제와 기준 이미지를 입력하면 VORA가 카드뉴스를 준비해요."}</p></section>;
}

function WorkflowSummary() {
  return <section id="workflow-progress" className="summary-section" aria-labelledby="workflow-title"><div className="section-heading"><div><span className="preview-eyebrow">HOW IT WORKS</span><h2 id="workflow-title">아이디어에서 게시 준비까지</h2></div><span className="source-label">다섯 단계로 완성</span></div><ol className="workflow-steps">{steps.map((step, index) => <li key={step}><span className="step-number">{String(index + 1).padStart(2, "0")}</span><strong>{step}</strong><small>{index === 4 ? "게시는 직접 선택" : index === 3 ? "보고, 고치고, 승인" : "함께 차근차근"}</small></li>)}</ol><p className="workflow-note">완성된 카드뉴스는 직접 확인하고 승인해요. Instagram 게시는 원할 때 별도로 진행할 수 있어요.</p></section>;
}

function RecentProjects({ authenticated, projects, loading, error, onRetry }: { authenticated: boolean; projects: RecentWorkflow[]; loading: boolean; error: string; onRetry: () => void }) {
  return <section id="recent-projects" className="recent-section" aria-labelledby="recent-title"><div className="section-heading"><div><span className="preview-eyebrow">RECENT PROJECTS</span><h2 id="recent-title">최근 프로젝트</h2></div><span className="source-label">최근 수정순</span></div>{!authenticated ? <div className="empty-projects"><span className="project-illustration" aria-hidden="true"><i /><i /><i /></span><div><strong>로그인하면 작업을 이어갈 수 있어요.</strong><p>로그인 후 내가 만든 카드뉴스가 이곳에 표시돼요.</p></div></div> : loading ? <div className="recent-project-grid" aria-label="최근 프로젝트 불러오는 중">{[1, 2, 3].map((item) => <div className="recent-project-card recent-project-skeleton" key={item} aria-hidden="true"><span /><div><span /><span /></div></div>)}</div> : error ? <div className="recent-project-feedback" role="alert"><strong>프로젝트를 불러오지 못했어요.</strong><p>{error}</p><button type="button" onClick={onRetry}>다시 시도</button></div> : projects.length === 0 ? <div className="empty-projects"><span className="project-illustration" aria-hidden="true"><i /><i /><i /></span><div><strong>아직 연결된 프로젝트 목록이 없어요.</strong><p>카드뉴스를 만들면 최근 작업이 이곳에 표시돼요.</p></div></div> : <div className="recent-project-grid">{projects.map((project) => <RecentProjectCard key={project.workflow_execution_id} project={project} />)}</div>}</section>;
}

function recentProjectsError(cause: unknown) {
  if (cause instanceof ApiError) {
    if (cause.status === 401) return "로그인이 만료됐어요. 다시 로그인해 주세요.";
    if (cause.status === 403) return "프로젝트 목록을 볼 권한이 없어요.";
    if (cause.status === 404 || cause.status === 405) return "프로젝트 목록 기능을 아직 사용할 수 없어요. 잠시 후 다시 시도해 주세요.";
    if (cause.status >= 500) return "서버에서 프로젝트 목록을 불러오지 못했어요. 잠시 후 다시 시도해 주세요.";
  }
  return "네트워크 상태를 확인하고 잠시 후 다시 시도해 주세요.";
}

function recentStatusLabel(status: string) {
  return ({ RUNNING: "제작 중", WAITING_APPROVAL: "확인 필요", SUCCEEDED: "제작 완료", SUCCESS: "제작 완료", FAILED: "제작 실패", PENDING: "준비 중", REVISING: "수정 중", CANCELED: "취소됨" } as Record<string, string>)[status] ?? "진행 상태 확인";
}

function RecentProjectCard({ project }: { project: RecentWorkflow }) {
  const updated = new Intl.DateTimeFormat("ko-KR", { month: "short", day: "numeric" }).format(new Date(project.updated_at));
  return <Link className="recent-project-card" to={`/workflows/${project.workflow_execution_id}`} aria-label={`${project.title}, ${recentStatusLabel(project.status)}`}><div className="recent-project-thumb"><RecentProjectThumbnail project={project} /><span className={`recent-project-status ${project.status.toLowerCase()}`}>{recentStatusLabel(project.status)}</span></div><div className="recent-project-copy"><strong>{project.title}</strong><span>마지막 수정 {updated}</span></div><span className="recent-project-arrow" aria-hidden="true">↗</span></Link>;
}

function RecentProjectThumbnail({ project }: { project: RecentWorkflow }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!project.thumbnail_url) return undefined;
    const controller = new AbortController(); let objectUrl: string | null = null; let active = true;
    void api.getSlideImageBlob(project.thumbnail_url, controller.signal).then((blob) => { if (!active) return; objectUrl = URL.createObjectURL(blob); setUrl(objectUrl); }).catch((cause) => { if (active && !(cause instanceof DOMException && cause.name === "AbortError")) setFailed(true); });
    return () => { active = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [project.thumbnail_url]);
  if (url && !failed) return <img src={url} alt="" />;
  return <span className="recent-project-placeholder" aria-hidden="true">✳</span>;
}
