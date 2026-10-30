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

  it("renders the redesigned detail shell without invoking workflow actions", () => {
    vi.spyOn(api, "getSlideImageBlob").mockResolvedValue(new Blob(["image"]));
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={9242} initialWorkflow={workflow()} /></MemoryRouter>);
    expect(screen.getByRole("main")).toHaveClass("v3-detail-shell");
    expect(screen.getByRole("heading", { name: "아이디어가 콘텐츠가 되는 순간" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "카드뉴스 미리보기" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "새로고침" })).toBeEnabled();
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
  });

  it("renders five user stages and authenticated image previews", async () => {
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:slide");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(api, "getSlideImageBlob").mockResolvedValue(new Blob(["image"], { type: "image/png" }));
    const { unmount } = render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={workflow()} /></MemoryRouter>);
    for (const label of ["기획", "글 작성", "이미지 제작", "최종 확인", "게시 준비"]) expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getAllByText("확인 대기 중").length).toBeGreaterThan(0);
    expect(screen.queryByText(/Revision|NodeExecution|REUSED/)).not.toBeInTheDocument();
    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(screen.getByAltText("1번 카드뉴스")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "3번 슬라이드" }));
    await waitFor(() => expect(screen.getByAltText("3번 카드뉴스")).toBeInTheDocument());
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });

  it("uses the current approval id for approve and sends rejection revision fields", async () => {
    vi.spyOn(api, "getSlideImageBlob").mockResolvedValue(new Blob(["image"]));
    const approve = vi.spyOn(api, "approve").mockResolvedValue({ workflow_execution_id: 42, status: "REVISING" });
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={workflow()} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "이대로 승인하기" }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith(42, 16, "APPROVED", undefined, undefined, undefined));
    fireEvent.click(screen.getByRole("button", { name: "수정 요청" }));
    fireEvent.change(screen.getByPlaceholderText("바꾸고 싶은 내용을 알려 주세요"), { target: { value: "이미지 톤을 바꿔주세요" } });
    fireEvent.click(screen.getByRole("button", { name: "수정 요청 보내기" }));
    await waitFor(() => expect(approve).toHaveBeenCalledWith(42, 16, "REJECTED", "TEXT", "이미지 톤을 바꿔주세요", undefined));
  });

  it("keeps current and previous slide previews distinct and supports keyboard navigation", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:slide");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const blobs = vi.spyOn(api, "getSlideImageBlob").mockResolvedValue(new Blob(["image"]));
    const current = workflow();
    const previous_images = current.images.map((asset) => ({ ...asset, image_url: `${asset.image_url}?revision_no=1` }));
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={{ ...current, previous_images }} /></MemoryRouter>);
    const first = screen.getByRole("tab", { name: "1번 슬라이드" });
    first.focus();
    fireEvent.keyDown(first, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "2번 슬라이드" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "2번 슬라이드" })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("button", { name: "이전 카드뉴스" }));
    expect(screen.getByText("이전에 완료한 카드뉴스를 보고 있어요.")).toBeInTheDocument();
    await waitFor(() => expect(blobs).toHaveBeenCalledWith("/workflow-executions/42/slides/2/image?revision_no=1", expect.any(AbortSignal)));
    fireEvent.click(screen.getByRole("button", { name: "현재 카드뉴스" }));
    expect(screen.getByRole("button", { name: "현재 카드뉴스" })).toHaveAttribute("aria-pressed", "true");
  });

  it("downloads the selected real image through the authenticated client", async () => {
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:slide");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const blob = vi.spyOn(api, "getSlideImageBlob").mockResolvedValue(new Blob(["image"]));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={workflow()} /></MemoryRouter>);
    fireEvent.click(screen.getByRole("tab", { name: "4번 슬라이드" }));
    fireEvent.click(screen.getByRole("button", { name: "이미지 저장" }));
    await waitFor(() => expect(blob).toHaveBeenCalledWith("/workflow-executions/42/slides/4/image"));
    expect(click).toHaveBeenCalledTimes(1);
  });
});
