// The level set (foundation slice: one playable hole).
export const LEVELS = [
  { name: 'Orbit One', par: 3, tee: { x: 60, y: 60 }, cup: { x: 400, y: 250 }, floor: 300 },
];

export function parTotal() {
  return LEVELS.reduce(function (sum, level) { return sum + level.par; }, 0);
}
