import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { api } from "./api";
import { useWorkflow } from "./WorkflowContext";
import type { Publication, PublicationDetail, SocialPlatform } from "./types";

const platforms: SocialPlatform[] = ["YOUTUBE", "INSTAGRAM"];

export function SocialConnectButton({ platform }: { platform: SocialPlatform }) {
  const location = useLocation();
  const [error, setError] = useState("");
  const connect = async () => { try { window.location.assign((await api.authorize(platform, location.pathname)).authorization_url); } catch (cause) { setError(cause instanceof Error ? cause.message : "OAuth connection failed"); } };
  return <>{error && <p className="error">{error}</p>}<button onClick={() => void connect()}>{platform} 연결</button></>;
}

export function PublicationPanel() {
  const { workflowExecutionId } = useWorkflow();
  const [detail, setDetail] = useState<PublicationDetail | null>(null);
  const [connections, setConnections] = useState<SocialPlatform[]>([]);
  const [caption, setCaption] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestInFlight = useRef(false);
  const load = useCallback(async () => {
    const [publication, accounts] = await Promise.all([
      api.getPublications(workflowExecutionId), api.connections(),
    ]);
    setDetail(publication);
    setConnections(accounts.connections.map((item) => item.platform));
    setCaption((value) => value || publication.draft.instagram_caption);
  }, [workflowExecutionId]);
  useEffect(() => {
    queueMicrotask(() => void load().catch((cause) => setError(cause instanceof Error ? cause.message : "Could not load publication status.")));
    const stream = api.subscribeSse?.(api.publicationEventUrl(workflowExecutionId), () => void load(), () => undefined);
    return () => { void stream?.then((value) => value.close()); };
  }, [load, workflowExecutionId]);

  const publication = detail?.publications.find((item) => item.platform === "INSTAGRAM");
  const blocked = ["PENDING", "PUBLISHING", "PUBLISHED"].includes(publication?.status ?? "");
  const requestPublication = async () => {
    if (requestInFlight.current || blocked) return;
    requestInFlight.current = true;
    setBusy(true);
    setError("");
    try {
      await api.publishInstagram(workflowExecutionId, {
        platforms: ["INSTAGRAM"], instagram_caption: caption, force_republish: false,
      });
      await load();
      setConfirm(false);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not request Instagram publication.");
    } finally {
      requestInFlight.current = false;
      setBusy(false);
    }
  };

  return <section className="publication">
    <h2>Instagram publication</h2>
    <p>Approval prepares the carousel. Publication starts only after your confirmation.</p>
    {!connections.includes("INSTAGRAM") && <SocialConnectButton platform="INSTAGRAM" />}
    <label>Instagram caption<textarea value={caption} onChange={(event) => setCaption(event.target.value)} /></label>
    {publication && <InstagramPublicationState publication={publication} />}
    {publication?.status === "FAILED" && <p className="notice">Automatic republishing is disabled. Review the failure before a new publication decision.</p>}
    {error && <p className="error">{error}</p>}
    <button className="primary" disabled={!detail || blocked || busy} onClick={() => setConfirm(true)}>Request Instagram publication</button>
    {confirm && <div className="modal-backdrop" role="dialog" aria-modal="true">
      <section className="modal"><h2>Request this Instagram Carousel publication?</h2>
        <button onClick={() => setConfirm(false)}>Cancel</button>
        <button className="primary" disabled={busy} onClick={() => void requestPublication()}>{busy ? "Requesting…" : "Confirm Instagram publication"}</button>
      </section>
    </div>}
  </section>;
}

function InstagramPublicationState({ publication }: { publication: Publication }) {
  if (publication.status === "PUBLISHED") return <p>Instagram: PUBLISHED {publication.external_post_url && <a href={publication.external_post_url} target="_blank" rel="noreferrer">View post</a>}</p>;
  if (publication.status === "PUBLISHING") return <p>Instagram: PUBLISHING — awaiting result; no automatic retry.</p>;
  if (publication.status === "PENDING") return <p>Instagram: PENDING — publication request accepted.</p>;
  return <p>Instagram: FAILED {publication.error_message}</p>;
}

export function LegacyPublicationPanel() {
  const { workflowExecutionId } = useWorkflow();
  const [detail, setDetail] = useState<PublicationDetail | null>(null);
  const [connections, setConnections] = useState<SocialPlatform[]>([]);
  const [selected, setSelected] = useState<SocialPlatform[]>(platforms);
  const [title, setTitle] = useState(""); const [description, setDescription] = useState(""); const [caption, setCaption] = useState("");
  const [confirm, setConfirm] = useState(false); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const load = useCallback(async () => { const [publication, accounts] = await Promise.all([api.getPublications(workflowExecutionId), api.connections()]); setDetail(publication); setConnections(accounts.connections.map((item) => item.platform)); setTitle((value) => value || publication.draft.youtube_title); setDescription((value) => value || publication.draft.youtube_description); setCaption((value) => value || publication.draft.instagram_caption); }, [workflowExecutionId]);
  useEffect(() => { queueMicrotask(() => void load().catch((cause) => setError(cause instanceof Error ? cause.message : "게시 정보를 불러오지 못했습니다."))); const stream = api.subscribeSse?.(api.publicationEventUrl(workflowExecutionId), () => void load(), () => undefined); return () => { void stream?.then((value) => value.close()); }; }, [load, workflowExecutionId]);
  const publish = async (retryPlatform?: SocialPlatform) => { setBusy(true); setError(""); try { await api.publish(workflowExecutionId, { platforms: retryPlatform ? [retryPlatform] : selected, youtube_title: title, youtube_description: description, instagram_caption: caption, force_republish: Boolean(retryPlatform) }); await load(); setConfirm(false); } catch (cause) { setError(cause instanceof Error ? cause.message : "게시를 시작하지 못했습니다."); } finally { setBusy(false); } };
  const toggle = (platform: SocialPlatform) => setSelected((items) => items.includes(platform) ? items.filter((item) => item !== platform) : [...items, platform]);
  return <section className="publication"><h2>SNS 게시</h2><p>최종 승인된 영상만 게시할 수 있습니다.</p>{platforms.map((platform) => <div key={platform}><label><input type="checkbox" checked={selected.includes(platform)} onChange={() => toggle(platform)} />{platform}</label>{!connections.includes(platform) && <SocialConnectButton platform={platform} />}</div>)}<label>YouTube title<input value={title} onChange={(event) => setTitle(event.target.value)} /></label><label>YouTube description<textarea value={description} onChange={(event) => setDescription(event.target.value)} /></label><label>Instagram caption<textarea value={caption} onChange={(event) => setCaption(event.target.value)} /></label>{detail?.publications.map((item) => <p key={item.id}>{item.platform}: {item.status} ({item.attempt.current}/{item.attempt.max}) {item.error_message} {item.status === "FAILED" && <button disabled={busy} onClick={() => void publish(item.platform)}>다시 게시</button>}</p>)}{error && <p className="error">{error}</p>}<button className="primary" disabled={!selected.length || busy} onClick={() => setConfirm(true)}>게시</button>{confirm && <div className="modal-backdrop" role="dialog" aria-modal="true"><section className="modal"><h2>선택한 플랫폼에 실제 게시할까요?</h2><button onClick={() => setConfirm(false)}>취소</button><button className="primary" disabled={busy} onClick={() => void publish()}>{busy ? "게시 중…" : "최종 게시"}</button></section></div>}</section>;
}
