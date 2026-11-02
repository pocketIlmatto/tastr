// The page's address after the #: which view is open, so a refresh (or a saved link) reopens the same page and
// Back/Forward step between views. "track/42" is the Library with that track open; "explore/blend" is an Explore tab;
// "g-vibes" (a Glossary contents link) is a spot in the Glossary. Leaf module: nav.js applies a route.

const VIEWS = ["library", "explore", "glossary", "files", "admin"];

// routeOf("#explore/blend") -> { view: "explore", sub: "blend" }; anything unknown is the Library.
export function routeOf(hash = location.hash) {
  const s = decodeURIComponent(hash.replace(/^#/, ""));
  const m = /^track\/(\d+)$/.exec(s);
  if (m) return { view: "library", track: +m[1] };
  if (/^g-[\w-]+$/.test(s)) return { view: "glossary", anchor: s };
  const [view, sub] = s.split("/");
  return VIEWS.includes(view) ? { view, sub: sub || "" } : { view: "library" };
}

// setRoute("explore/find") records where we are. Moving to another view adds a history entry (so Back returns);
// a change inside the same view (the next track, another Explore tab) replaces it, so Back doesn't replay every click.
// Changing the address this way fires no event, so applying a route never loops back here.
export function setRoute(r) {
  if (location.hash === "#" + r) return;
  const same = routeOf().view === routeOf("#" + r).view;
  history[same ? "replaceState" : "pushState"](null, "", "#" + r);
}
