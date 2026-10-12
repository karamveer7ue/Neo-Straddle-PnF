// Replay clock for the simulator: reveals one market minute at a time.
//
// At speed N, one market minute plays every 60 / N seconds (1X is real
// time). No DOM here, so it is unit-tested in Node (tests/sim.test.mjs).
//
// The clock keeps an absolute deadline for the next minute and catches up on
// every minute owed when a timer fires late, so a throttled background tab
// does not slow the replay down.

export const SPEEDS = [1, 10, 50, 100, 200];
const MINUTE_MS = 60_000;

export class Player {
  /**
   * @param {number} count number of market minutes in the run
   * @param {{onStep: (i: number) => void, onState?: () => void, start?: number,
   *          now?: () => number, setTimer?: Function, clearTimer?: Function}} hooks
   *   onStep(i) reveals minute i (called once per minute, in order, while playing);
   *   start is the minute already revealed at the beginning (-1: none yet);
   *   now/setTimer/clearTimer are injectable for tests.
   */
  constructor(count, { onStep, onState = () => {}, start = 0, now, setTimer, clearTimer }) {
    this.count = count;
    this.onStep = onStep;
    this.onState = onState;
    this.now = now ?? (() => performance.now());
    this.setTimer = setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = clearTimer ?? ((id) => clearTimeout(id));
    this.index = start; // the last revealed minute (-1: none yet)
    this.speed = 1;
    this.playing = false;
    this.timer = null;
    this.nextAt = 0; // while playing: when the next minute completes
    this.waitDone = 0; // while paused: fraction of the wait for the next minute already served
  }

  get interval() {
    return MINUTE_MS / this.speed;
  }

  get atEnd() {
    return this.index >= this.count - 1;
  }

  /** Fraction (0..1) of the wait for the next minute that has passed. */
  get progress() {
    if (!this.playing) return this.waitDone;
    return Math.min(1, Math.max(0, 1 - (this.nextAt - this.now()) / this.interval));
  }

  play() {
    if (this.playing || this.atEnd) return;
    this.playing = true;
    this.nextAt = this.now() + (1 - this.waitDone) * this.interval;
    this.schedule();
    this.onState();
  }

  pause() {
    if (!this.playing) return;
    this.waitDone = this.progress;
    this.playing = false;
    this.clearTimer(this.timer);
    this.onState();
  }

  setSpeed(speed) {
    if (!SPEEDS.includes(speed) || speed === this.speed) return;
    const done = this.progress; // keep progress towards the next minute
    this.speed = speed;
    if (this.playing) {
      this.nextAt = this.now() + (1 - done) * this.interval;
      this.schedule();
    } else {
      this.waitDone = done;
    }
    this.onState();
  }

  /** Jump to minute i (-1: before the first) without playing the minutes in between. */
  seek(i) {
    this.index = Math.max(-1, Math.min(this.count - 1, i));
    this.waitDone = 0;
    if (this.playing) {
      if (this.atEnd) {
        this.playing = false;
        this.clearTimer(this.timer);
      } else {
        this.nextAt = this.now() + this.interval;
        this.schedule();
      }
    }
    this.onState();
  }

  schedule() {
    this.clearTimer(this.timer);
    this.timer = this.setTimer(() => this.tick(), Math.max(0, this.nextAt - this.now()));
  }

  tick() {
    const t = this.now();
    // Catch up on every minute owed: timers fire late in throttled background tabs.
    while (this.playing && !this.atEnd && t >= this.nextAt) {
      this.index += 1;
      this.nextAt += this.interval;
      this.onStep(this.index);
    }
    if (!this.playing) return; // paused or reset from inside onStep
    if (this.atEnd) {
      this.playing = false;
      this.waitDone = 0;
      this.onState();
    } else {
      this.schedule();
    }
  }
}
