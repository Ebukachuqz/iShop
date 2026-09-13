import { describe, expect, test, vi, beforeEach, afterEach } from "vitest";
import { loader } from "./routes/proxy.bootstrap";
import { signatureMatches } from "./session-grant.server";

vi.mock("./shopify.server", () => ({
  authenticate: {
    public: {
      appProxy: vi.fn(),
    },
  },
}));

vi.mock("./merchant-config.server", () => ({
  getConfigurationSnapshot: vi.fn(),
}));

import { authenticate } from "./shopify.server";
import { getConfigurationSnapshot } from "./merchant-config.server";

describe("proxy.bootstrap loader with mocked Shopify authentication", () => {
  const originalEnv = { ...process.env };
  const mockAppProxy = authenticate.public.appProxy as unknown as ReturnType<typeof vi.fn>;
  const mockConfiguration = getConfigurationSnapshot as unknown as ReturnType<typeof vi.fn>;

  beforeEach(() => {
    process.env.SESSION_SIGNING_SECRET = "test-signing-secret-with-at-least-32-characters";
    process.env.DEV_ALLOWED_ORIGINS = "https://example-shop.myshopify.com,https://shop.example.com";
    mockConfiguration.mockResolvedValue({
      revision: "config-test-revision",
      selection: {
        asr: "sahara-stream-pcm",
        llm: "groq-gpt-oss-120b",
        tts: "sahara-tts-female-pcm",
      },
    });
  });

  afterEach(() => {
    process.env = { ...originalEnv };
    vi.clearAllMocks();
  });

  test("returns 401 when Shopify app proxy session is missing or inactive", async () => {
    mockAppProxy.mockResolvedValueOnce({ session: null });
    const request = new Request("https://shop.example.com/proxy/bootstrap?origin=https://shop.example.com");
    const response = await loader({ request } as any);
    expect(response.status).toBe(401);
    const data = await response.json();
    expect(data.error).toBe("App installation is not active");
  });

  test("returns 403 when origin parameter is missing, wildcard, or not in allowlist", async () => {
    mockAppProxy.mockResolvedValue({ session: { shop: "example-shop.myshopify.com" } });

    // Missing origin
    const req1 = new Request("https://shop.example.com/proxy/bootstrap");
    const res1 = await loader({ request: req1 } as any);
    expect(res1.status).toBe(403);

    // Wildcard origin
    const req2 = new Request("https://shop.example.com/proxy/bootstrap?origin=*");
    const res2 = await loader({ request: req2 } as any);
    expect(res2.status).toBe(403);

    // Unapproved external origin
    const req3 = new Request("https://shop.example.com/proxy/bootstrap?origin=https://malicious-site.com");
    const res3 = await loader({ request: req3 } as any);
    expect(res3.status).toBe(403);
  });

  test("generates and returns valid signed session grant on approved origin", async () => {
    mockAppProxy.mockResolvedValueOnce({ session: { shop: "example-shop.myshopify.com" } });
    const request = new Request("https://shop.example.com/proxy/bootstrap?origin=https://example-shop.myshopify.com");
    const response = await loader({ request } as any);
    expect(response.status).toBe(200);
    const data = await response.json();
    expect(data.grant).toBeDefined();

    // Verify with the real grant implementation
    expect(data.grant.shop_id).toBe("example-shop.myshopify.com");
    expect(data.grant.permitted_origin).toBe("https://example-shop.myshopify.com");
    expect(data.grant.config_revision).toBe("config-test-revision");
    expect(data.grant.asr_profile_id).toBe("sahara-stream-pcm");
    expect(data.grant.llm_profile_id).toBe("groq-gpt-oss-120b");
    expect(data.grant.tts_profile_id).toBe("sahara-tts-female-pcm");
    expect(signatureMatches(data.grant, process.env.SESSION_SIGNING_SECRET!)).toBe(true);
  });
});
