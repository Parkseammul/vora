import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
import { V3WorkflowPage } from "./v3Workflow";
import type { V3NodeType, V3Workflow } from "./types";

const nodeTypes: V3NodeType[] = ["CONTENT_PLAN", "TEXT_GENERATE", "TEXT_CHECK", "IMAGE_GENERATE", "IMAGE_CHECK", "HUMAN_APPROVAL", "PUBLICATION"];

function failedWorkflow(): V3Workflow {
  return {
    id: 9242,
    workflow_execution_id: 9242,
    workflow_version: 3,
    status: "FAILED",
    current_node_type: "IMAGE_GENERATE",
    current_revision: 1,
    nodes: Object.fromEntries(nodeTypes.map((node_type, index) => [node_type, {
      id: index + 1,
      node_type,
      revision_no: 1,
      status: node_type === "IMAGE_GENERATE" ? "FAILED" : "SUCCEEDED",
      attempt: { current: 1, automatic_max: 1 },
      result: null,
    }])) as V3Workflow["nodes"],
    human_approval: { waiting: false, node_execution_id: null, status: "FAILED" },
    images: [],
  };
}

function waitingWorkflow(): V3Workflow {
  const workflow = failedWorkflow();
  return {
    ...workflow,
    status: "WAITING_APPROVAL",
    current_node_type: "HUMAN_APPROVAL",
    nodes: Object.fromEntries(nodeTypes.map((node_type) => [node_type, {
      ...workflow.nodes[node_type],
      status: node_type === "HUMAN_APPROVAL" ? "WAITING" : "SUCCEEDED",
    }])) as V3Workflow["nodes"],
    human_approval: { waiting: true, node_execution_id: 7, status: "WAITING" },
  };
}

describe("V3 workflow retry", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("calls retry once, disables the button, and reloads the workflow after success", async () => {
    const workflow = failedWorkflow();
    let resolveRetry!: () => void;
    const retry = vi.spyOn(api, "retry").mockImplementation(() => new Promise<void>((resolve) => { resolveRetry = resolve; }));
    const getWorkflow = vi.spyOn(api, "getWorkflow").mockResolvedValue(workflow);
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={9242} initialWorkflow={workflow} /></MemoryRouter>);

    const button = screen.getByRole("button", { name: "다시 시도" });
    fireEvent.click(button);
    expect(screen.getByRole("button", { name: "재시도 중…" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "재시도 중…" }));
    expect(retry).toHaveBeenCalledTimes(1);

    resolveRetry();
    await waitFor(() => expect(getWorkflow).toHaveBeenCalledWith(9242, undefined));
  });

  it("shows an error and does not retry automatically when the request fails", async () => {
    const retry = vi.spyOn(api, "retry").mockRejectedValue(new ApiError(500, "retry failed"));
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={9242} initialWorkflow={failedWorkflow()} /></MemoryRouter>);

    fireEvent.click(screen.getByRole("button", { name: "다시 시도" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("서버에서 처리하지 못했습니다"));
    expect(retry).toHaveBeenCalledTimes(1);
  });
});

describe("V3 workflow refresh", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("fetches the current workflow once and reflects the latest response", async () => {
    const initial = failedWorkflow();
    const latest: V3Workflow = {
      ...initial,
      status: "WAITING_APPROVAL",
      current_node_type: "HUMAN_APPROVAL",
      nodes: Object.fromEntries(nodeTypes.map((node_type, index) => [node_type, {
        id: index + 1,
        node_type,
        revision_no: 1,
        status: node_type === "HUMAN_APPROVAL" ? "WAITING" : node_type === "PUBLICATION" ? "PENDING" : "SUCCEEDED",
        attempt: { current: 1, automatic_max: 1 },
        result: null,
      }])) as V3Workflow["nodes"],
      human_approval: { waiting: true, node_execution_id: 99, status: "WAITING" },
      images: [{ slide_no: 1, file_name: "31241.png", mime_type: "image/png", image_url: "/assets/31241" }],
    };
    const getWorkflow = vi.spyOn(api, "getWorkflow").mockResolvedValue(latest);
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={9242} initialWorkflow={initial} /></MemoryRouter>);

    fireEvent.click(screen.getByRole("button", { name: "새로고침" }));
    expect(screen.getByRole("button", { name: "새로고침 중…" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "새로고침 중…" }));
    expect(getWorkflow).toHaveBeenCalledTimes(1);

    await waitFor(() => expect(screen.getByRole("button", { name: "새로고침" })).toBeEnabled());
    expect(screen.getByText("새로고침 완료")).toBeInTheDocument();
    expect(screen.getAllByText("확인 대기 중").length).toBeGreaterThan(0);
    expect(screen.getAllByText("최종 확인").length).toBeGreaterThan(0);
    expect(screen.getByText("1 / 5")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "게시 준비" })).toBeInTheDocument();
  });

  it("shows an error when refresh fails without calling action APIs", async () => {
    const getWorkflow = vi.spyOn(api, "getWorkflow").mockRejectedValue(new ApiError(500, "refresh failed"));
    const retry = vi.spyOn(api, "retry");
    const approve = vi.spyOn(api, "approve");
    const publish = vi.spyOn(api, "publishInstagram");
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={9242} initialWorkflow={failedWorkflow()} /></MemoryRouter>);

    fireEvent.click(screen.getByRole("button", { name: "새로고침" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(getWorkflow).toHaveBeenCalledTimes(1);
    expect(retry).not.toHaveBeenCalled();
    expect(approve).not.toHaveBeenCalled();
    expect(publish).not.toHaveBeenCalled();
  });
});

describe("V3 selective image revision", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("sends explicit selected slides and feedback while preserving the existing revision API", async () => {
    const approve = vi.spyOn(api, "approve").mockResolvedValue({ workflow_execution_id: 9242, status: "RUNNING" });
    vi.spyOn(api, "getWorkflow").mockResolvedValue(waitingWorkflow());
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={9242} initialWorkflow={waitingWorkflow()} /></MemoryRouter>);

    fireEvent.click(screen.getByRole("button", { name: "수정 요청" }));
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "IMAGE" } });
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(5);
    [0, 1, 2, 4].forEach((index) => fireEvent.click(checkboxes[index]));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "4번 슬라이드 제목을 수정해 주세요." } });
    fireEvent.click(screen.getByRole("button", { name: "수정 요청 보내기" }));

    await waitFor(() => expect(approve).toHaveBeenCalledWith(
      9242,
      7,
      "REJECTED",
      "IMAGE",
      "4번 슬라이드 제목을 수정해 주세요.",
      { slideNumbers: [4], feedback: "4번 슬라이드 제목을 수정해 주세요." },
    ));
  });
});
