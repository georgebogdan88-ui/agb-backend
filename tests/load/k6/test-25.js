import { userJourney } from "./scenario.js";

// Baseline check - 25 concurrent users. Per the scalability report, this
// tier should show no issues at all; this run mainly confirms the test
// setup itself (staging env, synthetic data, script) works correctly
// before moving to heavier tiers.
export const options = {
  stages: [
    { duration: "1m", target: 25 }, // ramp up
    { duration: "5m", target: 25 }, // sustained
    { duration: "30s", target: 0 }, // ramp down
  ],
  thresholds: {
    http_req_duration: ["p(95)<1500", "p(99)<3000"],
    http_req_failed: ["rate<0.02"],
    login_success_rate: ["rate>0.95"],
    cart_add_success_rate: ["rate>0.95"],
    order_success_rate: ["rate>0.90"],
  },
};

export default function () {
  userJourney();
}
