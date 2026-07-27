// The level set (first attempt at the expanded set). The third hole is missing
// its `par`, which `parTotal()` silently turns into NaN — the blocking review
// finding the SPEC-28 fixture's revise slice fixes.
export const LEVELS = [
  { name: 'Orbit One', par: 3, tee: { x: 60, y: 60 }, cup: { x: 400, y: 250 }, floor: 300 },
  { name: 'Slingshot', par: 4, tee: { x: 48, y: 90 }, cup: { x: 430, y: 220 }, floor: 292 },
  { name: 'Deep Well', tee: { x: 72, y: 40 }, cup: { x: 380, y: 270 }, floor: 304 },
];

export function parTotal() {
  return LEVELS.reduce(function (sum, level) { return sum + level.par; }, 0);
}
