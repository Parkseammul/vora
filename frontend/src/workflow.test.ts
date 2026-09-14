import { describe, expect, it } from "vitest";
import { activeStep, isPolling, statusLabel } from "./workflow";
import type { Workflow } from "./types";

describe("workflow UI mappings", () => {
  it("maps only pending and running states to polling", () => {
    expect(isPolling("PENDING")).toBe(true); expect(isPolling("RUNNING")).toBe(true);
    expect(isPolling("WAITING_APPROVAL")).toBe(false); expect(isPolling("FAILED")).toBe(false);
  });
  it("uses user-facing labels and selects the active approval step", () => {
    const workflow = { id: 1, status: "WAITING_APPROVAL", nodes: { input_analysis: { id: 1, node_key: "input_analysis", user_requested_version: 1, status: "SUCCESS", attempt: { current: 1, automatic_max: 1 } }, content_planning: { id: 2, node_key: "content_planning", user_requested_version: 1, status: "WAITING_APPROVAL", attempt: { current: 1, automatic_max: 1 } }, script_generation: null, video_generation: null } } as Workflow;
    expect(activeStep(workflow)).toBe("content_planning"); expect(statusLabel.RETRYING).toBe("재실행 중");
  });
});
