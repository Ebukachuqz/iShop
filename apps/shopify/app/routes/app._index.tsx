import type { HeadersFunction, LoaderFunctionArgs } from "react-router";
import { boundary } from "@shopify/shopify-app-react-router/server";

import { authenticate } from "../shopify.server";

export const loader = async ({ request }: LoaderFunctionArgs) => {
  await authenticate.admin(request);
  return null;
};

export default function Index() {
  return (
    <s-page heading="iShop">
      <s-section heading="Drake storefront assistant">
        <s-paragraph>
          Install and enable the Drake app embed in the active storefront theme
          to begin the controlled-store capability test.
        </s-paragraph>
      </s-section>
      <s-section heading="Payment boundary">
        <s-paragraph>
          Drake can prepare the shopper&apos;s cart and open checkout. The shopper
          always completes payment directly in Shopify checkout.
        </s-paragraph>
      </s-section>
    </s-page>
  );
}

export const headers: HeadersFunction = (headersArgs) =>
  boundary.headers(headersArgs);
