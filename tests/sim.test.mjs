import assert from 'node:assert/strict';
import { test } from 'node:test';

import { Player, SPEEDS } from '../pnf_sim/static/player.js';

// A fake clock: timers fire only when advance() passes them.
function fakeClock() {
  let t = 0;
  let timers = [];
  let nextId = 1;
  return {
    now: () => t,
    setTimer: (fn, ms) => {
      const id = nextId++;
      timers.push({ id, at: t + ms, fn });
      return id;
    },
    clearTimer: (id) => {
      timers = timers.filter((x) => x.id !== id);
    },
    advance(ms) {
      const until = t + ms;
      for (;;) {
        timers.sort((a, b) => a.at - b.at);
        const next = timers[0];
        if (!next || next.at > until) break;
        timers.shift();
        t = next.at;
        next.fn();
      }
      t = until;
    },
  };
}

function makePlayer(count) {
  const clock = fakeClock();
  const steps = [];
  const player = new Player(count, { onStep: (i) => steps.push(i), ...clock });
  return { player, clock, steps };
}

test('offers 1X, 10X, 50X, 100X and 200X', () => {
  assert.deepEqual(SPEEDS, [1, 10, 50, 100, 200]);
});

test('1X reveals one market minute per real minute', () => {
  const { player, clock, steps } = makePlayer(10);
  player.play();
  clock.advance(59_999);
  assert.deepEqual(steps, []);
  clock.advance(1);
  assert.deepEqual(steps, [1]);
  clock.advance(120_000);
  assert.deepEqual(steps, [1, 2, 3]);
});

test('200X reveals a minute every 0.3 seconds (a session in about 2 minutes)', () => {
  const { player, clock, steps } = makePlayer(400);
  player.setSpeed(200);
  player.play();
  clock.advance(3_000);
  assert.equal(steps.length, 10);
  clock.advance(375 * 300 - 3_000);
  assert.equal(steps.length, 375);
});

test('changing speed keeps the progress towards the next minute', () => {
  const { player, clock, steps } = makePlayer(10);
  player.play();
  clock.advance(30_000); // half way at 1X
  player.setSpeed(10); // remaining half takes 3 s at 10X
  clock.advance(2_999);
  assert.deepEqual(steps, []);
  clock.advance(1);
  assert.deepEqual(steps, [1]);
});

test('pause holds the position and resume continues from it', () => {
  const { player, clock, steps } = makePlayer(10);
  player.setSpeed(10); // 6 s per minute
  player.play();
  clock.advance(4_000);
  player.pause();
  clock.advance(600_000); // nothing happens while paused
  assert.deepEqual(steps, []);
  assert.ok(Math.abs(player.progress - 2 / 3) < 1e-9);
  player.play();
  clock.advance(2_000); // the remaining third
  assert.deepEqual(steps, [1]);
});

test('stops at the last minute', () => {
  const { player, clock, steps } = makePlayer(3);
  player.setSpeed(10);
  player.play();
  clock.advance(60_000);
  assert.deepEqual(steps, [1, 2]);
  assert.equal(player.playing, false);
  player.play(); // nothing left to play
  assert.equal(player.playing, false);
});

test('seek jumps without stepping through the minutes in between', () => {
  const { player, clock, steps } = makePlayer(100);
  player.seek(50);
  assert.equal(player.index, 50);
  player.setSpeed(10);
  player.play();
  clock.advance(6_000);
  assert.deepEqual(steps, [51]);
  player.seek(500); // clamped
  assert.equal(player.index, 99);
  assert.equal(player.playing, false);
});
