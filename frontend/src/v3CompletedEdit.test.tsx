import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
import { V3WorkflowPage } from "./v3Workflow";
import type { V3NodeType, V3Workflow } from "./types";

const nodeTypes: V3NodeType[] = ["CONTENT_PLAN", "TEXT_GENERATE", "TEXT_CHECK", "IMAGE_GENERATE", "IMAGE_CHECK", "HUMAN_APPROVAL", "PUBLICATION"];

function completedWorkflow(): V3Workflow {
  return {
    id: 42, workflow_execution_id: 42, workflow_version: 3,
    status: "SUCCEEDED", current_node_type: null, current_revision: 1,
    nodes: Object.fromEntries(nodeTypes.map((node_type, index) => [node_type, {
      id: index + 10, node_type, revision_no: 1, status: "SUCCEEDED",
      attempt: { current: 1, automatic_max: 1 }, result: null,
    }])) as V3Workflow["nodes"],
    human_approval: { waiting: false, node_execution_id: null, status: "SUCCEEDED" },
    images: [1, 2, 3, 4, 5].map((slide_no) => ({
      slide_no: slide_no as 1 | 2 | 3 | 4 | 5,
      file_name: `slide-${slide_no}.png`, mime_type: "image/png",
      image_url: `/workflow-executions/42/slides/${slide_no}/image?revision_no=1`,
    })),
  };
}

function setup() {
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:slide");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  vi.spyOn(api, "getSlideImageBlob").mockResolvedValue(new Blob(["image"]));
  vi.spyOn(api, "getPublications").mockResolvedValue({
    draft: { youtube_title: "", youtube_description: "", instagram_caption: "" },
    publications: [],
  });
  vi.spyOn(api, "connections").mockResolvedValue({ connections: [] });
}

describe("completed V3 card editing", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("edits only slide four once and shows progress after refresh", async () => {
    setup();
    let resolveEdit!: (result: { workflow_execution_id: number; status: string }) => void;
    const edit = vi.spyOn(api, "reviseCompleted").mockImplementation(() => new Promise((resolve) => { resolveEdit = resolve; }));
    const previous = completedWorkflow();
    const next: V3Workflow = {
      ...previous, status: "RUNNING", current_node_type: "IMAGE_GENERATE", current_revision: 2,
      editing_slides: [4], images: [], previous_images: previous.images,
    };
    vi.spyOn(api, "getWorkflow").mockResolvedValue(next);
    const approve = vi.spyOn(api, "approve");
    const publish = vi.spyOn(api, "publishInstagram");
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={previous} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: /다시 수정하기/ }));
    expect(screen.getByRole("dialog", { name: "카드뉴스 수정하기" })).toBeInTheDocument();
    for (const slideNo of [1, 2, 3, 5]) fireEvent.click(screen.getByRole("button", { name: `${slideNo}번 슬라이드 수정` }));
    expect(screen.getByText("4번 슬라이드만 새로 만들어요.")).toBeInTheDocument();
    expect(screen.getByText("나머지 4장은 그대로 유지돼요.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "4번 슬라이드 수정" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.change(screen.getByPlaceholderText("예: 3번 이미지 상단에 제목을 추가하고 기존 색감은 유지해 주세요."), { target: { value: "4번 제목을 고쳐 주세요" } });
    const submit = screen.getByRole("button", { name: "선택한 1장 수정하기" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(edit).toHaveBeenCalledTimes(1);
    expect(edit).toHaveBeenCalledWith(42, { target: "IMAGE", slide_numbers: [4], feedback: "4번 제목을 고쳐 주세요" });
    expect(screen.getByRole("button", { name: "수정 시작 중…" })).toBeDisabled();
    resolveEdit({ workflow_execution_id: 42, status: "RUNNING" });
    await waitFor(() => expect(screen.getByText("4번 슬라이드를 수정하고 있어요.")).toBeInTheDocument());
    expect(screen.getByText("이전에 완료한 카드뉴스를 보고 있어요.")).toBeInTheDocument();
    expect(approve).not.toHaveBeenCalled();
    expect(publish).not.toHaveBeenCalled();
  });

  it("keeps the form open on an API error and supports cancel", async () => {
    setup();
    const edit = vi.spyOn(api, "reviseCompleted").mockRejectedValue(new ApiError(500, "수정 요청 실패"));
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={completedWorkflow()} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: /다시 수정하기/ }));
    fireEvent.change(screen.getByPlaceholderText("예: 3번 이미지 상단에 제목을 추가하고 기존 색감은 유지해 주세요."), { target: { value: "제목 수정" } });
    fireEvent.click(screen.getByRole("button", { name: "선택한 5장 수정하기" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(edit).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "선택한 5장 수정하기" })).not.toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "취소" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("supports multiple slide selection and selecting all again", () => {
    setup();
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={completedWorkflow()} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: /다시 수정하기/ }));
    for (const slideNo of [1, 3, 5]) fireEvent.click(screen.getByRole("button", { name: `${slideNo}번 슬라이드 수정` }));
    expect(screen.getByText("2장만 새로 만들어요")).toBeInTheDocument();
    expect(screen.getByText("나머지 3장은 그대로 유지돼요.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "전체 선택" }));
    expect(screen.getByText("5장만 새로 만들어요")).toBeInTheDocument();
  });

  it.each(["글", "구성"] as const)("keeps %s edits on the existing completed-edit API without slide numbers", async (label) => {
    setup();
    const edit = vi.spyOn(api, "reviseCompleted").mockResolvedValue({ workflow_execution_id: 42, status: "RUNNING" });
    vi.spyOn(api, "getWorkflow").mockResolvedValue({ ...completedWorkflow(), status: "RUNNING" });
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={completedWorkflow()} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: /다시 수정하기/ }));
    fireEvent.click(screen.getByRole("button", { name: label }));
    expect(screen.getByRole("button", { name: "1번 슬라이드 수정" })).toBeDisabled();
    fireEvent.change(screen.getByPlaceholderText("예: 3번 이미지 상단에 제목을 추가하고 기존 색감은 유지해 주세요."), { target: { value: "문구를 다듬어 주세요" } });
    fireEvent.click(screen.getByRole("button", { name: label === "글" ? "글 수정하기" : "구성 수정하기" }));
    await waitFor(() => expect(edit).toHaveBeenCalledWith(42, { target: label === "글" ? "TEXT" : "PLAN", feedback: "문구를 다듬어 주세요" }));
  });

  it("does not offer completed editing while waiting for approval", () => {
    setup();
    const workflow = completedWorkflow();
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={{ ...workflow, status: "WAITING_APPROVAL" }} /></MemoryRouter>);
    expect(screen.queryByRole("button", { name: /다시 수정하기/ })).not.toBeInTheDocument();
    expect(screen.getByText("수정된 카드뉴스를 확인하고 승인해 주세요.")).toBeInTheDocument();
  });

  it("closes on Escape, traps Tab, and restores focus to the trigger", () => {
    setup();
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={completedWorkflow()} /></MemoryRouter>);
    const trigger = screen.getByRole("button", { name: /다시 수정하기/ });
    trigger.focus();
    fireEvent.click(trigger);
    const close = screen.getByRole("button", { name: "수정 창 닫기" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(screen.getByRole("button", { name: "선택한 5장 수정하기" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});
