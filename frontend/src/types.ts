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
export interface Publication { id: number; platform: SocialPlatform; status: "PENDING" | "PUBLISHING" | "SUCCESS" | "FAILED"; external_post_url: string | null; error_message: string | null; attempt: { current: number; max: number }; }
export interface PublicationDetail { draft: { youtube_title: string; youtube_description: string; instagram_caption: string }; publications: Publication[]; }
export interface SocialConnection { platform: SocialPlatform; account_name: string | null; status: string; }
