import { userJourney } from "./scenario.js";

// 1000 concurrent users - the ceiling test. Per the report, current
// infrastructure (Render Standard - 2GB RAM/1 CPU, confirmed, for both
// agb-backend and agb-webshop - + Atlas M0) likely can't sustain this
// cleanly; the goal here isn't a passing threshold, it's finding exactly
// where and how it breaks (connection exhaustion vs. CPU saturation vs.
// timeouts vs. something else) to confirm/correct the report's ranking of
// which resource is the real ceiling. With only 1 CPU per instance, CPU
// saturation is a strong candidate for what breaks first here.
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
