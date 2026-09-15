import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { PublicationPanel, SocialConnectButton } from "./publication";
import { ApprovalActions } from "./components";
import type { PublicationDetail, Publication } from "./types";
import { WorkflowProvider } from "./WorkflowContext";

vi.mock("./api", () => ({ api: { getWorkflow: vi.fn(), getPublications: vi.fn(), connections: vi.fn(), authorize: vi.fn(), publish: vi.fn(), publicationEventUrl: vi.fn(), assetUrl: vi.fn() } }));

class EventSourceStub {
  static instances: EventSourceStub[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() { EventSourceStub.instances.push(this); }
  close() {}
}

const draft = { youtube_title: "Draft title", youtube_description: "Draft description", instagram_caption: "Draft caption" };
const publication = (status: Publication["status"] = "PENDING"): PublicationDetail => ({ draft, publications: [{ id: 1, platform: "YOUTUBE", status, external_post_url: null, error_message: status === "FAILED" ? "failed" : null, attempt: { current: 1, max: 3 } }, { id: 2, platform: "INSTAGRAM", status: "FAILED", external_post_url: null, error_message: "failed", attempt: { current: 3, max: 3 } }] });
const platformState = (value: string) => screen.getByText((_, element) => element?.tagName === "P" && element.textContent?.includes(value) === true);

function renderPanel() { return render(<BrowserRouter><WorkflowProvider workflowExecutionId={1}><PublicationPanel /></WorkflowProvider></BrowserRouter>); }

describe("publication UI", () => {
  beforeEach(() => {
    vi.resetAllMocks(); EventSourceStub.instances = [];
    vi.stubGlobal("EventSource", EventSourceStub);
    vi.mocked(api.getWorkflow).mockResolvedValue({ id: 1, status: "SUCCESS", nodes: { input_analysis: null, content_planning: null, script_generation: null, video_generation: null } });
    vi.mocked(api.getPublications).mockResolvedValue(publication());
    vi.mocked(api.connections).mockResolvedValue({ connections: [{ platform: "YOUTUBE", account_name: "channel", status: "CONNECTED" }] });
    vi.mocked(api.publicationEventUrl).mockReturnValue("/events");
    vi.mocked(api.publish).mockResolvedValue(publication());
  });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it("renders drafts, independent platform states, and only retries the failed platform", async () => {
    renderPanel();
    expect(await screen.findByDisplayValue("Draft title")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Draft description")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Draft caption")).toBeInTheDocument();
    expect(platformState("YOUTUBE: PENDING (1/3)")).toBeInTheDocument();
    expect(platformState("INSTAGRAM: FAILED (3/3)")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "다시 게시" }));
    await waitFor(() => expect(api.publish).toHaveBeenCalledWith(1, expect.objectContaining({ platforms: ["INSTAGRAM"], force_republish: true })));
  });

  it("mounts publication UI only after final video approval", async () => {
    vi.mocked(api.getWorkflow).mockResolvedValueOnce({ id: 1, status: "WAITING_APPROVAL", nodes: { input_analysis: null, content_planning: null, script_generation: null, video_generation: { id: 4, node_key: "video_generation", user_requested_version: 1, status: "WAITING_APPROVAL", attempt: { current: 1, automatic_max: 3 } } } });
    const pending = render(<BrowserRouter><WorkflowProvider workflowExecutionId={1}><ApprovalActions nodeKey="video_generation" /></WorkflowProvider></BrowserRouter>);
    await waitFor(() => expect(screen.queryByText("SNS 게시")).not.toBeInTheDocument());
    pending.unmount();
    vi.mocked(api.getWorkflow).mockResolvedValueOnce({ id: 1, status: "SUCCESS", nodes: { input_analysis: null, content_planning: null, script_generation: null, video_generation: { id: 4, node_key: "video_generation", user_requested_version: 1, status: "SUCCESS", attempt: { current: 1, automatic_max: 3 } } } });
    render(<BrowserRouter><WorkflowProvider workflowExecutionId={1}><ApprovalActions nodeKey="video_generation" /></WorkflowProvider></BrowserRouter>);
    expect(await screen.findByText("SNS 게시")).toBeInTheDocument();
  });

  it("does not publish until the confirmation modal is accepted", async () => {
    renderPanel(); await screen.findByDisplayValue("Draft title");
    const buttons = screen.getAllByRole("button");
    fireEvent.click(buttons.at(-1)!);
    expect(api.publish).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole("button").at(-1)!);
    await waitFor(() => expect(api.publish).toHaveBeenCalledWith(1, expect.objectContaining({ force_republish: false })));
  });

  it("starts OAuth with the current video return path", async () => {
    vi.mocked(api.authorize).mockResolvedValue({ authorization_url: "https://provider.test" });
    render(<BrowserRouter><SocialConnectButton platform="INSTAGRAM" /></BrowserRouter>);
    fireEvent.click(screen.getByRole("button"));
    await waitFor(() => expect(api.authorize).toHaveBeenCalledWith("INSTAGRAM", "/"));
  });

  it("refreshes publication data after an SSE event without losing another platform", async () => {
    vi.mocked(api.getPublications).mockResolvedValueOnce(publication("PUBLISHING")).mockResolvedValueOnce({ ...publication("SUCCESS"), publications: [{ ...publication("SUCCESS").publications[0], status: "SUCCESS" }, publication("SUCCESS").publications[1] ] });
    renderPanel(); expect(await screen.findByText("YOUTUBE: PUBLISHING (1/3)")).toBeInTheDocument();
    EventSourceStub.instances[0].onmessage?.(new MessageEvent("message", { data: "{}" }));
    await waitFor(() => expect(platformState("YOUTUBE: SUCCESS (1/3)")).toBeInTheDocument());
    expect(platformState("INSTAGRAM: FAILED (3/3)")).toBeInTheDocument();
  });
});
