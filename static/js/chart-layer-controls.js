(function(){
'use strict';
var P='gpu_tag_layer_';
function g(k){var v=localStorage.getItem(P+k);return v==='1';}
function s(k,v){localStorage.setItem(P+k,v?'1':'0');}
function state(){return{pct:g('pct'),lh:g('lh'),sig:g('sig'),pts:g('pts'),pat:g('pat')};}
function nm(x){return String((x&&x.name)||'');}
function has(n,a){return n.indexOf(a)>=0;}
function kind(x){var n=nm(x);if(has(n,'P05')||has(n,'P20')||has(n,'P80')||n==='P50')return'pct';if(has(n,'LL')||has(n,'L-H')||has(n,'L–H')||has(n,'H-HH')||has(n,'H–HH'))return'lh';if(has(n,'sigma')||has(n,'σ')||n==='CL'||n==='Ascendente'||n==='Descendente'||n==='Pegado'||has(n,'Fuera')||has(n,'Sobre CL')||has(n,'Bajo CL'))return'sig';if(n==='Rotura'||n==='Pre-rotura')return'pts';return'other';}
function real(x){var n=nm(x).toLowerCase();return x.type==='line'&&n!=='cl'&&!has(n,'ideal')&&!has(n,'proy')&&!has(n,'p50');}
function noPts(x){if(!real(x))return x;var y=Object.assign({},x);y.marker={enabled:false};y.data=(x.data||[]).map(function(p){return Array.isArray(p)?p:{x:p.x,y:p.y};});return y;}
function weekend(p){var x=Array.isArray(p)?p[0]:p.x;var d=new Date(x).getDay();return d===0||d===6;}
function splitLimit(x){if(kind(x)!=='lh')return[x];var wd=[],we=[];(x.data||[]).forEach(function(p){(weekend(p)?we:wd).push(p);});var out=[];if(wd.length){var a=Object.assign({},x);a.name=nm(x)+' patron LV';a.data=wd;a.color='#00bcd4';a.lineColor='#00bcd4';a.fillColor='rgba(0,188,212,0.16)';a.fillOpacity=0.16;out.push(a);}if(we.length){var b=Object.assign({},x);b.name=nm(x)+' patron SD';b.data=we;b.color='#e040fb';b.lineColor='#e040fb';b.fillColor='rgba(224,64,251,0.18)';b.fillOpacity=0.18;out.push(b);}return out;}
function patchFetch(){if(window.__layerFetch)return;window.__layerFetch=1;var old=window.fetch.bind(window);window.fetch=function(r,o){try{if(typeof r==='string'){var u=new URL(r,location.origin);if(u.pathname==='/api/chart'&&state().pat){u.searchParams.set('sigma_alarm','1');r=u.pathname+u.search+u.hash;}}}catch(e){}return old(r,o);};}
function patchChart(){if(!window.Highcharts||window.__layerChart)return;window.__layerChart=1;var old=Highcharts.stockChart;Highcharts.stockChart=function(c,opt,cb){var st=state();var next=Object.assign({},opt);var arr=[];(opt.series||[]).forEach(function(x){var k=kind(x);if(k==='pct'&&!st.pct)return;if(k==='lh'&&!st.lh&&!st.pat)return;if(k==='sig'&&!st.sig)return;if(k==='pts'&&!st.pts)return;var y=st.pts?x:noPts(x);if(st.pat&&kind(y)==='lh')splitLimit(y).forEach(function(z){arr.push(z);});else arr.push(y);});next.series=arr;return old.call(Highcharts,c,next,cb);};}
function cb(id,t,k){var l=document.createElement('label');l.className='chk layer-ctl';var c=document.createElement('input');c.type='checkbox';c.id=id;c.checked=g(k);l.appendChild(c);l.appendChild(document.createTextNode(' '+t));c.addEventListener('change',function(){s(k,c.checked);var b=document.getElementById('btn-refresh');if(b)b.click();});return l;}
function hideOld(){['chk-hide-pct','chk-hide-lh','chk-lh-sigma','sel-lh-sigma','chk-sixsigma','chk-sigma-alarm'].forEach(function(id){var e=document.getElementById(id);if(e){var w=e.closest('label')||e;w.style.display='none';}});}
function ui(){var t=document.querySelector('.chart-toolbar');if(!t||document.getElementById('layer-controls'))return;hideOld();var b=document.createElement('div');b.id='layer-controls';b.className='zoom-btns layer-controls';var title=document.createElement('span');title.className='lbl';title.textContent='Capas';b.appendChild(title);b.appendChild(cb('layer-pct','Percentiles','pct'));b.appendChild(cb('layer-lh','L/H percentil','lh'));b.appendChild(cb('layer-sig','Six Sigma','sig'));b.appendChild(cb('layer-pts','Puntos alarma','pts'));b.appendChild(cb('layer-pat','LL/HH 3 sigma patron','pat'));t.insertBefore(b,t.firstChild);}
patchFetch();patchChart();document.addEventListener('DOMContentLoaded',ui);
})();
