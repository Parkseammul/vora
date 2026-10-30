import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./api";
import { DashboardPage } from "./dashboard";

const authFixture = vi.hoisted(() => ({ state: "authenticated", email: "", logout: vi.fn() }));
vi.mock("./auth", () => ({
  useAuth: () => ({ state: authFixture.state, user: authFixture.email ? { profile: { email: authFixture.email } } : null, logout: authFixture.logout }),
}));

function renderDashboard() {
  if (!vi.isMockFunction(api.getRecentWorkflows)) vi.spyOn(api, "getRecentWorkflows").mockResolvedValue({ items: [], next_cursor: null });
  return render(<MemoryRouter initialEntries={["/request"]}><Routes><Route path="/request" element={<DashboardPage />} /><Route path="/login" element={<p>Login required</p>} /><Route path="/workflows/:id" element={<p>V3 workflow opened</p>} /><Route path="/workflows/:id/plan" element={<p>Workflow plan opened</p>} /></Routes></MemoryRouter>);
}

describe("Creative Studio dashboard", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); authFixture.state = "authenticated"; authFixture.email = ""; authFixture.logout.mockClear(); window.sessionStorage.clear(); });

  it("shows a compact account pill and keeps logout available", () => {
    authFixture.email = "hello@vora.test";
    renderDashboard();
    expect(screen.getByTitle("hello@vora.test")).toHaveTextContent("hello@vora.test");
    expect(screen.getByRole("navigation", { name: "주 탐색" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "로그아웃" }));
    expect(authFixture.logout).toHaveBeenCalledTimes(1);
  });

  it("keeps Studio public but protects creation and restores only safe text", async () => {
    authFixture.state = "unauthenticated";
    const create = vi.spyOn(api, "createWorkflow");
    const first = renderDashboard();
    expect(screen.getByRole("link", { name: "로그인" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/콘텐츠 주제/), { target: { value: "로그인 후 복원할 주제" } });
    fireEvent.click(screen.getByRole("button", { name: /AI로 생성하기/ }));
    expect(await screen.findByText("Login required")).toBeInTheDocument();
    expect(create).not.toHaveBeenCalled();
    first.unmount();

    authFixture.state = "authenticated";
    renderDashboard();
    expect(screen.getByLabelText(/콘텐츠 주제/)).toHaveValue("로그인 후 복원할 주제");
    expect(screen.getByRole("status")).toHaveTextContent("이미지는 보안상 저장되지 않으니 다시 선택해주세요");
  });

  it("uses the local reference as decorative artwork, not a fake UI control", () => {
    const { container } = renderDashboard();
    expect(container.querySelector(".mascot-image")).toHaveAttribute("src", "/__prototype__/home.png");
    expect(container.querySelector(".mascot-image")).toHaveAttribute("alt", "");
    expect(container.querySelector(".mascot-placeholder")).not.toBeInTheDocument();
    expect(container.querySelectorAll(".float-card")).toHaveLength(4);
  });

  it("shows the requested brand message, five-slide canvas, and honest empty states", async () => {
    renderDashboard();
    expect(screen.getByRole("heading", { name: /당신의 아이디어가.*콘텐츠가 되는 순간/ })).toBeInTheDocument();
    expect(screen.getByText("V3 / 5 SLIDES")).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: /번째 슬라이드 선택/ })).toHaveLength(0);
    expect(screen.getByText("아직 생성된 카드뉴스가 없어요")).toBeInTheDocument();
    expect(await screen.findByText("아직 연결된 프로젝트 목록이 없어요.")).toBeInTheDocument();
    expect(screen.getByText(/Instagram 게시는 원할 때 별도로 진행할 수 있어요/)).toBeInTheDocument();
  });

  it("requires a topic and a source image before generation", () => {
    const create = vi.spyOn(api, "createWorkflow");
    renderDashboard();
    fireEvent.click(screen.getByRole("button", { name: /AI로 생성하기/ }));
    expect(screen.getByRole("alert")).toHaveTextContent("콘텐츠 주제");
    fireEvent.change(screen.getByLabelText(/콘텐츠 주제/), { target: { value: "신제품 소개" } });
    fireEvent.click(screen.getByRole("button", { name: /AI로 생성하기/ }));
    expect(screen.getByRole("alert")).toHaveTextContent("기준 이미지 1장");
    expect(create).not.toHaveBeenCalled();
  });

  it("keeps the uploaded source separate from empty generated slides and supports arrow keys", () => {
    vi.stubGlobal("URL", class extends URL { static createObjectURL = vi.fn(() => "blob:source"); static revokeObjectURL = vi.fn(); });
    renderDashboard();
    fireEvent.change(screen.getByLabelText(/기준 이미지/), { target: { files: [new File(["image"], "source.png", { type: "image/png" })] } });
    expect(screen.getByAltText("업로드한 기준 이미지 미리보기")).toBeInTheDocument();
    const first = screen.getByRole("button", { name: "1번째 슬라이드 선택" });
    fireEvent.keyDown(first, { key: "ArrowRight" });
    expect(screen.getByText("02 / 05")).toBeInTheDocument();
    expect(screen.queryByAltText("업로드한 기준 이미지 미리보기")).not.toBeInTheDocument();
    expect(screen.getByText("2번 슬라이드 이미지 없음")).toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("renders recent projects with status and opens the workflow detail", async () => {
    const recent = vi.spyOn(api, "getRecentWorkflows").mockResolvedValue({ items: [{
      workflow_execution_id: 9242, workflow_version: 3, status: "SUCCEEDED", title: "브랜드 카드뉴스", created_at: "2026-10-30T04:47:59Z", updated_at: "2026-10-30T10:04:10Z", completed_at: "2026-10-30T10:04:10Z", thumbnail_url: null,
    }], next_cursor: null });
    renderDashboard();
    await waitFor(() => expect(screen.getByRole("link", { name: /브랜드 카드뉴스, 제작 완료/ })).toHaveAttribute("href", "/workflows/9242"));
    expect(recent).toHaveBeenCalledWith(5, undefined, expect.any(AbortSignal));
    expect(screen.getByText("제작 완료")).toBeInTheDocument();
    expect(screen.getByText("마지막 수정 10월 30일")).toBeInTheDocument();
  });

  it("opens the newest owned project from the header using the loaded list once", async () => {
    const recent = vi.spyOn(api, "getRecentWorkflows").mockResolvedValue({ items: [
      { workflow_execution_id: 9242, workflow_version: 3, status: "SUCCEEDED", title: "최신 카드뉴스", created_at: "2026-10-30T04:00:00Z", updated_at: "2026-10-30T10:00:00Z", completed_at: "2026-10-30T10:00:00Z", thumbnail_url: null },
      { workflow_execution_id: 42, workflow_version: 1, status: "FAILED", title: "이전 카드뉴스", created_at: "2026-10-29T04:00:00Z", updated_at: "2026-10-29T10:00:00Z", completed_at: null, thumbnail_url: null },
    ], next_cursor: null });
    renderDashboard();
    await screen.findByRole("link", { name: /최신 카드뉴스, 제작 완료/ });
    fireEvent.click(screen.getByRole("button", { name: "최근 프로젝트" }));
    expect(await screen.findByText("V3 workflow opened")).toBeInTheDocument();
    expect(recent).toHaveBeenCalledTimes(1);
  });

  it("scrolls to the empty recent projects section without opening a workflow", async () => {
    const scrollIntoView = vi.fn();
    const originalScroll = Object.getOwnPropertyDescriptor(Element.prototype, "scrollIntoView");
    Object.defineProperty(Element.prototype, "scrollIntoView", { configurable: true, value: scrollIntoView });
    const recent = vi.spyOn(api, "getRecentWorkflows").mockResolvedValue({ items: [], next_cursor: null });
    renderDashboard();
    await screen.findByText("아직 연결된 프로젝트 목록이 없어요.");
    fireEvent.click(screen.getByRole("button", { name: "최근 프로젝트" }));
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth" });
    expect(recent).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("V3 workflow opened")).not.toBeInTheDocument();
    if (originalScroll) Object.defineProperty(Element.prototype, "scrollIntoView", originalScroll);
    else Reflect.deleteProperty(Element.prototype, "scrollIntoView");
  });

  it("uses the existing login entry flow when the recent button is clicked signed out", () => {
    authFixture.state = "unauthenticated";
    const recent = vi.spyOn(api, "getRecentWorkflows");
    renderDashboard();
    fireEvent.click(screen.getByRole("button", { name: "최근 프로젝트" }));
    expect(screen.getByText("Login required")).toBeInTheDocument();
    expect(recent).not.toHaveBeenCalled();
  });

  it("blocks repeated header clicks while loading and offers retry after an API error", async () => {
    let rejectRequest!: (reason: unknown) => void;
    const recent = vi.spyOn(api, "getRecentWorkflows").mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectRequest = reject; }));
    renderDashboard();
    await waitFor(() => expect(recent).toHaveBeenCalledTimes(1));
    const button = screen.getByRole("button", { name: "불러오는 중…" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    fireEvent.click(button);
    expect(recent).toHaveBeenCalledTimes(1);
    rejectRequest(new Error("private details"));
    await screen.findByRole("alert");
    let rejectHeader!: (reason: unknown) => void;
    recent.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectHeader = reject; }));
    fireEvent.click(screen.getByRole("button", { name: "최근 프로젝트" }));
    fireEvent.click(screen.getByRole("button", { name: "불러오는 중…" }));
    expect(recent).toHaveBeenCalledTimes(2);
    rejectHeader(new ApiError(500, "private server details"));
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.getAllByRole("alert")[0]).toHaveTextContent("서버에서 프로젝트 목록을 불러오지 못했어요.");
    expect(screen.getAllByRole("alert")[0]).not.toHaveTextContent("private server details");
    recent.mockResolvedValueOnce({ items: [], next_cursor: null });
    fireEvent.click(screen.getAllByRole("button", { name: "다시 시도" })[0]);
    await screen.findByText("아직 연결된 프로젝트 목록이 없어요.");
    expect(recent).toHaveBeenCalledTimes(3);
  });

  it("shows a retry action when recent projects fail to load", async () => {
    const recent = vi.spyOn(api, "getRecentWorkflows").mockRejectedValue(new Error("network"));
    renderDashboard();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("프로젝트를 불러오지 못했어요"));
    expect(recent).toHaveBeenCalledTimes(1);
    recent.mockResolvedValue({ items: [], next_cursor: null });
    fireEvent.click(screen.getByRole("button", { name: "다시 시도" }));
    await waitFor(() => expect(screen.getByText("아직 연결된 프로젝트 목록이 없어요.")).toBeInTheDocument());
    expect(recent).toHaveBeenCalledTimes(2);
  });

  it("shows safe, actionable messages for recent project API errors", async () => {
    const recent = vi.spyOn(api, "getRecentWorkflows").mockRejectedValue(new ApiError(401, "Authentication required"));
    renderDashboard();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("로그인이 만료됐어요. 다시 로그인해 주세요."));
    expect(screen.getByRole("alert")).not.toHaveTextContent("Authentication required");
    recent.mockRejectedValue(new ApiError(500, "database details"));
    fireEvent.click(screen.getByRole("button", { name: "다시 시도" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("서버에서 프로젝트 목록을 불러오지 못했어요."));
    expect(screen.getByRole("alert")).not.toHaveTextContent("database details");
  });

  it("submits the existing workflow contract and opens the plan page", async () => {
    vi.stubEnv("VITE_VORA_RULES_VERSION_ID", "7291");
    vi.stubGlobal("URL", class extends URL { static createObjectURL = vi.fn(() => "blob:source"); static revokeObjectURL = vi.fn(); });
    const create = vi.spyOn(api, "createWorkflow").mockResolvedValue({ workflow_execution_id: 42 });
    renderDashboard();
    fireEvent.change(screen.getByLabelText(/콘텐츠 주제/), { target: { value: "  신제품 소개  " } });
    const file = new File(["image"], "source.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText(/기준 이미지/), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: /AI로 생성하기/ }));
    await waitFor(() => expect(screen.getByText("V3 workflow opened")).toBeInTheDocument());
    expect(create).toHaveBeenCalledWith("신제품 소개", file, "Instagram 카드뉴스 제작", 7291);
    vi.unstubAllGlobals();
  });
});
