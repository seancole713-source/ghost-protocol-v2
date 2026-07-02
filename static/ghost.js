/* ═══════════════════════════════════════════════════════════════
 * Ghost Protocol v2 — shared JS utilities
 * Used by cockpit.html and admin.html
 * ═══════════════════════════════════════════════════════════════ */

/* ─── HTML escaping ─── */
function escHtml(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,'&#39;').replace(/"/g,'&quot;')}

/* ─── Timestamp formatting ─── */
function fmtTsIso(ts){if(ts==null||ts===0)return '—';var s=Number(ts);if(s>1e12)s=Math.floor(s/1000);var d=new Date(s*1000);if(isNaN(d.getTime()))return '—';return d.toISOString().replace('T',' ').slice(0,19)+' UTC'}
function fmtTimeAgo(ts){if(!ts)return '';var s=Math.floor(Date.now()/1000)-Number(ts);if(s<60)return 'just now';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago'}
function fmtDateShort(ts){if(ts==null||ts===0)return '—';var s=Number(ts);if(s>1e12)s=Math.floor(s/1000);var d=new Date(s*1000);if(isNaN(d.getTime()))return '—';return d.toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'})}

/* ─── Number formatting ─── */
function fmtPrice(v){if(v==null||isNaN(v))return '—';var n=Number(v);if(n>=100)return '$'+n.toFixed(2);if(n>=1)return '$'+n.toFixed(3);return '$'+n.toFixed(6)}
function fmtMoney(v,places){if(v==null||isNaN(v))return '—';var n=Number(v);if(Math.abs(n)>=1e9)return '$'+(n/1e9).toFixed(2)+'B';if(Math.abs(n)>=1e6)return '$'+(n/1e6).toFixed(2)+'M';if(Math.abs(n)>=1e3)return '$'+(n/1e3).toFixed(2)+'K';return '$'+n.toFixed(places==null?2:places)}
function fmtPct(v,places){if(v==null||isNaN(v))return '—';var n=Number(v);return(n>=0?'+':'')+n.toFixed(places==null?2:places)+'%'}
function fmtInt(v){if(v==null||isNaN(v))return '—';return Number(v).toLocaleString('en-US')}
function fmtVol(v){if(v==null||isNaN(v))return '—';var n=Number(v);if(n>=1e9)return (n/1e9).toFixed(2)+'B';if(n>=1e6)return (n/1e6).toFixed(2)+'M';if(n>=1e3)return (n/1e3).toFixed(2)+'K';return n.toLocaleString('en-US')}
function fmtUsd(v){if(v==null||v===''||isNaN(Number(v))||Number(v)<=0)return '—';return '$'+Number(v).toFixed(2)}

/* ─── Safe JSON fetch ─── */
function _fetchJson(url, fallback){
  return fetch(url).then(function(r){
    if(!r.ok) return fallback || {ok:false, error:'HTTP '+r.status};
    return r.json();
  }).catch(function(){ return fallback || {ok:false, error:'network error'}; });
}

/* ─── Deploy meta ─── */
function renderDeployMeta(v, elId){
  var el = document.getElementById(elId || 'deploy-badge');
  if(!el||!v)return;
  var short=(v.git_sha_short||(v.git_sha&&v.git_sha!=='unset'?v.git_sha.slice(0,7):'unset'));
  el.className = 'deploy-badge';
  el.textContent = 'deploy ' + short;
  el.title = 'git ' + String(v.git_sha||'unset')
    + (v.deploy_id && v.deploy_id !== 'unset' ? '\ndeploy ' + v.deploy_id : '')
    + (v.app_version ? '\nv' + v.app_version : '');
  var metaBuild = document.querySelector('meta[name="ghost-build"]');
  if(metaBuild && metaBuild.getAttribute('content') && metaBuild.getAttribute('content') !== short){
    el.className = 'deploy-badge unset';
    el.title = 'Cached page build ' + metaBuild.getAttribute('content') + ' · live ' + short + '\nHard refresh (Cmd+Shift+R)';
  }
}

/* ─── Model table renderer ─── */
/* Flatten /api/v3/status symbols into [label, summary] rows.
 * Phase 2 shape: symbols[sym] = {UP:{...}, DOWN:{...}} (nested per direction).
 * Legacy shape:  symbols[sym] = {...flat summary...}. */
function _modelRows(syms){
  var rows=[];
  Object.keys(syms).sort().forEach(function(k){
    var m=syms[k]||{};
    if(m.accuracy!=null||m.engine!=null){rows.push([k,m]);return}   /* legacy flat */
    ['UP','DOWN'].forEach(function(dir){
      if(m[dir])rows.push([k+' '+dir,m[dir]]);
    });
  });
  return rows;
}

function renderModelTable(v3){
  if(!v3||!v3.trained)return '<p class="kv">'+escHtml(v3&&v3.reason?v3.reason:'No v3 models loaded.')+'</p>';
  var rows=_modelRows(v3.symbols||{});
  if(!rows.length)return '<p class="kv">No symbol rows.</p>';
  var h='<table class="tbl"><thead><tr><th>Symbol</th><th>Engine</th><th>Label</th><th>Acc</th><th>WF mean</th><th>WF min</th><th>Edge</th><th>N</th></tr></thead><tbody>';
  rows.forEach(function(pair){
    var k=pair[0],m=pair[1];
    h+='<tr><td><b>'+escHtml(k)+'</b></td><td>'+escHtml(m.engine!=null?m.engine:'—')+'</td>'
      +'<td>'+escHtml(m.label_type||'—')+'</td>'
      +'<td>'+(m.accuracy!=null?m.accuracy:'—')+'%</td>'
      +'<td>'+(m.wf_acc_mean!=null?m.wf_acc_mean:'—')+'%</td>'
      +'<td>'+(m.wf_acc_min!=null?m.wf_acc_min:'—')+'%</td>'
      +'<td>'+(m.edge!=null?m.edge:'—')+'%</td>'
      +'<td>'+(m.n_samples!=null?m.n_samples:0)+'</td></tr>';
  });
  h+='</tbody></table>';
  return h;
}

/* ─── Gate badge ─── */
function gateBadge(pass){
  return pass ? '<span class="ok">&#10003; pass</span>' : '<span class="fail">&#10007; fail</span>';
}

/* ─── Outcome badge ─── */
function _outcomeBadge(o){
  var map = {WIN:['#1ec77d','WIN'], LOSS:['#ff4d4f','LOSS'], EXPIRED:['#8b96a8','EXP'], WITHDRAWN:['#ffb84d','WDR']};
  if(!o) return '<span style="color:#f5a623;font-weight:600">OPEN</span>';
  var x = map[o] || ['#8b96a8', o];
  return '<span style="color:' + x[0] + ';font-weight:700">' + x[1] + '</span>';
}

/* ─── P&L percent ─── */
function _pjPct(v){ return (v === null || v === undefined) ? '—' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%'; }

/* ─── Time left ─── */
function timeLeft(ts){var s=(ts||0)-(Date.now()/1000);if(s<=0)return 'Expired';var h=Math.floor(s/3600);if(h>=24)return Math.floor(h/24)+'d left';return h+'h left'}
