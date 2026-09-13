import type { LoaderFunctionArgs } from "react-router";

import { createResumeReference, createSessionGrant, verifyResumeReference } from "../session-grant.server";
import { getConfigurationSnapshot } from "../merchant-config.server";
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
  const configuration = await getConfigurationSnapshot(session.shop);
  const resume = new URL(request.url).searchParams.get("resume");
  const resumed = resume ? verifyResumeReference(resume, signingSecret) : null;
  const canResume = resumed && resumed.shop_id === session.shop &&
    resumed.permitted_origin === requestedOrigin && resumed.config_revision === configuration.revision;
  const grant = createSessionGrant({
    shop: session.shop,
    origin: requestedOrigin,
    signingSecret,
    configRevision: configuration.revision,
    asrProfileId: configuration.selection.asr,
    llmProfileId: configuration.selection.llm,
    ttsProfileId: configuration.selection.tts,
    anonymousSessionId: canResume ? resumed.session_id : undefined,
  });

  return Response.json({ grant, resume_reference: createResumeReference(grant, signingSecret) }, { headers: { "Cache-Control": "no-store" } });
}
