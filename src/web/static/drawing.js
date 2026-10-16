/* 製圖設定與結果；全部使用 textContent，房間名稱不當成 HTML。 */
(() => {
  const $ = id => document.getElementById(id);
  const definitions = [
    ['floor_height_cm','層高',360,240,600], ['ceiling_height_cm','天花高度',280,200,500],
    ['exterior_wall_cm','外牆厚',20,12,50], ['interior_wall_cm','內牆厚',12,10,30],
    ['column_cm','方柱邊長',40,30,100], ['door_height_cm','門高',210,190,350],
    ['window_height_cm','窗高',150,120,300], ['window_sill_cm','窗台高',90,0,200],
    ['shutter_height_cm','捲門高',240,200,400], ['bathroom_tile_cm','浴室壁磚高',240,0,400],
  ];
  function node(tag, text, parent) {
    const el = document.createElement(tag);
    if (text != null) el.textContent = text;
    if (parent) parent.append(el);
    return el;
  }
  for (const [key,label,value,min,max] of definitions) {
    const wrap = node('label',label,$('drawing-inputs'));
    const input = node('input',null,wrap);
    Object.assign(input,{id:`drawing-${key}`,type:'number',min:String(min),max:String(max),step:'any',value:String(value),required:true});
  }
  $('drawing-enabled').addEventListener('change',() => {$('drawing-fields').disabled = !$('drawing-enabled').checked;});
  function addRoom(data = {}) {
    const box = node('div',null,$('drawing-rooms'));
    box.className = 'drawing-room drawing-grid';
    for (const [key,label,type,placeholder] of [
      ['floor','樓層','text','1F'], ['name','室名','text','客廳'], ['index','房間序號（同名才填）','number',''],
      ['width_cm','淨寬 cm（選填）','number',''], ['depth_cm','淨深 cm（選填）','number',''],
    ]) {
      const input = node('input',null,node('label',label,box));
      Object.assign(input,{type,placeholder,value:String(data[key]??'')});
      input.dataset.key = key;
      if (type === 'number') {input.min = key === 'index' ? '0' : '0.1';input.step = key === 'index' ? '1' : 'any';input.max='10000';}
      else {input.required=true;input.maxLength=key==='floor'?12:40;}
    }
    const select = node('select',null,node('label','位置',box));
    select.dataset.key='position';
    for (const [value,label] of [['auto','自動安排'],['front','前方'],['back','後方'],['left','左方'],['right','右方']]) {
      const opt = node('option',label,select);opt.value=value;
    }
    select.value=data.position||'auto';
    const remove = node('button','移除此條件',box);remove.type='button';remove.className='btn quiet';remove.onclick=()=>box.remove();
  }
  $('drawing-add-room').onclick=()=>addRoom();
  function read() {
    const enabled = $('drawing-enabled').checked;
    if (!enabled) return {enabled:false};
    for (const input of $('drawing-fields').querySelectorAll('input')) {
      if (!input.reportValidity()) throw new Error('請先修正製圖設定中標示的欄位。');
    }
    const settings={enabled,window_style:$('drawing-window-style').value,rooms:[]};
    for (const [key] of definitions) settings[key]=Number($(`drawing-${key}`).value);
    for (const box of document.querySelectorAll('.drawing-room')) {
      const r={};
      for (const field of box.querySelectorAll('[data-key]')) {
        if (field.value !== '') r[field.dataset.key]=field.type==='number'?Number(field.value):field.value.trim();
      }
      settings.rooms.push(r);
    }
    return settings;
  }
  function load(settings) {
    $('drawing-enabled').checked=!!settings?.enabled;
    $('drawing-fields').disabled=!settings?.enabled;
    for (const [key,,value] of definitions) $(`drawing-${key}`).value=settings?.[key]??value;
    $('drawing-window-style').value=settings?.window_style||'sliding';
    $('drawing-rooms').replaceChildren();
    for (const r of settings?.rooms||[]) addRoom(r);
  }
  function table(parent, headings, rows) {
    const scroll=node('div',null,parent);scroll.className='drawing-table-scroll';
    const t=node('table',null,scroll);t.className='drawing-table';
    const head=node('tr',null,node('thead',null,t));
    headings.forEach(h=>node('th',h,head));
    const body=node('tbody',null,t);
    for (const row of rows) {const tr=node('tr',null,body);row.forEach(c=>node('td',c,tr));}
  }
  function download(name,content,type) {
    const url=URL.createObjectURL(new Blob([content],{type}));
    const a=node('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),2000);
  }
  function render(data) {
    load(data.brief_data?._drawing);
    const report=data.drawing_report, box=$('drawing-result');box.replaceChildren();box.classList.toggle('hidden',!report);
    if (!report) return;
    node('h3','澄跡製圖成果',box);
    node('p','尺寸 cm · DXF／PDF 1:50。列印請選「實際大小／100%」；紙張會依圖面選擇。',box);
    const buttons=node('div',null,box);buttons.className='drawing-downloads';
    const json=node('button','下載完整明細 JSON',buttons);json.type='button';json.className='btn quiet';
    json.onclick=()=>download('drawing_report.json',JSON.stringify(report,null,2),'application/json');
    const csv=node('button','下載數量 CSV',buttons);csv.type='button';csv.className='btn quiet';
    csv.onclick=()=>{
      const rows=[['樓層','項目','計算式','數量','單位','坪','備註']];
      for (const f of report.floors) for (const q of f.quantities) rows.push([f.label,q.item,q.formula,q.value,q.unit,q.ping??'',q.note]);
      const quote=v=>'"'+String(v).replaceAll('"','""')+'"';
      download('quantities.csv','\ufeff'+rows.map(r=>r.map(quote).join(',')).join('\r\n'),'text/csv;charset=utf-8');
    };
    const assumptions=node('details',null,box);node('summary','計算依據與自動配置假設',assumptions);
    for (const note of report.notes) node('p',note,assumptions);
    for (const f of report.floors) {
      const details=node('details',null,box);node('summary',`${f.label} 房間與裝修明細`,details);
      table(details,['序號／室名','位置','淨寬×深 cm','淨面積 m²','周長 cm'],f.rooms.map(r=>[`${r.index} ${r.name}`,r.position,`${r.width_cm.toFixed(1)}×${r.depth_cm.toFixed(1)}${r.rectangular?'':'（凹形外包）'}`,r.area_m2.toFixed(3),r.perimeter_cm.toFixed(1)]));
      table(details,['項目','計算式','數量','單位','坪'],f.quantities.map(q=>[q.item,q.formula,q.value.toFixed(3),q.unit,q.ping==null?'—':q.ping.toFixed(3)]));
    }
    const openings=node('details',null,box);node('summary','門窗型號總表',openings);
    table(openings,['編號','型式','寬×高 cm','窗台 cm','數量'],report.openings.map(o=>[o.code,o.type,`${o.width_cm}×${o.height_cm}`,o.sill_cm,o.count]));
    node('p','平面、柱牆表、淨尺寸、周長明細與各型門窗立面，請在右側圖紙頁籤切換。',box);
  }
  window.DrawingProfile={read,render};
})();
