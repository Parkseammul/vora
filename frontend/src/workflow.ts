import type { NodeKey, NodeStatus, Workflow } from "./types";

export const steps: Array<{ key: NodeKey; label: string }> = [
  { key: "input_analysis", label: "입력 분석" }, { key: "content_planning", label: "기획" },
  { key: "script_generation", label: "대본" }, { key: "video_generation", label: "영상" },
];

export const statusLabel: Record<NodeStatus, string> = {
  PENDING: "대기", RUNNING: "진행 중", WAITING_APPROVAL: "승인 대기", SUCCESS: "완료", FAILED: "실패", RETRYING: "재실행 중",
};

export function isPolling(status?: NodeStatus): boolean {
  return status === "PENDING" || status === "RUNNING";
}

export function activeStep(workflow: Workflow): NodeKey {
  return steps.find(({ key }) => {
    const status = workflow.nodes[key]?.status;
    return status === "RUNNING" || status === "RETRYING" || status === "WAITING_APPROVAL" || status === "FAILED";
  })?.key ?? "input_analysis";
}
