import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { V3WorkflowPage } from "./v3Workflow";
import type { V3NodeType, V3Workflow } from "./types";

const nodeTypes: V3NodeType[] = ["CONTENT_PLAN", "TEXT_GENERATE", "TEXT_CHECK", "IMAGE_GENERATE", "IMAGE_CHECK", "HUMAN_APPROVAL", "PUBLICATION"];

function revisedWorkflow(): V3Workflow {
  return {
    id: 42,
    workflow_execution_id: 42,
    workflow_version: 3,
    status: "SUCCEEDED",
    current_node_type: null,
    current_revision: 2,
    nodes: Object.fromEntries(nodeTypes.map((node_type) => [node_type, node_type === "CONTENT_PLAN" ? null : {
      id: nodeTypes.indexOf(node_type) + 10,
      node_type,
      revision_no: 2,
      status: "SUCCEEDED",
      attempt: { current: 1, automatic_max: 1 },
      result: null,
    }])) as V3Workflow["nodes"],
    human_approval: { waiting: false, node_execution_id: null, status: "SUCCEEDED" },
    images: [],
  };
}

describe("V3 reused revision display", () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it("shows reused progress as complete without exposing internal history", async () => {
    vi.spyOn(api, "getWorkflowTrace").mockResolvedValue({
      nodes: [{ node_type: "CONTENT_PLAN", revision_no: 0, status: "SUCCEEDED" }],
      revisions: [{ revision_no: 2, start_node_type: "TEXT_GENERATE" }],
    });

    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={revisedWorkflow()} /></MemoryRouter>);

    await waitFor(() => expect(api.getWorkflowTrace).toHaveBeenCalled());
    const planning = screen.getByText("기획").closest("li");
    expect(planning).toHaveClass("reused");
    expect(planning).toHaveTextContent("완료");
    expect(screen.queryByText(/기존 결과 사용|이전 결과를 그대로 사용해요/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Revision|NodeExecution|REUSED/)).not.toBeInTheDocument();
  });
});
