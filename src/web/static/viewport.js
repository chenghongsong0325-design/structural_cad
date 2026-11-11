"use strict";
window.PlanViewport = {
  fit(svg) {
    if (!svg) return;
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    const original = svg.viewBox.baseVal;
    const boxes = [];
    for (const node of svg.children) {
      if (!(node instanceof SVGGraphicsElement) || node.matches("defs,style,title,desc")) continue;
      const paper = node.hasAttribute("data-preview-paper") || (node.tagName.toLowerCase() === "rect"
        && (node.getAttribute("width") === "100%" ||
          (node.x.baseVal.value === original.x && node.y.baseVal.value === original.y
           && node.width.baseVal.value >= original.width && node.height.baseVal.value >= original.height)));
      if (paper) { node.setAttribute("data-preview-paper", ""); continue; }
      try {
        const b = node.getBBox();
        if (b.width || b.height) {
          const matrix = svg.getCTM().inverse().multiply(node.getCTM());
          const corners = [[b.x,b.y],[b.x+b.width,b.y],[b.x+b.width,b.y+b.height],[b.x,b.y+b.height]]
            .map(([x,y]) => new DOMPoint(x,y).matrixTransform(matrix));
          const x = Math.min(...corners.map(p=>p.x)), y = Math.min(...corners.map(p=>p.y));
          boxes.push({x,y,width:Math.max(...corners.map(p=>p.x))-x,height:Math.max(...corners.map(p=>p.y))-y});
        }
      } catch (_) { /* Keep the server viewBox if no drawable bounds exist. */ }
    }
    if (!boxes.length) return;
    const x = Math.min(...boxes.map(b=>b.x)), y = Math.min(...boxes.map(b=>b.y));
    const w = Math.max(...boxes.map(b=>b.x+b.width))-x;
    const h = Math.max(...boxes.map(b=>b.y+b.height))-y;
    const pad = Math.max(w,h)*.025;
    svg.setAttribute("viewBox", `${x-pad} ${y-pad} ${w+2*pad} ${h+2*pad}`);
  },
};
