import { userJourney } from "./scenario.js";

// Sudden-spike test: 50 -> 500 in 20s, matching a real scenario (a
// promotion, a social-media mention, a newsletter blast). Tests recovery
// behavior under a sharp step, not just gradual ramping - a system can
// handle 500 fine when ramped over 5 minutes and still fall over when it
// arrives in 20 seconds, because connection pools/caches/autoscaling (if
// any) don't have time to react.
export const options = {
  stages: [
    { duration: "1m", target: 50 }, // baseline
    { duration: "20s", target: 500 }, // the spike
    { duration: "5m", target: 500 }, // hold at peak - does it recover or stay degraded?
    { duration: "2m", target: 50 }, // back to baseline - does it actually recover?
    { duration: "3m", target: 50 }, // confirm baseline performance is back to normal
  ],
  thresholds: {
    http_req_failed: ["rate<0.15"], // loose on purpose - the point is to observe, not gate
  },
};

export default function () {
  userJourney();
}
