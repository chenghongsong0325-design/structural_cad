/* Geometry-derived room and circulation inspector. No LLM-generated claims. */
"use strict";
(() => {
  const byId = id => document.getElementById(id);
  const panel = byId("spatial-inspector");
  const toggle = byId("spatial-toggle");
  const floorSelect = byId("spatial-floor");
  const graphBox = byId("spatial-graph");
  const detailBox = byId("spatial-room");
  const conflictBox = byId("spatial-conflicts");
  const ns = "http://www.w3.org/2000/svg";
  let report = null, conflicts = [], selected = null, highlighted = new Set(), diagnostic = false;
  const kinds = { bedroom: "臥室", master_bedroom: "主臥", living: "起居空間", dining: "餐飲空間",
    kitchen: "廚房", study: "書房", stair_hall: "樓梯間", bathroom: "浴廁", storage: "收納",
    patio: "天井", pipe_shaft: "管道間", garage: "車庫", corridor: "走廊", elder_room: "孝親房" };
  const fixtureNames = {bed_double:"雙人床",bed_single:"單人床",bed_queen:"雙人床",sofa:"沙發",
    sofa_3seat:"沙發",sofa3:"三人沙發",desk:"書桌",wardrobe:"衣櫃",nightstand:"床頭櫃",car:"汽車",counter:"流理台",
    armchair:"單人椅",basin_small:"洗手台",closet_rail:"吊衣架",coffee_table:"茶几",fridge:"冰箱",
    shoe_cabinet:"鞋櫃",shower:"淋浴間",table4:"四人餐桌",toilet:"馬桶",tv_cabinet:"電視櫃"};
  function el(tag, text, parent, cls) {
    const e = document.createElement(tag);
    if (text != null) e.textContent = text;
    if (cls) e.className = cls;
    if (parent) parent.appendChild(e);
    return e;
  }
  function svgEl(tag, attrs, parent, text) {
    const e = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([k,v]) => e.setAttribute(k, String(v)));
    if (text != null) e.textContent = text;
    parent.appendChild(e);
    return e;
  }
  const currentFloor = () => report?.floors?.find(f => f.id === floorSelect.value);
  function showAnalysis(visible) {
    panel.classList.toggle("hidden", !visible);
    byId("viewer").classList.toggle("hidden", visible);
    byId("tabs").classList.toggle("hidden", visible);
    document.querySelector(".stage-hint").classList.toggle("hidden", visible);
    toggle.textContent = visible ? "返回平面圖" : "房間與動線分析";
    toggle.setAttribute("aria-expanded", String(visible));
  }
  toggle.addEventListener("click", () => showAnalysis(panel.classList.contains("hidden")));
  floorSelect.addEventListener("change", () => { selected = null; highlighted.clear(); draw(); });
  byId("spatial-adjacent").addEventListener("change", draw);
  byId("spatial-export").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify({diagnostic, spatial_report: report, conflicts}, null, 2)], {type:"application/json"});
    const url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url; a.download = diagnostic ? "未通過候選_空間診斷.json" : "空間推理紀錄.json";
    a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  window.renderSpatialInspector = (data, extraConflicts = [], failed = false) => {
    report = data || null; diagnostic = failed;
    conflicts = data?.conflicts || extraConflicts || [];
    selected = null; highlighted.clear(); floorSelect.replaceChildren();
    for (const floor of report?.floors || []) {
      const option = el("option", floor.label, floorSelect); option.value = floor.id;
    }
    toggle.disabled = !report && !conflicts.length;
    toggle.title = toggle.disabled ? "此方案未保存空間分析，重新生成後可查看。" : "";
    byId("spatial-status").textContent = diagnostic
      ? "本次未產生可交付圖面。以下為失敗原因或未通過候選的診斷資料。"
      : report?.status === "ready" ? "分析來自這次實際圖面。點選房間查看尺寸、連通路徑與檢核紀錄。"
      : report?.message || "這份歷史方案未保存空間分析；重新生成後可查看。";
    byId("spatial-status").classList.toggle("diagnostic", diagnostic);
    byId("spatial-method").textContent = report?.scope || "沒有可用的房間幾何資料。";
    byId("spatial-allocation").textContent = report?.bedroom_allocation
      ? `指定 ${report.bedroom_allocation.requested} 房：` + report.bedroom_allocation.floors.map(f => `${f.floor} ${f.assigned} 房`).join("、")
      : "";
    draw();
    showAnalysis(failed && !toggle.disabled);
  };
  function selectRoom(id) { selected = id; highlighted.clear(); draw(); }
  function draw() {
    graphBox.replaceChildren(); detailBox.replaceChildren(); conflictBox.replaceChildren();
    const floor = currentFloor();
    if (!floor || !floor.nodes?.length) {
      el("p", report?.status === "unverified" ? "空間分析未完成，不能推定可通行。" : "尚無可顯示的房間關係圖。", graphBox);
      drawConflicts(null); return;
    }
    const nodeMap = new Map(floor.nodes.map(n => [n.id,n]));
    const coordinates = floor.nodes.flatMap(n => n.polygon);
    const minX = Math.min(...coordinates.map(p=>p[0])), maxX = Math.max(...coordinates.map(p=>p[0]));
    const minY = Math.min(...coordinates.map(p=>p[1])), maxY = Math.max(...coordinates.map(p=>p[1]));
    const pad = Math.max(maxX-minX,maxY-minY)*.035;
    const map = p => [p[0]-minX+pad, maxY-p[1]+pad];
    const size = Math.max(maxX-minX,maxY-minY);
    const svg = svgEl("svg", {viewBox:`0 0 ${maxX-minX+2*pad} ${maxY-minY+2*pad}`, role:"group", "aria-label":`${floor.label} 房間與動線關係圖`}, graphBox);
    const chosen = nodeMap.get(selected), route = chosen?.route || [];
    const routePairs = new Set(route.slice(1).map((id,i)=>[route[i],id].sort().join("|")));
    const shapes = svgEl("g",{},svg), edges = svgEl("g",{},svg), labels = svgEl("g",{},svg);
    for (const node of floor.nodes) {
      const active = node.id === selected || highlighted.has(node.id);
      const poly = svgEl("polygon", {points:node.polygon.map(p=>map(p).join(",")).join(" "),
        class:`spatial-region${active?" selected":""}${node.reachable===false?" unreachable":""}`,
        "stroke-width":size*.003, tabindex:0, role:"button",
        "aria-label":`${floor.label} ${node.name}，${node.area_m2.toFixed(1)} 平方米，房間 ${node.index+1}`},shapes);
      poly.addEventListener("click",()=>selectRoom(node.id));
      poly.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();selectRoom(node.id);}});
    }
    function line(a,b,cls) {
      const [x1,y1]=map(nodeMap.get(a).center),[x2,y2]=map(nodeMap.get(b).center);
      svgEl("line",{x1,y1,x2,y2,class:cls,"stroke-width":size*.004},edges);
    }
    if (byId("spatial-adjacent").checked) {
      const walk = new Set(floor.edges.map(e=>[e.a,e.b].sort().join("|")));
      for (const [a,b] of floor.adjacency) if(!walk.has([a,b].sort().join("|"))) line(a,b,"spatial-edge adjacent");
    }
    for(const edge of floor.edges) line(edge.a,edge.b,`spatial-edge ${edge.kind}${routePairs.has([edge.a,edge.b].sort().join("|"))?" route":""}`);
    for(const node of floor.nodes) {
      const [x,y]=map(node.center);
      svgEl("circle",{cx:x,cy:y,r:size*.007,class:floor.anchors.includes(node.id)?"spatial-anchor":"spatial-node"},labels);
      svgEl("text",{x,y:y-size*.015,"font-size":size*.022,class:"spatial-label"},labels,node.name);
      svgEl("text",{x,y:y+size*.033,"font-size":size*.018,class:"spatial-label sub"},labels,`${node.area_m2.toFixed(1)} m²`);
    }
    el("p", `起點：${floor.anchor_status==="identified"?floor.anchor_basis:"未辨識，無法確認可達性"}。藍實線＝門，青虛線＝開放通道，灰虛線＝僅相鄰，金線＝選取房間的拓撲路徑。`,graphBox,"spatial-legend");
    drawRoom(chosen,floor,nodeMap); drawConflicts(floor);
  }
  function drawRoom(node,floor,nodeMap) {
    el("h3",node?`${floor.label} · ${node.name}`:"點選房間查看依據",detailBox);
    if(!node) { el("p","房間以獨立編號識別，同名房間也能分開查看。圖上連線表示房間關係，並非實際行走軌跡。",detailBox); return; }
    el("p",`${kinds[node.kind]||node.kind} · 編號 ${node.id}`,detailBox,"muted");
    el("p",`面積 ${node.area_m2.toFixed(2)} m²（牆中心線範圍）`,detailBox);
    const [x0,y0,x1,y1]=node.bounds_mm;
    el("p",`外接矩形 ${((x1-x0)/1000).toFixed(2)} × ${((y1-y0)/1000).toFixed(2)} 米；不等於淨寬。`,detailBox);
    el("h4","連通依據",detailBox);
    el("p",node.access_required===false?"天井／管道間不作為可進入的動線目的地。":node.reachable===null?"起點未知，尚未驗證可達。":node.reachable?"從本層起點可沿房間關係圖抵達。":"本層關係圖找不到抵達路徑。",detailBox);
    if(node.route.length) el("p",node.route.map(id=>nodeMap.get(id).name).join(" → "),detailBox);
    el("p",node.circulation?`房內動線：${node.circulation.ok?"通過現有模型":"需處理"}；量測 ${node.circulation.openings} 個開口、${node.circulation.targets} 個目標。${node.circulation.reason||""}`:"此用途／櫥櫃未納入房內動線模型。",detailBox);
    el("h4","門窗與家具",detailBox);
    const openings=floor.openings.filter(o=>o.room_ids.includes(node.id));
    el("p",openings.map(o=>`${o.kind==="window"?"窗":"門／通道"} ${(o.width_mm/1000).toFixed(2)}m${o.exterior?"（外牆）":""}`).join("、")||"未量得貼房間邊界的門窗。",detailBox);
    el("p",node.fixtures.length?node.fixtures.map(n=>fixtureNames[n]||n).join("、"):"未配置家具。",detailBox);
    const issues=conflicts.filter(c=>(c.room_ids||[]).includes(node.id));
    el("h4","檢核紀錄",detailBox);
    if(!issues.length) el("p","現有報告未列出可定位至此房間的問題；樓層或全棟問題仍須一併查看。",detailBox);
    for(const issue of issues) el("p",`${issue.location==="ambiguous"?"同名房間候選，尚未唯一定位：":""}${issue.detail}`,detailBox,"spatial-room-issue");
    const details=el("details",null,detailBox); el("summary","查看座標（毫米）",details);
    el("pre",node.polygon.map(p=>p.map(v=>v.toFixed(0)).join(", ")).join("\n"),details);
  }
  function drawConflicts(floor) {
    el("h3",`衝突與設計警告（${conflicts.length}）`,conflictBox);
    if(!conflicts.length) { el("p",report?.status==="ready"?"現有報告沒有待處理紀錄；分析範圍與假設仍適用。":"沒有可用的檢核紀錄。",conflictBox); return; }
    for(const issue of conflicts) {
      const box=el("article",null,conflictBox,"spatial-conflict");
      el("b",`${issue.severity==="warning"?"設計警告":"待處理"} · ${issue.floor||"全棟"} · ${issue.code}`,box);
      el("p",issue.detail||`要求 ${issue.requested} 房，目前可配置區塊 ${issue.available} 個。`,box);
      if(issue.floor_capacities) el("p",issue.floor_capacities.map(f=>`${f.floor}：${f.capacity} 個`).join("、"),box);
      if(issue.scope) el("small",issue.scope,box);
      if(issue.evidence) el("p",`原始要求：${issue.evidence}`,box);
      el("p",issue.suggestion||"請核對配置與原始需求後重試。",box);
      if(issue.location==="ambiguous") el("small","原檢查器只記同名房間，以下位置都是候選，不能確定只有其中一間。",box);
      if(issue.room_ids?.length) {
        const button=el("button",issue.location==="ambiguous"?"標示候選房間":"查看位置",box,"btn quiet");
        button.addEventListener("click",()=>{
          const target=report.floors.find(f=>f.nodes.some(n=>issue.room_ids.includes(n.id)));
          if(target){floorSelect.value=target.id; selected=issue.room_ids.length===1?issue.room_ids[0]:null;
            highlighted=new Set(issue.room_ids);draw();}
        });
      }
    }
  }
})();
