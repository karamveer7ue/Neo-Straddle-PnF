// Price-axis layout helpers (no DOM, so they are unit-tested in Node).

export const TAG_H = 18; // axis tag height; tags closer than this would overlap

/**
 * Lay out axis tags without overlaps. Tags are taken in priority order; each
 * keeps its own y if that is free, else takes the free slot nearest to it.
 * Free slots can only be the wanted y or a spot touching an already placed
 * tag, so those are the only candidates. Everything stays within [minY, maxY].
 */
export function placeTags(tags, minY, maxY) {
  const placed = [];
  const clamp = (y) => Math.max(minY, Math.min(maxY, y));
  const free = (y) => placed.every((p) => Math.abs(p.y - y) >= TAG_H);
  for (const tag of tags) {
    const want = clamp(tag.y);
    const candidates = [want, ...placed.flatMap((p) => [p.y - TAG_H, p.y + TAG_H])].map(clamp).filter(free);
    const y = candidates.length
      ? candidates.reduce((best, cand) => (Math.abs(cand - want) < Math.abs(best - want) ? cand : best))
      : want; // no room at all: overlap rather than vanish
    placed.push({ ...tag, y });
  }
  return placed;
}
