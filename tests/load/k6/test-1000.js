import { userJourney } from "./scenario.js";

// 1000 concurrent users - the ceiling test. Per the report, current
// infrastructure (Render Pro + Atlas M0) likely can't sustain this
// cleanly; the goal here isn't a passing threshold, it's finding exactly
// where and how it breaks (connection exhaustion vs. CPU saturation vs.
// timeouts vs. something else) to confirm/correct the report's ranking of
// which resource is the real ceiling.
export const options = {
  stages: [
    { duration: "10m", target: 1000 },
    { duration: "15m", target: 1000 },
    { duration: "3m", target: 0 },
  ],
  // No hard thresholds that abort the run - this test is exploratory by
  // design. Read the actual numbers afterward rather than pass/fail.
};

export default function () {
  userJourney();
}
