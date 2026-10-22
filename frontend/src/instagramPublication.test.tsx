import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { PublicationPanel } from "./publication";
import type { PublicationDetail, Publication } from "./types";
import { WorkflowProvider } from "./WorkflowContext";

vi.mock("./api", () => ({ api: { getWorkflow: vi.fn(), getPublications: vi.fn(), connections: vi.fn(), authorize: vi.fn(), publishInstagram: vi.fn(), publicationEventUrl: vi.fn() } }));

class EventSourceStub {
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  close() {}
}

const detail = (status?: Publication["status"], url: string | null = null): PublicationDetail => ({
  draft: { youtube_title: "legacy", youtube_description: "legacy", instagram_caption: "Approved caption" },
  publications: status ? [{ id: 2, platform: "INSTAGRAM", status, external_post_url: url, error_message: status === "FAILED" ? "failed" : null, attempt: { current: 1, max: 3 } }] : [],
});

function renderPanel() {
  return render(<BrowserRouter><WorkflowProvider workflowExecutionId={12}><PublicationPanel /></WorkflowProvider></BrowserRouter>);
}

describe("Instagram-only publication UI", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.stubGlobal("EventSource", EventSourceStub);
    vi.mocked(api.getWorkflow).mockResolvedValue({
      id: 12, status: "SUCCESS",
      nodes: { input_analysis: null, content_planning: null, script_generation: null, video_generation: null },
    });
    vi.mocked(api.getPublications).mockResolvedValue(detail());
    vi.mocked(api.connections).mockResolvedValue({ connections: [{ platform: "INSTAGRAM", account_name: "account", status: "CONNECTED" }] });
    vi.mocked(api.publicationEventUrl).mockReturnValue("/events");
    vi.mocked(api.publishInstagram).mockResolvedValue(detail("PENDING"));
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("shows only Instagram controls and no YouTube inputs", async () => {
    renderPanel();
    expect(await screen.findByDisplayValue("Approved caption")).toBeInTheDocument();
    expect(screen.getByText("Instagram publication")).toBeInTheDocument();
    expect(screen.queryByText("YouTube title")).not.toBeInTheDocument();
    expect(screen.queryByText("YOUTUBE")).not.toBeInTheDocument();
  });

  it("does not publish until confirmation and sends the exact Instagram contract", async () => {
    renderPanel();
    await screen.findByDisplayValue("Approved caption");
    fireEvent.click(screen.getByRole("button", { name: "Request Instagram publication" }));
    expect(api.publishInstagram).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm Instagram publication" }));
    await waitFor(() => expect(api.publishInstagram).toHaveBeenCalledWith(12, {
      platforms: ["INSTAGRAM"], instagram_caption: "Approved caption", force_republish: false,
    }));
  });

  it("prevents duplicate confirmation clicks while the request is in flight", async () => {
    let finish: (value: PublicationDetail) => void = () => undefined;
    vi.mocked(api.publishInstagram).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    renderPanel();
    await screen.findByDisplayValue("Approved caption");
    fireEvent.click(screen.getByRole("button", { name: "Request Instagram publication" }));
    const confirm = screen.getByRole("button", { name: "Confirm Instagram publication" });
    fireEvent.click(confirm); fireEvent.click(confirm);
    expect(api.publishInstagram).toHaveBeenCalledTimes(1);
    finish(detail("PENDING"));
    await waitFor(() => expect(api.getPublications).toHaveBeenCalledTimes(2));
  });

  it.each(["PENDING", "PUBLISHING", "PUBLISHED", "FAILED"] as const)("renders %s safely", async (status) => {
    vi.mocked(api.getPublications).mockResolvedValue(detail(status, status === "PUBLISHED" ? "https://instagram.com/p/1" : null));
    renderPanel();
    expect(await screen.findByText(new RegExp(`Instagram: ${status}`))).toBeInTheDocument();
    if (status === "PENDING" || status === "PUBLISHING" || status === "PUBLISHED") {
      expect(screen.getByRole("button", { name: "Request Instagram publication" })).toBeDisabled();
    }
    if (status === "PUBLISHING") expect(screen.getByText(/no automatic retry/)).toBeInTheDocument();
    if (status === "PUBLISHED") expect(screen.getByRole("link", { name: "View post" })).toHaveAttribute("href", "https://instagram.com/p/1");
    if (status === "FAILED") expect(screen.getByText(/Automatic republishing is disabled/)).toBeInTheDocument();
  });
});
