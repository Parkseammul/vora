import type { Detail, InstagramPublicationRequest, PlanOutput, PublicationDetail, ScriptOutput, SocialConnection, VideoDetail, WorkflowRead, V3WorkflowTrace } from "./types";

const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "";
const apiUrl = (path: string) => baseUrl ? `${baseUrl.replace(/\/$/, "")}/${path.replace(/^\//, "")}` : path;

export class ApiError extends Error {
  constructor(public readonly status: number, message: string) { super(message); this.name = "ApiError"; }
}

type AccessTokenProvider = () => string | null | Promise<string | null>;
let accessTokenProvider: AccessTokenProvider | undefined;
export function configureAccessTokenProvider(provider: AccessTokenProvider | undefined) { accessTokenProvider = provider; }
async function authHeaders(): Promise<Headers> {
  const headers = new Headers();
  const token = await accessTokenProvider?.();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return headers;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = await authHeaders();
  new Headers(init.headers).forEach((value, key) => headers.set(key, value));
  const response = await fetch(apiUrl(path), { ...init, headers });
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null;
    if (response.status === 401) window.dispatchEvent(new Event("vora:auth-expired"));
    throw new ApiError(response.status, body?.detail ?? `Request failed (${response.status})`);
    throw new Error(body?.detail ?? "요청을 처리하지 못했습니다.");
  }
  return response.json() as Promise<T>;
}

export type EventStream = { close: () => void };
async function openEventStream(path: string, onMessage: (data: string) => void, onError: (error: unknown) => void): Promise<EventStream> {
  const controller = new AbortController();
  void (async () => { try {
    const response = await fetch(apiUrl(path), { headers: await authHeaders(), signal: controller.signal });
    if (!response.ok || !response.body) throw new ApiError(response.status, `Event stream failed (${response.status})`);
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
    while (true) { const { value, done } = await reader.read(); if (done) break; buffer += decoder.decode(value, { stream: true }); const blocks = buffer.split("\n\n"); buffer = blocks.pop() ?? ""; for (const block of blocks) { const data = block.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n"); if (data) onMessage(data); } }
  } catch (error) { if (!(error instanceof DOMException && error.name === "AbortError")) onError(error); } })();
  return { close: () => controller.abort() };
}

async function requestBlob(path: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(apiUrl(path), { headers: await authHeaders(), signal });
  if (response.status === 401) window.dispatchEvent(new Event("vora:auth-expired"));
  if (!response.ok) throw new ApiError(response.status, `Image request failed (${response.status})`);
  return response.blob();
}

export const api = {
  getWorkflow: (id: number, signal?: AbortSignal) => request<WorkflowRead>(`/workflow-executions/${id}`, { signal }),
  getWorkflowTrace: (id: number, signal?: AbortSignal) => request<V3WorkflowTrace>(`/workflow-executions/${id}/trace`, { signal }),
  getPlan: (id: number) => request<Detail<PlanOutput>>(`/workflow-executions/${id}/plan`),
  getScript: (id: number) => request<Detail<ScriptOutput>>(`/workflow-executions/${id}/script`),
  getVideo: (id: number) => request<VideoDetail>(`/workflow-executions/${id}/video`),
  createWorkflow: (text: string, image: File | File[], marketingInfo = "Instagram 카드뉴스 제작", rulesVersionId = 1) => {
    const form = new FormData(); form.append("request_text", text); form.append("marketing_info", marketingInfo); form.append("rules_version_id", String(rulesVersionId));
    const source = Array.isArray(image) ? image[0] : image; if (source) form.append("source_image", source);
    return request<{ workflow_execution_id: number; workflow_version?: number }>("/workflow-executions", { method: "POST", body: form });
  },
  approve: (id: number, nodeExecutionId: number, decision: "APPROVED" | "REJECTED" = "APPROVED", revisionTarget?: "PLAN" | "TEXT" | "IMAGE", revisionRequest?: string) => request<{ workflow_execution_id: number; status: string }>(`/workflow-executions/${id}/approvals`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node_execution_id: nodeExecutionId, decision, ...(decision === "REJECTED" ? { revision_target: revisionTarget, revision_request: revisionRequest } : {}) }),
  }),
  revise: (id: number, currentNode: string, revisionRequest: string) => request<{ restart_node: string }>(`/workflow-executions/${id}/revisions`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ current_node: currentNode, revision_request: revisionRequest }),
  }),
  retry: (id: number) => request<void>(`/workflow-executions/${id}/retry`, { method: "POST" }),
  eventUrl: (id: number) => apiUrl(`/workflow-executions/${id}/video-generation/events`),
  subscribeSse: (path: string, onMessage: (data: string) => void, onError: (error: unknown) => void) => openEventStream(path, onMessage, onError),
  getPublications: (id: number) => request<PublicationDetail>(`/workflow-executions/${id}/publications`),
  publish: (id: number, body: object) => request<PublicationDetail>(`/workflow-executions/${id}/publications`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  publishInstagram: (id: number, body: InstagramPublicationRequest) => request<PublicationDetail>(`/workflow-executions/${id}/publications`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  publicationEventUrl: (id: number) => apiUrl(`/workflow-executions/${id}/publication/events`),
  connections: () => request<{ connections: SocialConnection[] }>("/social/connections"),
  authorize: (platform: string, returnTo = "/settings/social") => request<{ authorization_url: string }>(`/social/${platform}/authorize?return_to=${encodeURIComponent(returnTo)}`),
  assetUrl: apiUrl,
  getSlideImageBlob: requestBlob,
};
