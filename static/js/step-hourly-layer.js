(function(){
'use strict';
var K='gpu_tag_layer_step',LAST=null;
function en(){return localStorage.getItem(K)==='1';}
function sv(v){localStorage.setItem(K,v?'1':'0');}
function serie(n,d,c,z){return{name:n,type:'arearange',step:'left',connectNulls:false,data:d||[],color:c,lineColor:c,fillColor:c.replace(')',',.18)').replace('rgb','rgba'),fillOpacity:.18,lineWidth:1,zIndex:z};}
function backendBands(){var sp=LAST&&LAST.step_patterns;if(!sp||!sp.series)return[];var s=sp.series,out=[];if(s.weekday&&s.weekday.length)out.push(serie('LL/HH step lunes-viernes',s.weekday,'#00bcd4',7));if(s.weekend&&s.weekend.length)out.push(serie('LL/HH step sab-dom',s.weekend,'#e040fb',8));if(s.holiday&&s.holiday.length)out.push(serie('LL/HH step feriado Chile',s.holiday,'#ff1744',9));return out;}
function pf(){if(window.__stepFetch)return;window.__stepFetch=1;var old=window.fetch.bind(window);window.fetch=function(r,o){var ch=false;try{if(typeof r==='string'){var u=new URL(r,location.origin);ch=u.pathname==='/api/chart';if(ch&&en()){u.searchParams.set('step','1');r=u.pathname+u.search+u.hash;}}}catch(e){}return old(r,o).then(function(resp){if(ch){try{resp.clone().json().then(function(j){LAST=j;}).catch(function(){});}catch(e){}}return resp;});};}
function pc(){if(!window.Highcharts||window.__stepChart)return;window.__stepChart=1;var old=Highcharts.stockChart;Highcharts.stockChart=function(c,opt,cb){var next=Object.assign({},opt),arr=(opt.series||[]).slice();if(en())backendBands().forEach(function(x){arr.push(x);});next.series=arr;return old.call(Highcharts,c,next,cb);};}
function ui(){var t=document.getElementById('layer-controls')||document.querySelector('.chart-toolbar');if(!t||document.getElementById('layer-step'))return;var l=document.createElement('label');l.className='chk layer-ctl';var c=document.createElement('input');c.type='checkbox';c.id='layer-step';c.checked=en();l.appendChild(c);l.appendChild(document.createTextNode(' LL/HH step'));c.addEventListener('change',function(){sv(c.checked);var b=document.getElementById('btn-refresh');if(b)b.click();});t.appendChild(l);}
pf();pc();document.addEventListener('DOMContentLoaded',ui);
})();
