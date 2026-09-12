import type { LoaderFunctionArgs } from "react-router";

import { createSessionGrant } from "../session-grant.server";
import { authenticate } from "../shopify.server";

function configuredDevelopmentOrigins() {
  return new Set(
    (process.env.DEV_ALLOWED_ORIGINS ?? "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean),
  );
}

export async function loader({ request }: LoaderFunctionArgs) {
  const { session } = await authenticate.public.appProxy(request);
  if (!session) {
    return Response.json({ error: "App installation is not active" }, { status: 401 });
  }

  const requestedOrigin = new URL(request.url).searchParams.get("origin") ?? "";
  const allowedOrigins = configuredDevelopmentOrigins();
  if (!requestedOrigin || requestedOrigin === "*" || !allowedOrigins.has(requestedOrigin)) {
    return Response.json({ error: "Storefront origin is not allowed" }, { status: 403 });
  }

  const signingSecret = process.env.SESSION_SIGNING_SECRET ?? "";
  const grant = createSessionGrant({
    shop: session.shop,
    origin: requestedOrigin,
    signingSecret,
  });

  return Response.json({ grant }, { headers: { "Cache-Control": "no-store" } });
}
