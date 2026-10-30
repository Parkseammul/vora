import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { V3WorkflowPage } from "./v3Workflow";
import type { Publication, PublicationDetail, V3NodeType, V3Workflow } from "./types";

const nodeTypes: V3NodeType[] = ["CONTENT_PLAN", "TEXT_GENERATE", "TEXT_CHECK", "IMAGE_GENERATE", "IMAGE_CHECK", "HUMAN_APPROVAL", "PUBLICATION"];
const draft = { youtube_title: "", youtube_description: "", instagram_caption: "다섯 장의 이야기 #VORA" };

function workflow(): V3Workflow {
  return {
    id: 42, workflow_execution_id: 42, workflow_version: 3, status: "SUCCEEDED", current_node_type: null, current_revision: 1,
    nodes: Object.fromEntries(nodeTypes.map((node_type, index) => [node_type, { id: index + 10, node_type, revision_no: 1, status: "SUCCEEDED", attempt: { current: 1, automatic_max: 1 }, result: null }])) as V3Workflow["nodes"],
    human_approval: { waiting: false, node_execution_id: null, status: "SUCCEEDED" },
    images: [1, 2, 3, 4, 5].map((slide_no) => ({ slide_no: slide_no as 1 | 2 | 3 | 4 | 5, file_name: `slide-${slide_no}.png`, mime_type: "image/png", image_url: `/workflow-executions/42/slides/${slide_no}/image?revision_no=1` })),
  };
}

function publication(status: Publication["status"], url: string | null = null): Publication {
  return { id: 7, platform: "INSTAGRAM", status, revision_no: 1, external_post_url: url, error_message: null, attempt: { current: 1, max: 3 } };
}

function setup(publications: Publication[] = []) {
  const details: PublicationDetail = { draft, publications };
  const get = vi.spyOn(api, "getPublications").mockResolvedValue(details);
  const accounts = vi.spyOn(api, "connections").mockResolvedValue({ connections: [{ platform: "INSTAGRAM", account_name: "@vora.studio", status: "CONNECTED" }] });
  const post = vi.spyOn(api, "publishInstagram").mockResolvedValue({ ...details, publications: [publication("PENDING")] });
  vi.spyOn(api, "getSlideImageBlob").mockResolvedValue(new Blob(["image"]));
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:slide");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  return { get, accounts, post };
}

function renderPage(value = workflow()) { return render(<MemoryRouter><V3WorkflowPage workflowExecutionId={42} initialWorkflow={value} /></MemoryRouter>); }
async function openDialog() {
  const trigger = await screen.findByRole("button", { name: "Instagram에 게시하기 ↗" });
  await waitFor(() => expect(trigger).toBeEnabled());
  fireEvent.click(trigger);
  return screen.getByRole("dialog", { name: /이 콘텐츠, 세상에 보여줄까/ });
}

