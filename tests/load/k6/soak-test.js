import { userJourney } from "./scenario.js";

// Soak/endurance test: moderate constant load (150 VUs, comfortably below
// the 250-user degradation estimate) held for 2-4 hours. Short tests can't
// catch slow leaks - unclosed connections, unbounded cache growth, the
// never-expiring cart collection accumulating rows, a slow memory leak in
// a background job. Duration is set via K6_SOAK_HOURS (default 3).
//
// Run with: k6 run --env K6_SOAK_HOURS=4 soak-test.js

const hours = parseFloat(__ENV.K6_SOAK_HOURS || "3");

export const options = {
  stages: [
    { duration: "5m", target: 150 },
    { duration: `${hours}h`, target: 150 },
    { duration: "5m", target: 0 },
  ],
  thresholds: {
    // Loose thresholds - the point of a soak test is watching *trend*
    // over time (is p95 creeping up hour-over-hour?), not a single
    // pass/fail number. Check the time-series in the summary/dashboard,
    // not just whether this threshold passed.
    http_req_failed: ["rate<0.05"],
  },
};

export default function () {
  userJourney();
}
