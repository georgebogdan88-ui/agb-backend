import { userJourney } from "./scenario.js";

// 250 concurrent users - the report's estimated point of first VISIBLE
// degradation (pre-fix: bcrypt event-loop blocking + unindexed token
// lookups + regex product search). This run is the key one to compare
// against the report's prediction now that the CPU/index fixes are live.
export const options = {
  stages: [
    { duration: "5m", target: 250 },
    { duration: "10m", target: 250 },
    { duration: "2m", target: 0 },
  ],
  thresholds: {
    http_req_duration: ["p(95)<3000", "p(99)<6000"],
    http_req_failed: ["rate<0.05"],
    login_success_rate: ["rate>0.90"],
    cart_add_success_rate: ["rate>0.90"],
    order_success_rate: ["rate>0.85"],
  },
};

export default function () {
  userJourney();
}
