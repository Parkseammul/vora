import type { Detail, PlanOutput, PublicationDetail, ScriptOutput, SocialConnection, VideoDetail, Workflow } from "./types";

const baseUrl = import.meta.env.VITE_API_BASE_URL ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(body?.detail ?? "요청을 처리하지 못했습니다.");
  }
  return response.json() as Promise<T>;
}

export const api = {
  getWorkflow: (id: number) => request<Workflow>(`/workflow-executions/${id}`),
  getPlan: (id: number) => request<Detail<PlanOutput>>(`/workflow-executions/${id}/plan`),
  getScript: (id: number) => request<Detail<ScriptOutput>>(`/workflow-executions/${id}/script`),
  getVideo: (id: number) => request<VideoDetail>(`/workflow-executions/${id}/video`),
  createWorkflow: (text: string, images: File[]) => {
    const form = new FormData(); form.append("request_text", text); images.forEach((image) => form.append("images", image));
    return request<{ workflow_execution_id: number }>("/workflow-executions", { method: "POST", body: form });
  },
  approve: (id: number, nodeExecutionId: number) => request<void>(`/workflow-executions/${id}/approvals`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ node_execution_id: nodeExecutionId }),
  }),
  revise: (id: number, currentNode: string, revisionRequest: string) => request<{ restart_node: string }>(`/workflow-executions/${id}/revisions`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ current_node: currentNode, revision_request: revisionRequest }),
  }),
  retry: (id: number) => request<void>(`/workflow-executions/${id}/retry`, { method: "POST" }),
  eventUrl: (id: number) => `${baseUrl}/workflow-executions/${id}/video-generation/events`,
  getPublications: (id: number) => request<PublicationDetail>(`/workflow-executions/${id}/publications`),
  publish: (id: number, body: object) => request<PublicationDetail>(`/workflow-executions/${id}/publications`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  publicationEventUrl: (id: number) => `${baseUrl}/workflow-executions/${id}/publication/events`,
  connections: () => request<{ connections: SocialConnection[] }>("/social/connections"),
  authorize: (platform: string, returnTo = "/settings/social") => request<{ authorization_url: string }>(`/social/${platform}/authorize?return_to=${encodeURIComponent(returnTo)}`),
  assetUrl: (path: string) => `${baseUrl}${path}`,
};
