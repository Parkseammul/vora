import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

describe("Instagram publication API", () => {
  afterEach(() => { configureAccessTokenProvider(undefined); vi.unstubAllGlobals(); });

  it("uses the workflow-scoped backend route and Instagram-only payload", async () => {
    const fetchStub = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ publications: [] }) });
    vi.stubGlobal("fetch", fetchStub);

    await api.publishInstagram(42, {
      platforms: ["INSTAGRAM"], instagram_caption: "caption", force_republish: false,
    });

    expect(fetchStub).toHaveBeenCalledWith("/workflow-executions/42/publications", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ platforms: ["INSTAGRAM"], instagram_caption: "caption", force_republish: false }),
    }));
  });
});

import { configureAccessTokenProvider } from "./api";

describe("frontend authentication transport", () => {
  it("adds the access token to API and image blob requests", async () => {
    configureAccessTokenProvider(() => "access-token");
    const fetchStub = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ publications: [] }) })
      .mockResolvedValueOnce({ ok: true, blob: async () => new Blob(["image"]) });
    vi.stubGlobal("fetch", fetchStub);

    await api.getPublications(7);
    await api.getSlideImageBlob("/workflow-executions/7/slides/1/image");

    expect(fetchStub.mock.calls[0][1]).toEqual(expect.objectContaining({ headers: expect.any(Headers) }));
    expect(((fetchStub.mock.calls[0][1] as RequestInit).headers as Headers).get("Authorization")).toBe("Bearer access-token");
    expect(((fetchStub.mock.calls[1][1] as RequestInit).headers as Headers).get("Authorization")).toBe("Bearer access-token");
  });

  it("signals session expiry for 401 while preserving 403 and 404 statuses", async () => {
    const expired = vi.fn(); window.addEventListener("vora:auth-expired", expired);
    const fetchStub = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 401, json: async () => ({ detail: "expired" }) })
      .mockResolvedValueOnce({ ok: false, status: 403, json: async () => ({ detail: "forbidden" }) })
      .mockResolvedValueOnce({ ok: false, status: 404, json: async () => ({ detail: "missing" }) });
    vi.stubGlobal("fetch", fetchStub);
    await expect(api.getWorkflow(1)).rejects.toMatchObject({ status: 401 });
    await expect(api.getWorkflow(1)).rejects.toMatchObject({ status: 403 });
    await expect(api.getWorkflow(1)).rejects.toMatchObject({ status: 404 });
    expect(expired).toHaveBeenCalledTimes(1);
    window.removeEventListener("vora:auth-expired", expired);
  });

  it("opens an authenticated SSE stream and aborts it on close", async () => {
    configureAccessTokenProvider(() => "sse-token");
    let resolveRead!: (value: { value?: Uint8Array; done: boolean }) => void;
    const read = vi.fn(() => new Promise<{ value?: Uint8Array; done: boolean }>((resolve) => { resolveRead = resolve; }));
    const fetchStub = vi.fn().mockResolvedValue({ ok: true, body: { getReader: () => ({ read }) } });
    vi.stubGlobal("fetch", fetchStub);
    const messages: string[] = [];
    const stream = await api.subscribeSse("/events", (message) => messages.push(message), vi.fn());
    await vi.waitFor(() => expect(fetchStub).toHaveBeenCalled());
    expect(((fetchStub.mock.calls[0][1] as RequestInit).headers as Headers).get("Authorization")).toBe("Bearer sse-token");
    resolveRead({ value: new TextEncoder().encode('data: {"stage":"DONE"}\n\n'), done: false });
    resolveRead({ done: true });
    await vi.waitFor(() => expect(messages).toEqual(['{"stage":"DONE"}']));
    stream.close();
    expect((fetchStub.mock.calls[0][1] as RequestInit).signal).toBeInstanceOf(AbortSignal);
    expect(((fetchStub.mock.calls[0][1] as RequestInit).signal as AbortSignal).aborted).toBe(true);
  });
});
