export type WorkflowStatus = "PENDING" | "RUNNING" | "WAITING_APPROVAL" | "SUCCESS" | "FAILED";
export type NodeStatus = WorkflowStatus | "RETRYING";
export type NodeKey = "input_analysis" | "content_planning" | "script_generation" | "video_generation";

export interface NodeSummary {
  id: number;
  node_key: NodeKey;
  user_requested_version: number;
  status: NodeStatus;
  attempt: { current: number; automatic_max: number };
}

export interface Workflow {
  id: number;
  status: WorkflowStatus;
  nodes: Record<NodeKey, NodeSummary | null>;
}

export type V3WorkflowStatus = "PENDING" | "RUNNING" | "WAITING_APPROVAL" | "REVISING" | "SUCCEEDED" | "FAILED" | "CANCELED";
export type V3NodeType = "CONTENT_PLAN" | "TEXT_GENERATE" | "TEXT_CHECK" | "IMAGE_GENERATE" | "IMAGE_CHECK" | "HUMAN_APPROVAL" | "PUBLICATION";
export type V3NodeStatus = "PENDING" | "RUNNING" | "WAITING" | "SUCCEEDED" | "FAILED" | "SKIPPED";

export interface V3NodeExecution {
  id: number;
  node_type: V3NodeType;
  revision_no: number;
  status: V3NodeStatus;
  attempt: { current: number; automatic_max: number };
  result: Record<string, unknown> | null;
}

export interface V3ApprovalTarget {
  waiting: boolean;
  node_execution_id: number | null;
  status: V3NodeStatus | null;
}

export interface V3ImageAsset {
  slide_no: 1 | 2 | 3 | 4 | 5;
  file_name: string;
  mime_type: string;
  image_url: string;
}

export interface V3Workflow {
  id: number;
  workflow_execution_id: number;
  workflow_version: 3;
  status: V3WorkflowStatus;
  current_node_type: V3NodeType | null;
  current_revision: number;
  nodes: Record<V3NodeType, V3NodeExecution | null>;
  human_approval: V3ApprovalTarget;
  images: V3ImageAsset[];
}

export type WorkflowRead = Workflow | V3Workflow;

export function isV3Workflow(workflow: WorkflowRead): workflow is V3Workflow {
  return "workflow_version" in workflow && workflow.workflow_version === 3;
}

export interface Detail<T> {
  workflow_execution_id: number;
  workflow_status: WorkflowStatus;
  node: NodeSummary | null;
  output: T | null;
}

export interface PlanOutput {
  concept: string; hook: string; key_message: string; cta: string;
  visual_style: string; bgm_direction: string;
  scenes: Array<{ duration_seconds: number }>;
}

export interface ScriptOutput {
  scenes: Array<{ planning_scene_id: string; narration: string | null; subtitle: string | null; speaking_style: string | null }>;
}

export interface VideoOutput { video_asset_id: number }
export interface VideoDetail extends Detail<VideoOutput> {
  video: { stream_url: string; download_url: string } | null;
}

export type SocialPlatform = "YOUTUBE" | "INSTAGRAM";
export interface Publication { id: number; platform: SocialPlatform; status: "PENDING" | "PUBLISHING" | "PUBLISHED" | "SUCCESS" | "FAILED"; external_post_url: string | null; error_message: string | null; attempt: { current: number; max: number }; }
export interface PublicationDetail { draft: { youtube_title: string; youtube_description: string; instagram_caption: string }; publications: Publication[]; }
export interface SocialConnection { platform: SocialPlatform; account_name: string | null; status: string; }
export interface InstagramPublicationRequest { platforms: ["INSTAGRAM"]; instagram_caption: string; force_republish: false; }
