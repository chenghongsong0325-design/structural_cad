/* Trace a disposable copy of the actual sheet; the original SVG is never edited.
   No invented lines, geometry jitter, network requests, or changes to exports. */
"use strict";

window.PlanSketch = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const shapes = "path,line,polyline,polygon,rect,circle,ellipse,text,image,use";
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let active = null;

  function finish() { if (active) active.finish(); }

  function play(svg, { speed = 1, onProgress = () => {}, onFinish = () => {} } = {}) {
    finish();
    if (!svg || motion.matches) { onProgress(1); onFinish(); return; }
    const originalVisibility = svg.getAttribute("visibility");
    const copy = svg.cloneNode(true);
    copy.classList.add("sketch-copy");
    copy.setAttribute("aria-hidden", "true");
    copy.setAttribute("focusable", "false");
    const entries = [];
    let frame = 0, ended = false;
    const session = { finish: () => {
      if (ended) return;
      ended = true;
      cancelAnimationFrame(frame);
      copy.remove();
      if (originalVisibility === null) svg.removeAttribute("visibility");
      else svg.setAttribute("visibility", originalVisibility);
      if (active === session) active = null;
      onProgress(1);
      onFinish();
    }};
    active = session;
    try {
      const sources = [...svg.querySelectorAll(shapes)];
      const clones = [...copy.querySelectorAll(shapes)];
      const box = svg.viewBox.baseVal;
      const unit = Math.max(box.width, box.height, 1) / 900;
      let total = 0;
      // Read styles before hiding the original. Definitions are not visible marks.
      sources.forEach((node, i) => {
        if (node.closest("defs,clipPath,mask,pattern,marker,symbol")) return;
        const target = clones[i], style = getComputedStyle(node);
        if (style.display === "none" || style.visibility === "hidden") return;
        const isPaper = node.tagName.toLowerCase() === "rect"
          && node.parentElement === svg && node.x.baseVal.value === box.x
          && node.y.baseVal.value === box.y && node.width.baseVal.value >= box.width
          && node.height.baseVal.value >= box.height;
        if (isPaper) {
          target.style.fill = "#faf8f2";
          target.style.stroke = "none";
          return;
        }
        const hasStroke = style.stroke !== "none" && parseFloat(style.strokeOpacity) > 0;
        const hasFill = style.fill !== "none" && parseFloat(style.fillOpacity) > 0;
        let length = 0;
        try { length = node.getTotalLength(); } catch (_) { /* text/image/use fade in */ }
        const trace = Number.isFinite(length) && length > 0 && (hasStroke || hasFill);
        target.style.opacity = "0";
        if (trace) {
          target.style.stroke = "#4b4943";
          target.style.strokeOpacity = "0.88";
          target.style.strokeWidth = String(hasStroke ? Math.max(parseFloat(style.strokeWidth) || 0, unit * .8) : unit * .65);
          target.style.strokeLinecap = "round";
          target.style.strokeLinejoin = "round";
          // getTotalLength uses user units; a pathLength attribute would rescale dashes.
          target.removeAttribute("pathLength");
          target.style.strokeDasharray = `${length} ${length}`;
          target.style.strokeDashoffset = String(length);
          target.style.fill = hasFill ? "#4b4943" : "none";
          target.style.fillOpacity = "0";
        }
        const weight = trace ? Math.max(1, Math.min(12, Math.sqrt(length / unit))) : 1;
        entries.push({ target, length, trace, hasFill, start: total, weight });
        total += weight;
      });
      if (!entries.length) { session.finish(); return; }
      copy.style.background = "#faf8f2";
      svg.parentElement.appendChild(copy);
      svg.setAttribute("visibility", "hidden");
      const pen = document.createElementNS(NS, "g");
      pen.innerHTML = '<circle r="7" fill="#b5a385" opacity=".12"/><path d="M0 0L5 -16L10 -11Z" fill="#dfc291" stroke="#625646" stroke-width=".7"/><path d="M0 0L2 -6L4 -4Z" fill="#4b4943"/>';
      pen.style.pointerEvents = "none";
      copy.appendChild(pen);
      const duration = 10000 / Math.max(.5, Math.min(3, Number(speed) || 1));
      let started = null, cursor = 0;
      function paint(entry, fraction) {
        entry.target.style.opacity = "1";
        if (entry.trace) {
          entry.target.style.strokeDashoffset = String(entry.length * (1 - fraction));
          if (entry.hasFill) entry.target.style.fillOpacity = String(Math.max(0, (fraction - .7) / .3));
        } else entry.target.style.opacity = String(fraction);
      }
      function tick(now) {
        if (ended) return;
        try {
          if (!svg.isConnected) { session.finish(); return; }
          if (started === null) started = now;
          const progress = Math.min(1, (now - started) / duration);
          const distance = progress * total;
          while (cursor < entries.length && distance >= entries[cursor].start + entries[cursor].weight) {
            paint(entries[cursor++], 1);
          }
          const entry = entries[cursor];
          pen.style.display = "none";
          if (entry) {
            const fraction = Math.min(1, (distance - entry.start) / entry.weight);
            paint(entry, fraction);
            if (entry.trace) {
              const point = entry.target.getPointAtLength(entry.length * fraction);
              const transform = copy.getScreenCTM().inverse().multiply(entry.target.getScreenCTM());
              const tip = new DOMPoint(point.x, point.y).matrixTransform(transform);
              pen.setAttribute("transform", `translate(${tip.x} ${tip.y}) scale(${unit * 1.4})`);
              pen.style.display = "";
            }
          }
          onProgress(progress);
          if (progress >= 1) session.finish();
          else frame = requestAnimationFrame(tick);
        } catch (_) { session.finish(); } // An unsupported SVG still shows its exact original.
      }
      onProgress(0);
      frame = requestAnimationFrame(tick);
    } catch (_) { session.finish(); }
  }

  motion.addEventListener("change", () => { if (motion.matches) finish(); });
  return { play, finish, get running() { return active !== null; } };
})();
