import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { DashboardPage } from "./dashboard";

function renderDashboard() {
  return render(<MemoryRouter initialEntries={["/request"]}><Routes><Route path="/request" element={<DashboardPage />} /><Route path="/workflows/:id" element={<p>V3 workflow opened</p>} /><Route path="/workflows/:id/plan" element={<p>Workflow plan opened</p>} /></Routes></MemoryRouter>);
}

describe("Creative Studio dashboard", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); });

  it("uses the local reference as decorative artwork, not a fake UI control", () => {
    const { container } = renderDashboard();
    expect(container.querySelector(".mascot-image")).toHaveAttribute("src", "/__prototype__/home.png");
    expect(container.querySelector(".mascot-image")).toHaveAttribute("alt", "");
    expect(container.querySelector(".mascot-placeholder")).not.toBeInTheDocument();
    expect(container.querySelectorAll(".float-card")).toHaveLength(4);
  });

  it("shows the requested brand message, five-slide canvas, and honest empty states", () => {
    renderDashboard();
    expect(screen.getByRole("heading", { name: /당신의 아이디어가.*콘텐츠가 되는 순간/ })).toBeInTheDocument();
    expect(screen.getByText("V3 / 5 SLIDES")).toBeInTheDocument();
    expect(screen.queryAllByRole("button", { name: /번째 슬라이드 선택/ })).toHaveLength(0);
    expect(screen.getByText("아직 생성된 카드뉴스가 없어요")).toBeInTheDocument();
    expect(screen.getByText("아직 연결된 프로젝트 목록이 없어요.")).toBeInTheDocument();
    expect(screen.getByText(/아직 조회한 Workflow가 없습니다/)).toBeInTheDocument();
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

  it("submits the existing workflow contract and opens the plan page", async () => {
    vi.stubGlobal("URL", class extends URL { static createObjectURL = vi.fn(() => "blob:source"); static revokeObjectURL = vi.fn(); });
    const create = vi.spyOn(api, "createWorkflow").mockResolvedValue({ workflow_execution_id: 42 });
    renderDashboard();
    fireEvent.change(screen.getByLabelText(/콘텐츠 주제/), { target: { value: "  신제품 소개  " } });
    const file = new File(["image"], "source.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText(/기준 이미지/), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: /AI로 생성하기/ }));
    await waitFor(() => expect(screen.getByText("V3 workflow opened")).toBeInTheDocument());
    expect(create).toHaveBeenCalledWith("신제품 소개", file);
    vi.unstubAllGlobals();
  });
});
