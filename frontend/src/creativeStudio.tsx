import { useEffect, useState, type ChangeEvent, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "./api";
import { useAuth } from "./auth";

const steps = ["Plan", "Text", "Image", "Review", "Publish"];
const loginDraftKey = "vora.login-draft.request-text";

export function DashboardPage() {
  const navigate = useNavigate();
  const { state: authState } = useAuth();
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
    setBusy(true); setError("");
    try {
      const result = await api.createWorkflow(brief.trim(), image);
      navigate(`/workflows/${result.workflow_execution_id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "콘텐츠 생성 요청에 실패했습니다.");
    } finally { setBusy(false); }
  };

  return <div className="studio-shell studio-shell--home">
    <StudioHeader />
    <main className="studio-main">
      <section className="hero-section" aria-labelledby="hero-title">
        <div className="hero-copy"><span className="hero-edition">CREATIVE STUDIO <span aria-hidden="true">/</span> 01</span><p className="hero-eyebrow">✦ YOUR TINY CREATIVE TEAM</p><h1 id="hero-title">당신의 아이디어가<br /><span>콘텐츠가 되는 순간.</span></h1><p className="hero-subtitle">혼자 일해도, 작은 크리에이티브 팀이<br className="desktop-break" /> 함께하는 것처럼.</p><a className="hero-cta" href="#create-studio">카드뉴스 만들기 <span aria-hidden="true">↗</span></a><div className="hero-promise"><span className="hero-promise-mark" aria-hidden="true">✳</span><span>아이디어 한 줄에서<br /><strong>다섯 장의 이야기까지.</strong></span></div></div>
        <HeroScene />
      </section>
      <div className="studio-grid">
        <CreateForm brief={brief} setBrief={setBrief} image={image} onImage={onImage} onCreate={create} busy={busy} error={error} restoredDraft={restoredDraft} />
        <PreviewPanel imageUrl={imageUrl} selectedSlide={selectedSlide} setSelectedSlide={setSelectedSlide} />
        <WorkflowSummary />
        <RecentProjects />
      </div>
    </main>
  </div>;
}

function StudioHeader() {
  const { user, state, logout } = useAuth();
  return <header className="studio-header"><Link to="/request" className="studio-logo" aria-label="VORA 홈"><span className="logo-mark" aria-hidden="true">✳</span><span>VORA<small>Creative Studio</small></span></Link><nav className="studio-nav" aria-label="주 탐색"><a href="#create-studio" className="studio-nav-item active">만들기</a><a href="#workflow-progress" className="studio-nav-item">제작 흐름</a><a href="#recent-projects" className="studio-nav-item">최근 프로젝트</a></nav>{state === "authenticated" ? <span className="studio-account-link">{user?.profile.email ?? "Account"} <button type="button" onClick={() => void logout()}>로그아웃</button></span> : <Link className="studio-account-link" to="/login?returnTo=%2Frequest">로그인</Link>}</header>;
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
  return <section id="workflow-progress" className="summary-section" aria-labelledby="workflow-title"><div className="section-heading"><div><span className="preview-eyebrow">HOW IT WORKS</span><h2 id="workflow-title">아이디어에서 게시 준비까지</h2></div><span className="source-label">제작 흐름 안내</span></div><ol className="workflow-steps">{steps.map((step, index) => <li key={step}><span className="step-number">{String(index + 1).padStart(2, "0")}</span><strong>{step}</strong><small>{index === 4 ? "별도 게시 액션" : index === 3 ? "사람이 검토·승인" : "생성 후 확인"}</small></li>)}</ol><p className="workflow-note">아직 조회한 Workflow가 없습니다. 실제 진행 상태는 생성 후 작업 화면에서 확인하세요. 승인과 SNS 게시는 별개예요.</p></section>;
}

function RecentProjects() {
  return <section id="recent-projects" className="recent-section" aria-labelledby="recent-title"><div className="section-heading"><div><span className="preview-eyebrow">RECENT PROJECTS</span><h2 id="recent-title">최근 프로젝트</h2></div></div><div className="empty-projects"><span className="project-illustration" aria-hidden="true"><i /><i /><i /></span><div><strong>아직 연결된 프로젝트 목록이 없어요.</strong><p>프로젝트 목록 API가 연결되면 이곳에서 작업을 볼 수 있어요.</p></div></div></section>;
}
