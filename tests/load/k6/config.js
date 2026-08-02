// Shared config + safety guard. Every test script imports BASE_URL from
// here instead of hardcoding it, so this one file is the single place
// that decides where traffic goes.
//
// SAFETY: refuses to run unless BASE_URL is explicitly passed AND doesn't
// match a known production hostname. This is a load test - it must only
// ever point at a staging environment with synthetic data, never at
// agb-backend.onrender.com or any other real production host.

const PRODUCTION_HOSTS = [
  "agb-backend.onrender.com",
  "agb-webshop.onrender.com",
  "agb-crm-api.onrender.com",
  "agb-agroparts.ro",
];

// Two origins, matching the real architecture: browsing pages are
// server-rendered by the webshop (which itself calls the backend
// internally, so that hop is captured indirectly); logins/cart/orders are
// client-side JS calls straight to the backend API, same as a real
// browser would make - so this test replicates both halves realistically.
export const WEBSHOP_URL = __ENV.WEBSHOP_URL;
export const BACKEND_URL = __ENV.BACKEND_URL;

if (!WEBSHOP_URL || !BACKEND_URL) {
  throw new Error(
    "WEBSHOP_URL and BACKEND_URL env vars are both required, e.g.:\n" +
      "  --env WEBSHOP_URL=https://agb-webshop-staging.onrender.com \\\n" +
      "  --env BACKEND_URL=https://agb-backend-staging.onrender.com/api\n" +
      "Refusing to default to anything, to avoid accidentally hitting production."
  );
}

for (const url of [WEBSHOP_URL, BACKEND_URL]) {
  if (PRODUCTION_HOSTS.some((host) => url.includes(host))) {
    throw new Error(
      `${url} looks like a production host. These tests must only run against a ` +
        "separate staging environment with synthetic data. Refusing to continue."
    );
  }
}

// Test-account credentials and product IDs must come from the synthetic
// dataset seeded into staging - never real customer accounts or real
// product IDs copied from production.
export const TEST_USER_EMAIL_PREFIX = __ENV.TEST_USER_EMAIL_PREFIX || "loadtest-user-";
export const TEST_USER_PASSWORD = __ENV.TEST_USER_PASSWORD || "LoadTest123!";
export const TEST_USER_COUNT = parseInt(__ENV.TEST_USER_COUNT || "1000", 10);

// A handful of representative synthetic product IDs to search/browse/order
// - populate via --env SEARCH_TERMS=... / PRODUCT_IDS=... or edit the
// fallback list below once the synthetic catalog is seeded.
export const SEARCH_TERMS = (__ENV.SEARCH_TERMS || "filtru,pompa,DZ100001,rulment,cuzineti").split(",");
export const COLLECTIONS = (__ENV.COLLECTIONS || "Piese noi,Piese din dezmembrare").split(",");
export const PRODUCT_IDS = (__ENV.PRODUCT_IDS || "").split(",").filter(Boolean);
