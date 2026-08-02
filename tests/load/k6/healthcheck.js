import http from "k6/http";
import { check, sleep } from "k6";
import { WEBSHOP_URL, BACKEND_URL } from "./config.js";

// Lightweight continuous availability probe - one VU, hits both services
// every 2s. Meant to run ALONGSIDE another load test (or alongside a
// manual "stop the service for a few minutes" recovery drill in staging),
// in a second terminal, so you get a clean timeline of exactly when each
// component went down and when it came back - separate from the main
// load test's own noisier failure timeline.
//
// Run with: k6 run --duration 30m healthcheck.js
// (pick a duration that comfortably covers whatever you're testing
// alongside it)

export const options = {
  vus: 1,
  duration: __ENV.DURATION || "30m",
};

export default function () {
  const backendRes = http.get(`${BACKEND_URL}/products?limit=1`, {
    tags: { component: "backend" },
    timeout: "5s",
  });
  check(backendRes, { "backend up": (r) => r.status === 200 });

  const webshopRes = http.get(`${WEBSHOP_URL}/`, {
    tags: { component: "webshop" },
    timeout: "10s",
  });
  check(webshopRes, { "webshop up": (r) => r.status === 200 });

  sleep(2);
}
