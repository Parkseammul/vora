import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { PlanPage, ScriptPage } from "./pages";
import { WorkflowProvider } from "./WorkflowContext";

vi.mock("./api", () => ({ api: { getWorkflow: vi.fn(), getPlan: vi.fn(), getScript: vi.fn(), revise: vi.fn() } }));

const workflow = {
  id: 1, status: "WAITING_APPROVAL" as const,
  nodes: {
    input_analysis: { id: 1, node_key: "input_analysis" as const, user_requested_version: 1, status: "SUCCESS" as const, attempt: { current: 1, automatic_max: 1 } },
    content_planning: { id: 2, node_key: "content_planning" as const, user_requested_version: 1, status: "WAITING_APPROVAL" as const, attempt: { current: 1, automatic_max: 1 } },
    script_generation: { id: 3, node_key: "script_generation" as const, user_requested_version: 1, status: "WAITING_APPROVAL" as const, attempt: { current: 1, automatic_max: 1 } },
    video_generation: null,
  },
};

function renderPage(page: React.ReactNode) {
  return render(<BrowserRouter><WorkflowProvider workflowExecutionId={1}>{page}</WorkflowProvider></BrowserRouter>);
}

async function submitRevision() {
  fireEvent.click(screen.getByRole("button", { name: "수정 요청" }));
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "같은 단계 수정" } });
  fireEvent.click(screen.getAllByRole("button", { name: "수정 요청" })[1]);
}

describe("same-stage revision detail refresh", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.getWorkflow).mockResolvedValue(workflow);
    vi.mocked(api.revise).mockResolvedValue({ restart_node: "content_planning" });
  });

  it("shows refreshed plan content after a same-stage revision", async () => {
    vi.mocked(api.getPlan)
      .mockResolvedValueOnce({ workflow_execution_id: 1, workflow_status: "WAITING_APPROVAL", node: workflow.nodes.content_planning, output: { concept: "기존 기획", hook: "기존", key_message: "기존", cta: "기존", visual_style: "기존", bgm_direction: "기존", scenes: [{ duration_seconds: 30 }] } })
      .mockResolvedValueOnce({ workflow_execution_id: 1, workflow_status: "WAITING_APPROVAL", node: workflow.nodes.content_planning, output: { concept: "수정된 기획", hook: "수정", key_message: "수정", cta: "수정", visual_style: "수정", bgm_direction: "수정", scenes: [{ duration_seconds: 30 }] } });
    renderPage(<PlanPage />);
    expect(await screen.findByText("기존 기획")).toBeInTheDocument();
    await submitRevision();
    await waitFor(() => expect(screen.getByText("수정된 기획")).toBeInTheDocument());
  });

  it("shows refreshed script content after a same-stage revision", async () => {
    vi.mocked(api.revise).mockResolvedValue({ restart_node: "script_generation" });
    vi.mocked(api.getPlan).mockResolvedValue({ workflow_execution_id: 1, workflow_status: "WAITING_APPROVAL", node: workflow.nodes.content_planning, output: { concept: "기획", hook: "훅", key_message: "메시지", cta: "CTA", visual_style: "스타일", bgm_direction: "BGM", scenes: [{ duration_seconds: 30 }] } });
    vi.mocked(api.getScript)
      .mockResolvedValueOnce({ workflow_execution_id: 1, workflow_status: "WAITING_APPROVAL", node: workflow.nodes.script_generation, output: { scenes: [{ planning_scene_id: "scene-1", narration: "기존 대본", subtitle: "기존", speaking_style: null }] } })
      .mockResolvedValueOnce({ workflow_execution_id: 1, workflow_status: "WAITING_APPROVAL", node: workflow.nodes.script_generation, output: { scenes: [{ planning_scene_id: "scene-1", narration: "수정된 대본", subtitle: "수정", speaking_style: null }] } });
    renderPage(<ScriptPage />);
    expect(await screen.findByText("Narration: 기존 대본")).toBeInTheDocument();
    await submitRevision();
    await waitFor(() => expect(screen.getByText("Narration: 수정된 대본")).toBeInTheDocument());
  });

  it("keeps the earlier-stage navigation modal for a script revision", async () => {
    vi.mocked(api.getPlan).mockResolvedValue({ workflow_execution_id: 1, workflow_status: "WAITING_APPROVAL", node: workflow.nodes.content_planning, output: { concept: "기획", hook: "훅", key_message: "메시지", cta: "CTA", visual_style: "스타일", bgm_direction: "BGM", scenes: [{ duration_seconds: 30 }] } });
    vi.mocked(api.getScript).mockResolvedValue({ workflow_execution_id: 1, workflow_status: "WAITING_APPROVAL", node: workflow.nodes.script_generation, output: { scenes: [{ planning_scene_id: "scene-1", narration: "대본", subtitle: "자막", speaking_style: null }] } });
    renderPage(<ScriptPage />);
    expect(await screen.findByText("Narration: 대본")).toBeInTheDocument();
    await submitRevision();
    expect(await screen.findByText("기획 단계부터 다시 생성 중입니다.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "현재 화면에 있기" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "기획 화면으로 이동" })).toBeInTheDocument();
  });
});
