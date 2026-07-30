// The level set. Every hole declares a par so the HUD can score the round.
export const LEVELS = [
  { name: 'Orbit One', par: 3, tee: { x: 60, y: 60 }, cup: { x: 400, y: 250 }, floor: 300 },
  { name: 'Slingshot', par: 4, tee: { x: 48, y: 90 }, cup: { x: 430, y: 220 }, floor: 292 },
  { name: 'Deep Well', par: 5, tee: { x: 72, y: 40 }, cup: { x: 380, y: 270 }, floor: 304 },
];

export function parTotal() {
  return LEVELS.reduce(function (sum, level) { return sum + level.par; }, 0);
}
