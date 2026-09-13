import { boundary } from "@shopify/shopify-app-react-router/server";
import type { ActionFunctionArgs, HeadersFunction, LoaderFunctionArgs } from "react-router";
import { Form, useActionData, useLoaderData } from "react-router";

import {
  getMerchantConfiguration,
  InvalidConfigurationError,
  saveMerchantConfiguration,
  StaleConfigurationError,
} from "../merchant-config.server";
import { profilesForBrowser, type ProviderRole } from "../provider-profiles.server";
import { authenticate } from "../shopify.server";

export const loader = async ({ request }: LoaderFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const configuration = await getMerchantConfiguration(session.shop);
  return { configuration, profiles: profilesForBrowser() };
};

export const action = async ({ request }: ActionFunctionArgs) => {
  const { session } = await authenticate.admin(request);
  const form = await request.formData();
  const selection: Record<ProviderRole, string> = {
    asr: String(form.get("asr") ?? ""),
    llm: String(form.get("llm") ?? ""),
    tts: String(form.get("tts") ?? ""),
  };
  try {
    const configuration = await saveMerchantConfiguration({
      shop: session.shop,
      expectedRevision: String(form.get("revision") ?? ""),
      selection,
    });
    return { ok: true, configuration, errors: null, message: "Provider settings saved" };
  } catch (error) {
    if (error instanceof InvalidConfigurationError) {
      return { ok: false, configuration: null, errors: error.errors, message: error.message };
    }
    if (error instanceof StaleConfigurationError) {
      return {
        ok: false,
        configuration: null,
        errors: null,
        message: "Settings changed in another session. Reload before saving again.",
      };
    }
    throw error;
  }
};

export default function Index() {
  const { configuration, profiles } = useLoaderData<typeof loader>();
  const result = useActionData<typeof action>();
  const value = result?.ok && result.configuration ? result.configuration : configuration;
  const fields: Array<{ role: ProviderRole; label: string; selected: string }> = [
    { role: "asr", label: "Speech recognition", selected: value.asrProfileId },
    { role: "llm", label: "Reasoning model", selected: value.llmProfileId },
    { role: "tts", label: "Drake voice", selected: value.ttsProfileId },
  ];

  return (
    <s-page heading="iShop">
      <s-section heading="Drake provider settings">
        <s-paragraph>
          Choose from configurations validated by iShop. Changes apply to new shopper sessions.
        </s-paragraph>
        {result?.message ? <p role="status">{result.message}</p> : null}
        <Form method="post">
          <input type="hidden" name="revision" value={value.revision} />
          {fields.map((field) => (
            <p key={field.role}>
              <label>
                {field.label}
                <br />
                <select name={field.role} defaultValue={field.selected}>
                  {profiles
                    .filter((profile) => profile.role === field.role)
                    .map((profile) => (
                      <option key={profile.id} value={profile.id} disabled={!profile.enabled}>
                        {profile.label} — {profile.mode}
                        {profile.enabled ? "" : ` — unavailable: ${profile.disabledReason}`}
                      </option>
                    ))}
                </select>
              </label>
              {result?.errors?.[field.role] ? (
                <span role="alert"> {result.errors[field.role]}</span>
              ) : null}
            </p>
          ))}
          <button type="submit">Save provider settings</button>
        </Form>
      </s-section>
      <s-section heading="Payment boundary">
        <s-paragraph>
          Drake can prepare the cart and open checkout. The shopper completes payment in Shopify.
        </s-paragraph>
      </s-section>
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) => boundary.headers(headersArgs);
