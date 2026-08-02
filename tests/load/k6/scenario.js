// Shared realistic user journey, imported by every scenario file (test-25.js,
// test-100.js, spike-test.js, soak-test.js, etc.) so the flow itself is
// defined once. Each scenario file only differs in its `options` (VU
// ramp/duration).
//
// Mirrors a real visitor: browsing hits the webshop's own pages (which
// server-render via a call to the backend, same as a real request would),
// while login/cart/order are the client-side JS calls a browser's own
// fetch() would make straight to the backend - not two artificially
// separate hits per action.

import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";
import {
  WEBSHOP_URL,
  BACKEND_URL,
  TEST_USER_EMAIL_PREFIX,
  TEST_USER_PASSWORD,
  TEST_USER_COUNT,
  SEARCH_TERMS,
  COLLECTIONS,
} from "./config.js";

// ---- Custom metrics (beyond k6's built-in http_req_duration/http_req_failed) ----
export const loginSuccessRate = new Rate("login_success_rate");
export const orderSuccessRate = new Rate("order_success_rate");
export const ordersCreated = new Counter("orders_created_total");
export const cartAddSuccessRate = new Rate("cart_add_success_rate");
export const imageLoadDuration = new Trend("image_load_duration", true);
export const searchDuration = new Trend("search_page_duration", true);
export const productPageDuration = new Trend("product_page_duration", true);

function randomItem(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

// Pulls the first /produse/{id} link out of a listing page's HTML - good
// enough for picking "a plausible product to click on next" without a full
// DOM parser (k6 has none built in; this is standard practice for k6
// scripts against server-rendered HTML).
function extractFirstProductId(html) {
  const match = html.match(/\/produse\/([a-zA-Z0-9._-]+)"/);
  return match ? match[1] : null;
}

function extractImageUrl(html) {
  // Generic (not Cloudinary-specific) - scripts/seed_staging_data.py uses
  // Lorem Picsum placeholder images for staging, so production's
  // Cloudinary and a freshly-seeded staging DB both match this.
  const match = html.match(/https:\/\/[^\s"'<>]+\.(?:png|jpg|jpeg|webp)/i);
  return match ? match[0] : null;
}

export function userJourney() {
  const sessionId = `k6-${__VU}-${__ITER}-${Date.now()}`;

  // 1. Open the homepage.
  let res = http.get(`${WEBSHOP_URL}/`, { tags: { step: "homepage" } });
  check(res, { "homepage: 200": (r) => r.status === 200 });
  sleep(randBetween(2, 5));

  // 2. Search for a part.
  const term = randomItem(SEARCH_TERMS);
  res = http.get(`${WEBSHOP_URL}/produse?q=${encodeURIComponent(term)}`, {
    tags: { step: "search" },
  });
  check(res, { "search: 200": (r) => r.status === 200 });
  searchDuration.add(res.timings.duration);
  sleep(randBetween(1, 3));

  // 3. Filter by collection (a separate action, not chained off search -
  // most real users do one or the other per visit, but exercising both
  // keeps this scenario representative of the full filter surface).
  const collection = randomItem(COLLECTIONS);
  res = http.get(`${WEBSHOP_URL}/produse?collection=${encodeURIComponent(collection)}`, {
    tags: { step: "filter" },
  });
  check(res, { "filter: 200": (r) => r.status === 200 });
  const productId = extractFirstProductId(res.body) || extractFirstProductId(
    http.get(`${WEBSHOP_URL}/produse?q=${encodeURIComponent(term)}`).body
  );
  sleep(randBetween(1, 3));

  // 4. Open a product page (+ its image).
  if (productId) {
    res = http.get(`${WEBSHOP_URL}/produse/${productId}`, { tags: { step: "product_page" } });
    check(res, { "product page: 200": (r) => r.status === 200 });
    productPageDuration.add(res.timings.duration);

    const imageUrl = extractImageUrl(res.body);
    if (imageUrl) {
      const imgRes = http.get(imageUrl, { tags: { step: "product_image" } });
      check(imgRes, { "image: 200": (r) => r.status === 200 });
      imageLoadDuration.add(imgRes.timings.duration);
    }
  }
  sleep(randBetween(3, 8));

  // 5. Login (~30% of visits - matches the report's assumed mix of
  // browsing-only vs. returning/authenticated visitors).
  let authToken = null;
  if (Math.random() < 0.3) {
    const userNum = Math.floor(Math.random() * TEST_USER_COUNT);
    res = http.post(
      `${BACKEND_URL}/auth/login`,
      JSON.stringify({
        email: `${TEST_USER_EMAIL_PREFIX}${userNum}@loadtest.invalid`,
        password: TEST_USER_PASSWORD,
      }),
      { headers: { "Content-Type": "application/json" }, tags: { step: "login" } }
    );
    const ok = res.status === 200;
    loginSuccessRate.add(ok);
    if (ok) authToken = JSON.parse(res.body).token;
    sleep(randBetween(1, 2));
  }

  // 6. Add to cart, if we found a product to add.
  if (productId) {
    res = http.post(
      `${BACKEND_URL}/cart`,
      JSON.stringify({
        session_id: sessionId,
        product_id: productId,
        product_name: "Load test product",
        product_image: "",
        price: 1, // intentionally wrong - since the CRITIC-3 fix, the
        // backend must reprice this from its own catalog; a 200 here with
        // the correct (non-1) price back would itself be a regression
        // check, not just a smoke test.
        quantity: 1,
      }),
      { headers: { "Content-Type": "application/json" }, tags: { step: "add_to_cart" } }
    );
    const ok = res.status === 200;
    cartAddSuccessRate.add(ok);
    sleep(randBetween(1, 2));

    // 7. View the cart.
    res = http.get(`${BACKEND_URL}/cart/${sessionId}`, { tags: { step: "view_cart" } });
    check(res, { "cart: 200": (r) => r.status === 200 });
    sleep(randBetween(2, 4));

    // 8. Create an order (~10% of visits that made it this far - matches
    // the report's assumed browse-to-purchase ratio).
    if (Math.random() < 0.1) {
      const cart = JSON.parse(res.body);
      const items = cart.map((item) => ({
        product_id: item.product_id,
        product_name: item.product_name,
        product_image: item.product_image,
        price: item.price,
        quantity: item.quantity,
      }));
      if (items.length > 0) {
        const subtotal = items.reduce((sum, it) => sum + it.price * it.quantity, 0);
        res = http.post(
          `${BACKEND_URL}/orders`,
          JSON.stringify({
            session_id: sessionId,
            items,
            customer: {
              name: `Load Test User ${__VU}`,
              email: `${TEST_USER_EMAIL_PREFIX}${__VU}@loadtest.invalid`,
              phone: "0700000000",
              address: "Str. Test nr. 1",
              city: "Test City",
              county: "Test County",
              postal_code: "000000",
              notes: "SYNTHETIC LOAD TEST ORDER - safe to delete",
            },
            subtotal,
            shipping: 25,
            total: subtotal + 25,
            payment_method: "ramburs",
          }),
          { headers: { "Content-Type": "application/json" }, tags: { step: "create_order" } }
        );
        const ok = res.status === 200;
        orderSuccessRate.add(ok);
        if (ok) ordersCreated.add(1);
      }
    }
  }
}

function randBetween(min, max) {
  return Math.random() * (max - min) + min;
}
