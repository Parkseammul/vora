import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { V3WorkflowPage } from "./v3Workflow";
import type { V3NodeType, V3Workflow } from "./types";

const nodeTypes: V3NodeType[] = ["CONTENT_PLAN", "TEXT_GENERATE", "TEXT_CHECK", "IMAGE_GENERATE", "IMAGE_CHECK", "HUMAN_APPROVAL", "PUBLICATION"];
function workflow(): V3Workflow {
  return {
    id: 42, workflow_execution_id: 42, workflow_version: 3, status: "WAITING_APPROVAL", current_node_type: "HUMAN_APPROVAL", current_revision: 2,
    nodes: Object.fromEntries(nodeTypes.map((node_type) => [node_type, { id: nodeTypes.indexOf(node_type) + 10, node_type, revision_no: 2, status: node_type === "HUMAN_APPROVAL" ? "WAITING" : "SUCCEEDED", attempt: { current: 1, automatic_max: 1 }, result: null }])) as V3Workflow["nodes"],
    human_approval: { waiting: true, node_execution_id: 16, status: "WAITING" },
    images: [1, 2, 3, 4, 5].map((slide_no) => ({ slide_no: slide_no as 1 | 2 | 3 | 4 | 5, file_name: `slide-${slide_no}.png`, mime_type: "image/png", image_url: `/workflow-executions/42/slides/${slide_no}/image` })),
  };
}

describe("V3 workflow detail", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("renders seven real nodes and five authenticated image blobs", async () => {
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:slide");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(api, "getSlideImageBlob").mockResolvedValue(new Blob(["image"], { type: "image/png" }));
    const { unmount } = render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={workflow()} /></MemoryRouter>);
    for (const node of nodeTypes) expect(screen.getAllByText(node).length).toBeGreaterThan(0);
    expect(screen.getAllByText("WAITING APPROVAL").length).toBeGreaterThan(0);
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(screen.getByAltText("1번 카드뉴스")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "3" }));
    await waitFor(() => expect(screen.getByAltText("3번 카드뉴스")).toBeInTheDocument());
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });

  it("uses the current approval id for approve and sends rejection revision fields", async () => {
    vi.spyOn(api, "getSlideImageBlob").mockResolvedValue(new Blob(["image"]));
    const approve = vi.spyOn(api, "approve").mockResolvedValue({ workflow_execution_id: 42, status: "REVISING" });
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={workflow()} /></MemoryRouter>);
    fireEvent.click(screen.getAllByRole("button", { name: "승인" })[0]);
    await waitFor(() => expect(approve).toHaveBeenCalledWith(42, 16, "APPROVED", undefined, undefined));
    fireEvent.change(screen.getAllByPlaceholderText("수정 요청을 입력하세요")[0], { target: { value: "이미지 톤을 바꿔주세요" } });
    fireEvent.click(screen.getByRole("button", { name: "수정 요청" }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith(42, 16, "REJECTED", "TEXT", "이미지 톤을 바꿔주세요"));
  });
});
