import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let managerInstance: MockUserManager;
class MockUserManager {
  settings: Record<string, unknown>;
  events = { addUserLoaded: vi.fn(), removeUserLoaded: vi.fn(), addUserUnloaded: vi.fn(), removeUserUnloaded: vi.fn(), addAccessTokenExpired: vi.fn(), removeAccessTokenExpired: vi.fn() };
  getUser = vi.fn().mockResolvedValue(null);
  signinRedirect = vi.fn().mockResolvedValue(undefined);
  signinCallback = vi.fn();
  signinSilent = vi.fn();
  signoutRedirect = vi.fn().mockResolvedValue(undefined);
  removeUser = vi.fn().mockResolvedValue(undefined);
  // The test needs the constructed OIDC manager to configure callback outcomes.
  // eslint-disable-next-line @typescript-eslint/no-this-alias
  constructor(settings: Record<string, unknown>) { this.settings = settings; managerInstance = this; }
}
vi.mock("oidc-client-ts", () => ({ UserManager: MockUserManager, WebStorageStateStore: class {} }));

function Location() { return <span data-testid="location">{useLocation().pathname}</span>; }
async function loadAuth(configureProviderNames = true) {
  vi.resetModules();
  vi.stubEnv("VITE_COGNITO_ISSUER", "https://cognito-idp.test/pool");
  vi.stubEnv("VITE_COGNITO_DOMAIN", "https://vora.auth.test");
  vi.stubEnv("VITE_COGNITO_CLIENT_ID", "spa-client");
  if (configureProviderNames) {
    vi.stubEnv("VITE_COGNITO_KAKAO_PROVIDER", "KakaoOIDC");
    vi.stubEnv("VITE_COGNITO_NATIVE_PROVIDER", "COGNITO");
  }
  return import("./auth");
}

describe("Cognito authentication flow", () => {
  beforeEach(() => { vi.unstubAllEnvs(); cleanup(); });
  afterEach(() => { cleanup(); vi.unstubAllEnvs(); });

  it("redirects an unauthenticated protected route to login once", async () => {
    const { AuthProvider, LoginPage, RequireAuth } = await loadAuth();
    render(<MemoryRouter initialEntries={["/workflows/42"]}><AuthProvider><Routes><Route path="/workflows/:id" element={<RequireAuth><p>protected</p></RequireAuth>} /><Route path="/login" element={<><LoginPage /><Location /></>} /></Routes></AuthProvider></MemoryRouter>);
    expect(await screen.findByRole("button", { name: "카카오 로그인" })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/login");
    expect(screen.getByRole("button", { name: "VORA 계정으로 로그인" })).toBeEnabled();
  });

  it("handles callback success and rejects an invalid state without redirect loops", async () => {
    const { AuthCallbackPage } = await loadAuth();
    managerInstance.signinCallback.mockResolvedValueOnce({ state: { returnTo: "/request" } });
    render(<StrictMode><MemoryRouter initialEntries={["/auth/callback?code=ok&state=valid"]}><Routes><Route path="/auth/callback" element={<AuthCallbackPage />} /><Route path="/request" element={<Location />} /></Routes></MemoryRouter></StrictMode>);
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/request"));
    expect(managerInstance.signinCallback).toHaveBeenCalledTimes(1);

    cleanup();
    managerInstance.signinCallback.mockRejectedValueOnce(new Error("No matching state"));
    render(<MemoryRouter initialEntries={["/auth/callback?code=bad&state=bad"]}><Routes><Route path="/auth/callback" element={<AuthCallbackPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole("alert")).toHaveTextContent("No matching state");
    expect(screen.getByRole("link", { name: "다시 로그인" })).toHaveAttribute("href", "/login");
  });

  it("stores the safe return path in the OIDC login request", async () => {
    const { LoginPage, AuthProvider } = await loadAuth();
    render(<MemoryRouter initialEntries={["/login?returnTo=%2Fworkflows%2F9"]}><AuthProvider><LoginPage /></AuthProvider></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "VORA 계정으로 로그인" }));
    await waitFor(() => expect(managerInstance.signinRedirect).toHaveBeenCalledWith({ extraQueryParams: { identity_provider: "COGNITO" }, state: { returnTo: "/workflows/9" } }));
  });

  it("uses only the scopes allowed by the Cognito app client by default", async () => {
    await loadAuth();
    expect(managerInstance.settings.scope).toBe("openid email profile");
    expect(managerInstance.settings.response_type).toBe("code");
    expect(managerInstance.settings.scope).not.toContain("offline_access");
  });

  it("starts Kakao through Cognito with the Kakao identity provider", async () => {
    const { LoginPage, AuthProvider } = await loadAuth();
    render(<MemoryRouter initialEntries={["/login?returnTo=%2Frequest"]}><AuthProvider><LoginPage /></AuthProvider></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "카카오 로그인" }));
    await waitFor(() => expect(managerInstance.signinRedirect).toHaveBeenCalledWith({ extraQueryParams: { identity_provider: "KakaoOIDC" }, state: { returnTo: "/request" } }));
  });

  it("uses Kakao as the default external provider name", async () => {
    const { LoginPage, AuthProvider } = await loadAuth(false);
    render(<MemoryRouter initialEntries={["/login"]}><AuthProvider><LoginPage /></AuthProvider></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "카카오 로그인" }));
    await waitFor(() => expect(managerInstance.signinRedirect).toHaveBeenCalledWith({ extraQueryParams: { identity_provider: "Kakao" }, state: { returnTo: "/request" } }));
  });

  it("rejects an external return path and prevents duplicate login clicks", async () => {
    const { LoginPage, AuthProvider } = await loadAuth();
    let finish!: () => void;
    managerInstance.signinRedirect.mockReturnValueOnce(new Promise<void>((resolve) => { finish = resolve; }));
    render(<MemoryRouter initialEntries={["/login?returnTo=https%3A%2F%2Fevil.test"]}><AuthProvider><LoginPage /></AuthProvider></MemoryRouter>);
    const kakao = screen.getByRole("button", { name: "카카오 로그인" });
    fireEvent.click(kakao);
    fireEvent.click(kakao);
    expect(managerInstance.signinRedirect).toHaveBeenCalledTimes(1);
    expect(managerInstance.signinRedirect).toHaveBeenCalledWith({ extraQueryParams: { identity_provider: "KakaoOIDC" }, state: { returnTo: "/request" } });
    expect(screen.getByRole("button", { name: "VORA 계정으로 로그인" })).toBeDisabled();
    finish();
  });

  it("clears the local session on the auth-expired signal and uses Cognito logout", async () => {
    const { AuthProvider, LoginPage, useAuth } = await loadAuth();
    function Controls() { const auth = useAuth(); return <><button onClick={() => void auth.logout()}>logout</button><button onClick={() => window.dispatchEvent(new Event("vora:auth-expired"))}>expire</button><LoginPage /></>; }
    render(<MemoryRouter><AuthProvider><Controls /></AuthProvider></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "expire" }));
    await waitFor(() => expect(managerInstance.removeUser).toHaveBeenCalled());
    managerInstance.removeUser.mockClear();
    managerInstance.signoutRedirect.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "logout" }));
    await waitFor(() => expect(managerInstance.removeUser).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(managerInstance.signoutRedirect).toHaveBeenCalled());
    expect(managerInstance.removeUser.mock.invocationCallOrder[0]).toBeLessThan(managerInstance.signoutRedirect.mock.invocationCallOrder[0]);
  });
});
