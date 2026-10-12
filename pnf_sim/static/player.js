// Replay clock for the simulator: reveals one market minute at a time.
//
// At speed N, one market minute plays every 60 / N seconds (1X is real
// time). No DOM here, so it is unit-tested in Node (tests/sim.test.mjs).

export const SPEEDS = [1, 10, 50, 100, 200];
const MINUTE_MS = 60_000;

export class Player {
  /**
   * @param {number} count number of market minutes in the run
   * @param {{onStep: (i: number) => void, onState?: () => void,
   *          now?: () => number, setTimer?: Function, clearTimer?: Function}} hooks
   *   onStep(i) reveals minute i (called once per minute, in order, while playing);
   *   now/setTimer/clearTimer are injectable for tests.
   */
  constructor(count, { onStep, onState = () => {}, now, setTimer, clearTimer }) {
    this.count = count;
    this.onStep = onStep;
    this.onState = onState;
    this.now = now ?? (() => performance.now());
    this.setTimer = setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = clearTimer ?? ((id) => clearTimeout(id));
    this.index = 0; // the last revealed minute
    this.speed = 1;
    this.playing = false;
    this.timer = null;
    this.waitStart = 0; // when the wait for the next minute began
    this.waitDone = 0; // fraction of that wait already served (kept across pause and speed changes)
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
    return Math.min(1, this.waitDone + (this.now() - this.waitStart) / this.interval);
  }

  play() {
    if (this.playing || this.atEnd) return;
    this.playing = true;
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
      this.clearTimer(this.timer);
      this.waitDone = done;
      this.schedule();
    }
    this.onState();
  }

  /** Jump to minute i without playing the minutes in between. */
  seek(i) {
    this.index = Math.max(0, Math.min(this.count - 1, i));
    this.waitDone = 0;
    if (this.playing) {
      this.clearTimer(this.timer);
      if (this.atEnd) this.playing = false;
      else this.schedule();
    }
    this.onState();
  }

  schedule() {
    this.waitStart = this.now();
    this.timer = this.setTimer(() => this.tick(), Math.max(0, (1 - this.waitDone) * this.interval));
  }

  tick() {
    this.waitDone = 0;
    this.index += 1;
    this.onStep(this.index);
    if (this.atEnd) {
      this.playing = false;
      this.onState();
    } else {
      this.schedule();
    }
  }
}
