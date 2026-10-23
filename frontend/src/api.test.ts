import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

describe("Instagram publication API", () => {
  afterEach(() => vi.unstubAllGlobals());

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
