// Gravity Golf — entry point. Buildless ES module: every import below is a
// relative path the browser resolves on its own, so this tree needs no bundler.
import { LEVELS } from './levels.js';
import { draw } from './render.js';
import { readAim } from './input.js';

// Tuned launch physics (SPEC-28 fixture: the "tune the launch physics" slice).
export const GRAVITY = 0.42;
export const LAUNCH_SCALE = 0.11;
export const DAMPING = 0.995;

export function makeState(levelIndex) {
  const level = LEVELS[levelIndex % LEVELS.length];
  return {
    level: level,
    ball: { x: level.tee.x, y: level.tee.y, vx: 0, vy: 0 },
    aim: readAim(0),
    strokes: 0,
  };
}

export function step(state) {
  const ball = state.ball;
  ball.vy = (ball.vy + GRAVITY) * DAMPING;
  ball.vx = ball.vx * DAMPING;
  ball.x = ball.x + ball.vx;
  ball.y = ball.y + ball.vy;
  if (ball.y > state.level.floor) {
    ball.y = state.level.floor;
    ball.vy = -ball.vy * 0.5;
  }
  return state;
}

export function start() {
  const canvas = document.getElementById('stage');
  if (!canvas) {
    return null;
  }
  const ctx = canvas.getContext('2d');
  const state = makeState(0);
  function frame() {
    step(state);
    draw(ctx, state, canvas.width, canvas.height);
    window.requestAnimationFrame(frame);
  }
  window.requestAnimationFrame(frame);
  return state;
}

start();