describe("V3 Instagram publication confirmation", () => {
  afterEach(() => { cleanup(); vi.restoreAllMocks(); window.sessionStorage.clear(); });

  it("previews the five actual slides, account and caption without posting on open or cancel", async () => {
    const { post, get } = setup();
    renderPage();
    const dialog = await openDialog();
    expect(dialog).toHaveTextContent("READY TO GO LIVE");
    expect(dialog).toHaveTextContent("총 5장의 현재 카드뉴스가 게시돼요.");
    expect(dialog).toHaveTextContent("@vora.studio");
    expect(screen.getByLabelText("게시될 캡션")).toHaveValue(draft.instagram_caption);
    expect(screen.getAllByRole("button", { name: /번 게시 이미지 보기/ })).toHaveLength(5);
    fireEvent.click(screen.getByRole("button", { name: "3번 게시 이미지 보기" }));
    expect(dialog).toHaveTextContent("3 / 5");
    expect(post).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "아직 아니야" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("supports Escape, backdrop close, focus trapping and focus restoration", async () => {
    setup(); renderPage();
    const trigger = await screen.findByRole("button", { name: "Instagram에 게시하기 ↗" });
    await openDialog();
    const close = screen.getByRole("button", { name: "게시 창 닫기" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(screen.getByRole("button", { name: "응, 게시할래 ↗" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    const backdrop = screen.getByRole("dialog").parentElement!;
    fireEvent.mouseDown(backdrop);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("posts exactly once after explicit confirmation and shows PENDING, then PUBLISHING", async () => {
    const { post, get } = setup();
    let resolvePost!: (value: PublicationDetail) => void;
    post.mockImplementationOnce(() => new Promise((resolve) => { resolvePost = resolve; }));
    renderPage(); await openDialog();
    fireEvent.change(screen.getByLabelText("게시될 캡션"), { target: { value: "  직접 쓴 캡션  " } });
    const confirm = screen.getByRole("button", { name: "응, 게시할래 ↗" });
    fireEvent.click(confirm); fireEvent.click(confirm);
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(post).toHaveBeenCalledWith(42, { platforms: ["INSTAGRAM"], instagram_caption: "직접 쓴 캡션", force_republish: false });
    expect(get).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "게시 요청 중…" })).toBeDisabled();
    resolvePost({ draft, publications: [publication("PENDING")] });
    expect(await screen.findByText("게시 요청을 접수했어요.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Instagram에 게시하기 ↗" })).not.toBeInTheDocument();
    get.mockResolvedValue({ draft, publications: [publication("PUBLISHING")] });
    fireEvent.click(screen.getByRole("button", { name: "게시 상태 다시 확인" }));
    expect(await screen.findByText("Instagram에 올리는 중이에요 ✦")).toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("restores PUBLISHED with only a valid Instagram HTTPS post link", async () => {
    const { post } = setup([publication("PUBLISHED", "https://www.instagram.com/p/ABC123/")]);
    renderPage();
    expect(await screen.findByText("It's live! ✳")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Instagram에서 보기 ↗" })).toHaveAttribute("href", "https://www.instagram.com/p/ABC123/");
    expect(screen.getByRole("link", { name: "Instagram에서 보기 ↗" })).toHaveAttribute("target", "_blank");
    expect(screen.getByRole("link", { name: "Instagram에서 보기 ↗" })).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.queryByRole("button", { name: "Instagram에 게시하기 ↗" })).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it.each([null, "http://www.instagram.com/p/ABC123/", "https://instagram.com.evil.test/p/ABC123/"])("does not link an unsafe or absent published URL (%s)", async (url) => {
    setup([publication("PUBLISHED", url)]); renderPage();
    expect(await screen.findByText("It's live! ✳")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Instagram에서 보기 ↗" })).not.toBeInTheDocument();
    expect(screen.getByText("게시물 링크는 아직 제공되지 않았어요.")).toBeInTheDocument();
  });

  it("shows FAILED without automatic or manual repost", async () => {
    const { post } = setup([publication("FAILED")]); renderPage();
    expect(await screen.findByText("앗, 게시를 완료하지 못했어요.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Instagram에 게시하기 ↗" })).not.toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("checks a pending publication on a bounded interval and shows only backend-confirmed success", async () => {
    let tick!: () => void;
    vi.spyOn(window, "setInterval").mockImplementation((callback, delay) => {
      if (delay === 15000) tick = callback as () => void;
      return 999;
    });
    const { get, post } = setup([publication("PENDING")]);
    get.mockResolvedValueOnce({ draft, publications: [publication("PENDING")] })
      .mockResolvedValueOnce({ draft, publications: [publication("PUBLISHED", "https://www.instagram.com/p/ABC123/")] });
    renderPage();
    expect(await screen.findByText("게시 요청을 접수했어요.")).toBeInTheDocument();
    await waitFor(() => expect(tick).toBeTypeOf("function"));
    act(() => tick());
    expect(await screen.findByText("It's live! ✳")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("preserves older publication history while allowing a new revision to request its own post", async () => {
    const older = { ...publication("PUBLISHED", "https://www.instagram.com/p/OLDER/"), revision_no: 0 };
    const { post } = setup([older]); renderPage();
    await openDialog();
    expect(screen.getByText("이전 카드뉴스 · Instagram 게시 완료")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("blocks publication without a connected Instagram account", async () => {
    const { accounts, post } = setup();
    accounts.mockResolvedValue({ connections: [{ platform: "INSTAGRAM", account_name: "@vora.studio", status: "EXPIRED" }] });
    renderPage();
    expect(await screen.findByText(/연결된 Instagram 계정이 없어요/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "계정 연결하기 ↗" })).toHaveAttribute("href", "/settings/social");
    expect(screen.getByRole("button", { name: "Instagram에 게시하기 ↗" })).toBeDisabled();
    expect(post).not.toHaveBeenCalled();
  });

  it("does not offer publication before the workflow and all five slides are ready", () => {
    const { get, post } = setup();
    const unfinished = workflow();
    unfinished.status = "WAITING_APPROVAL";
    unfinished.images = unfinished.images.slice(0, 4);
    renderPage(unfinished);
    expect(screen.queryByRole("button", { name: "Instagram에 게시하기 ↗" })).not.toBeInTheDocument();
    expect(get).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
  });

  it("blocks an unrecognized publication revision rather than risking another post", async () => {
    const { post } = setup([{ ...publication("PUBLISHED"), revision_no: null }]);
    renderPage();
    expect(await screen.findByText("게시 이력을 확인할 수 없어 새 요청을 보내지 않아요.")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("blocks a race when another request appears during the final status check", async () => {
    const { get, post } = setup();
    get.mockResolvedValueOnce({ draft, publications: [] }).mockResolvedValueOnce({ draft, publications: [publication("PUBLISHING")] });
    renderPage(); await openDialog();
    fireEvent.click(screen.getByRole("button", { name: "응, 게시할래 ↗" }));
    expect(await screen.findByText("Instagram에 올리는 중이에요 ✦")).toBeInTheDocument();
    expect(post).not.toHaveBeenCalled();
  });

  it("checks status but never reposts after an ambiguous network result", async () => {
    const { get, post } = setup();
    post.mockRejectedValueOnce(new Error("network timeout"));
    renderPage(); await openDialog();
    fireEvent.click(screen.getByRole("button", { name: "응, 게시할래 ↗" }));
    await waitFor(() => expect(get).toHaveBeenCalledTimes(3));
    expect(post).toHaveBeenCalledTimes(1);
    expect(screen.getByText("이전 게시 요청의 결과를 확인 중이에요. 자동으로 다시 요청하지 않아요.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Instagram에 게시하기 ↗" })).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem("vora.publication-uncertain:42:1")).toBe("1");
  });

  it("recovers publication information after an initial API error without posting", async () => {
    const { get, post } = setup();
    get.mockRejectedValueOnce(new Error("network unavailable"));
    renderPage();
    expect(await screen.findByText(/게시 정보나 계정 연결 상태를 확인하지 못했어요/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Instagram에 게시하기 ↗" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "게시 정보 다시 불러오기" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Instagram에 게시하기 ↗" })).toBeEnabled());
    expect(post).not.toHaveBeenCalled();
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("allows closing a confirmed request without cancelling or repeating the POST", async () => {
    const { post } = setup();
    let resolvePost!: (value: PublicationDetail) => void;
    post.mockImplementationOnce(() => new Promise((resolve) => { resolvePost = resolve; }));
    renderPage(); await openDialog();
    fireEvent.click(screen.getByRole("button", { name: "응, 게시할래 ↗" }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    expect(screen.getAllByText(/창을 닫아도 게시 요청은 취소되지 않아요/)).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "창만 닫기" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1);
    resolvePost({ draft, publications: [publication("PENDING")] });
    expect(await screen.findByText("게시 요청을 접수했어요.")).toBeInTheDocument();
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("stops waiting for a hung publication-information request and offers manual recovery", async () => {
    vi.useFakeTimers();
    try {
      const { get, post } = setup();
      get.mockImplementationOnce(() => new Promise(() => undefined));
      renderPage();
      await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
      expect(screen.getByText(/게시 정보나 계정 연결 상태를 확인하지 못했어요/)).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "게시 정보 다시 불러오기" })).toBeEnabled();
      expect(post).not.toHaveBeenCalled();
    } finally { vi.useRealTimers(); }
  });

  it("treats a timed-out POST as uncertain and never sends it again", async () => {
    const { get, post } = setup();
    post.mockImplementationOnce(() => new Promise(() => undefined));
    renderPage(); await openDialog();
    vi.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole("button", { name: "응, 게시할래 ↗" }));
      await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
      expect(post).toHaveBeenCalledTimes(1);
      expect(get).toHaveBeenCalledTimes(3);
      expect(screen.getByText("이전 게시 요청의 결과를 확인 중이에요. 자동으로 다시 요청하지 않아요.")).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Instagram에 게시하기 ↗" })).not.toBeInTheDocument();
    } finally { vi.useRealTimers(); }
  });
});
