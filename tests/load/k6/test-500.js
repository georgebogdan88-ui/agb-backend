import { userJourney } from "./scenario.js";

// 500 concurrent users - report's estimate for sustained/clear degradation
// and the approximate ceiling of Atlas M0's connection budget shared with
// agb-crm. Watch Atlas connection-count metrics during this run in
// particular, not just k6's own output.
export const options = {
  stages: [
    { duration: "5m", target: 500 },
    { duration: "15m", target: 500 },
    { duration: "2m", target: 0 },
  ],
  thresholds: {
    http_req_duration: ["p(95)<5000"],
    http_req_failed: ["rate<0.10"],
  },
};

export default function () {
  userJourney();
}
