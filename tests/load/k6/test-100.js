import { userJourney } from "./scenario.js";

// 100 concurrent users - per the report, should still be stable, but this
// is where the first signs of pressure (bcrypt-related stalls, before the
// scalability fixes; now testing whether those fixes actually held) might
// start to show under a concentrated login burst.
export const options = {
  stages: [
    { duration: "3m", target: 100 },
    { duration: "10m", target: 100 },
    { duration: "1m", target: 0 },
  ],
  thresholds: {
    http_req_duration: ["p(95)<2000", "p(99)<4000"],
    http_req_failed: ["rate<0.03"],
    login_success_rate: ["rate>0.95"],
    cart_add_success_rate: ["rate>0.95"],
    order_success_rate: ["rate>0.90"],
  },
};

export default function () {
  userJourney();
}
