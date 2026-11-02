// Popovers a narrow or scrolling sidebar can't cut off (TAS-79): pinPop fixes a menu to the window, under its button
// (or above it when there's more room there), inside the window with an 8px margin, scrolling when it's taller than
// the space it has, and following the button while the page scrolls or the window resizes.

export function pinPop(pop, anchor) {
  pop.classList.add("pinned");
  const place = () => {
    if (!pop.isConnected || pop.hidden) return;
    const r = anchor.getBoundingClientRect();
    pop.style.minWidth = `${Math.min(r.width, window.innerWidth - 16)}px`;
    const below = window.innerHeight - r.bottom - 12, above = r.top - 12;
    pop.style.maxHeight = `${Math.max(below, above, 160)}px`;
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    pop.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - pw - 8))}px`;
    pop.style.top = `${ph > below && above > below ? Math.max(8, r.top - 4 - Math.min(ph, above)) : r.bottom + 4}px`;
  };
  const ro = new window.ResizeObserver(place);
  ro.observe(pop);
  window.addEventListener("resize", place);
  window.addEventListener("scroll", place, true);
  place();
  return () => { ro.disconnect(); window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); pop.classList.remove("pinned"); };
}
